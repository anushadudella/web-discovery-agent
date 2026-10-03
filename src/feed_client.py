"""Calendar-feed (ICS) discovery client. Free: no AI, no API key.

Same role and output shape as sonar_client: it PROPOSES candidates, flags
problems, and never asserts confidence (needs_review is always True).

Why this exists: official campus calendars (e.g. Texas Today, which runs on
Localist) publish ICS feeds with exact titles, times, and location text, so
there is nothing for an AI to guess. Only the "is it free?" judgement is
heuristic here, and anything uncertain is flagged for human review.

Only events with free-evidence become candidates. Everything else is counted
in the run stats (skipped_not_free etc.) so you can audit what was ignored.
"""
from __future__ import annotations

import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .sonar_client import CollegeConfig, DiscoveryResult, RawCandidate, _host_allowed

# --------------------------------------------------------------------------- #
# ICS parsing (minimal, stdlib only)
# --------------------------------------------------------------------------- #


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """'DTSTART;TZID=America/Chicago:20261005T180000' -> (name, params, value)."""
    in_quotes, colon = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon = i
            break
    if colon == -1:
        return None
    head, value = line[:colon], line[colon + 1 :]
    parts = head.split(";")
    params: dict[str, str] = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_ics(text: str) -> list[dict[str, tuple[dict[str, str], str]]]:
    """Return one dict per VEVENT: {PROPNAME: (params, raw_value)}. Nested VALARMs ignored."""
    events: list[dict] = []
    current: dict | None = None
    alarm_depth = 0
    for line in _unfold(text):
        u = line.strip().upper()
        if u == "BEGIN:VEVENT":
            current, alarm_depth = {}, 0
        elif u == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None:
            if u == "BEGIN:VALARM":
                alarm_depth += 1
            elif u == "END:VALARM":
                alarm_depth -= 1
            elif alarm_depth == 0:
                prop = _split_property(line)
                if prop:
                    name, params, value = prop
                    current.setdefault(name, (params, value))  # first wins
    return events


def _parse_ics_datetime(
    params: dict[str, str], value: str, campus_tz: ZoneInfo
) -> tuple[datetime | date | None, bool]:
    """Return (value, is_all_day). Floating times are interpreted in campus tz."""
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
            return datetime.strptime(value[:8], "%Y%m%d").date(), True
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
        naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
        tz = campus_tz
        if "TZID" in params:
            try:
                tz = ZoneInfo(params["TZID"])
            except Exception:  # noqa: BLE001  (unknown/Windows tz name)
                tz = campus_tz
        return naive.replace(tzinfo=tz), False
    except ValueError:
        return None, False


# --------------------------------------------------------------------------- #
# "Is it free?" heuristic (conservative; uncertain -> flags, not confidence)
# --------------------------------------------------------------------------- #
# Standalone word "free" only: not gluten-free, sugar-free, freedom, etc.
_FREE_RE = re.compile(r"(?<![-\w])free(?![-\w])", re.I)
_GIVEAWAY_RE = re.compile(r"(?<![-\w])(giveaway|giveaways|no cost|free of charge)(?![-\w])", re.I)
_FREE_FALSE_POSITIVES = re.compile(
    r"free (speech|market|markets|trade|will|time|agent|software|throw|fall|lance|range|tier|"
    r"press|expression|enterprise|world)|(not|isn't|aren't|never) free|free[- ]?for[- ]?all",
    re.I,
)
_PRICE_RE = re.compile(r"\$\s?\d")
_CONDITIONAL_RE = re.compile(r"free (for|to) (members|member|students with|alumni|faculty|staff|"
                             r"first|the first|those who|attendees who)", re.I)


def find_free_evidence(text: str) -> tuple[str | None, list[str]]:
    """Return (evidence snippet <= 25 words or None, extra flags)."""
    if not text:
        return None, []
    cleaned = _FREE_FALSE_POSITIVES.sub(" ", text)
    m = _FREE_RE.search(cleaned) or _GIVEAWAY_RE.search(cleaned)
    if not m:
        return None, []
    words = cleaned.split()
    # Window of words around the match for a short verbatim-ish snippet.
    prefix_words = len(cleaned[: m.start()].split())
    lo, hi = max(0, prefix_words - 6), min(len(words), prefix_words + 12)
    snippet = " ".join(words[lo:hi])
    flags: list[str] = []
    if _PRICE_RE.search(text):
        flags.append("possible_paid_or_conditional")
    if _CONDITIONAL_RE.search(text):
        flags.append("conditional_free")
    return snippet, flags


# --------------------------------------------------------------------------- #
# Feed -> candidates
# --------------------------------------------------------------------------- #
_IMG_RE = re.compile(r"\.(jpe?g|png|gif|webp)(\?.*)?$", re.I)


def _val(ev: dict, key: str) -> str | None:
    if key not in ev:
        return None
    v = _unescape(ev[key][1]).strip()
    return v or None


def candidates_from_ics(
    text: str,
    college: CollegeConfig,
    window_start: date,
    window_end: date,
    now: datetime | None = None,
) -> tuple[list[RawCandidate], list[dict], dict[str, int]]:
    """Returns (candidates, rejected, stats)."""
    tz = ZoneInfo(college.timezone)
    now = now or datetime.now(timezone.utc)
    events = parse_ics(text)
    stats = {
        "events_in_feed": len(events),
        "skipped_cancelled": 0,
        "skipped_not_free": 0,
        "skipped_outside_window": 0,
        "skipped_past": 0,
        "skipped_duplicate": 0,
    }
    candidates: list[RawCandidate] = []
    rejected: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for i, ev in enumerate(events):
        title = _val(ev, "SUMMARY")
        url = _val(ev, "URL")
        if not title:
            rejected.append({"index": i, "reason": "missing_title"})
            continue

        status = (_val(ev, "STATUS") or "").upper()
        if status == "CANCELLED":
            stats["skipped_cancelled"] += 1
            continue

        flags: list[str] = []
        start, all_day = (None, False)
        if "DTSTART" in ev:
            start, all_day = _parse_ics_datetime(ev["DTSTART"][0], ev["DTSTART"][1], tz)
        if start is None:
            flags.append("missing_start" if "DTSTART" not in ev else "unparseable_start")
        else:
            start_local_date = start if isinstance(start, date) and not isinstance(start, datetime) \
                else start.astimezone(tz).date()
            if not (window_start <= start_local_date <= window_end):
                stats["skipped_outside_window"] += 1
                continue
            if isinstance(start, datetime) and start < now:
                # Started already. Keep if still running, otherwise skip as past.
                end_chk = None
                if "DTEND" in ev:
                    end_chk, _ = _parse_ics_datetime(ev["DTEND"][0], ev["DTEND"][1], tz)
                if not (isinstance(end_chk, datetime) and end_chk > now):
                    stats["skipped_past"] += 1
                    continue
                flags.append("already_started")
        if all_day:
            flags.append("all_day_no_time")

        description = _val(ev, "DESCRIPTION")
        evidence, free_flags = find_free_evidence(f"{title}. {description or ''}")
        if not evidence:
            stats["skipped_not_free"] += 1
            continue
        flags.extend(free_flags)

        if not url:
            rejected.append({"index": i, "reason": "missing_source_url", "title": title})
            continue

        key = (_val(ev, "UID") or url, start.isoformat() if start else "")
        if key in seen:
            stats["skipped_duplicate"] += 1
            continue
        seen.add(key)

        location = _val(ev, "LOCATION")
        if not location:
            flags.append("missing_location")

        image = None
        if "ATTACH" in ev:
            params, v = ev["ATTACH"]
            if params.get("FMTTYPE", "").startswith("image/") or _IMG_RE.search(v):
                image = v
        if not image:
            flags.append("missing_image")

        end_iso = None
        if "DTEND" in ev:
            end_dt, _ = _parse_ics_datetime(ev["DTEND"][0], ev["DTEND"][1], tz)
            if end_dt is None:
                flags.append("unparseable_end")
            else:
                end_iso = end_dt.isoformat()
        if "RRULE" in ev:
            flags.append("recurring_rule_present")
        if url and not _host_allowed(url, college.allowed_domains):
            flags.append("off_allowlist")

        candidates.append(
            RawCandidate(
                college_id=college.id,
                title=title,
                source_url=url,
                description=description,
                location_text=location,
                start_datetime=start.isoformat() if start else None,
                end_datetime=end_iso,
                free_evidence=evidence,
                image_url=image,
                organizer=_val(ev, "ORGANIZER"),
                flags=flags,
            )
        )
    return candidates, rejected, stats


def fetch_feed(url: str, timeout: float = 30.0) -> str:
    """Download a feed. Identify ourselves honestly; set FEED_CONTACT to an email/URL."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Refusing non-http(s) feed URL: {url}")
    contact = os.environ.get("FEED_CONTACT", "set-FEED_CONTACT-env-var")
    req = urllib.request.Request(url, headers={"User-Agent": f"MerchDiscoveryBot/0.1 ({contact})"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def discover_from_ics(
    ics_text: str,
    college: CollegeConfig,
    window_start: date,
    window_end: date,
    source: str,
) -> tuple[DiscoveryResult, str]:
    """Build a DiscoveryResult (same type as Sonar runs). Returns (result, raw_ics_text)."""
    cands, rejected, stats = candidates_from_ics(ics_text, college, window_start, window_end)
    result = DiscoveryResult(
        college_id=college.id,
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        request={"source_type": "ics_feed", "source": source},
        candidates=cands,
        rejected=rejected,
        parse_errors=[] if stats["events_in_feed"] else ["no VEVENTs found in feed"],
        search_results=[],
        raw_response={"stats": stats},
    )
    return result, ics_text
