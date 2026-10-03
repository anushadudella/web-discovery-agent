"""Perplexity Sonar discovery client.

Role in the pipeline: DISCOVERY ONLY. Sonar proposes candidate freebies; it is
never trusted as the source of truth. Every candidate this module returns is
`needs_review=True` and carries flags describing what looks wrong. Downstream
steps (page verification, location ladder, dedup, confidence gate) decide
what is publishable.

Design rules:
  * Every raw request/response is saved so evals are reproducible.
  * Nothing is silently dropped except entries with no title or no source URL
    (those can't be verified at all); those go to `rejected` with a reason.
  * Domain allowlist is enforced both in the Sonar request AND on the
    returned source_url (the model can ignore the filter).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger(__name__)

MAX_DOMAINS = 20  # Sonar search_domain_filter limit
DATE_FMT = "%m/%d/%Y"  # Sonar date filter format


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class CollegeConfig:
    id: str
    name: str
    timezone: str
    campus_lat: float
    campus_lng: float
    allowed_domains: list[str]
    source_hints: list[str] = field(default_factory=list)
    model: str = "sonar"
    search_context_size: str = "medium"
    pub_lookback_days: int = 30
    feed_urls: list[str] = field(default_factory=list)  # ICS/RSS feeds (see feed_client.py)


def _clean_domain(d: str) -> str:
    d = d.strip()
    if d.startswith("-"):
        raise ValueError(f"Denylist entry {d!r} not allowed; allowlist mode only")
    return d


def load_college_config(path: str | Path) -> CollegeConfig:
    raw = yaml.safe_load(Path(path).read_text())
    domains = [_clean_domain(d) for d in raw.get("allowed_domains", [])]
    if not domains:
        raise ValueError(f"{path}: allowed_domains is empty")
    if len(domains) > MAX_DOMAINS:
        raise ValueError(f"{path}: {len(domains)} domains; Sonar max is {MAX_DOMAINS}")
    ZoneInfo(raw["timezone"])  # fail fast on a bad tz name
    return CollegeConfig(
        id=raw["id"],
        name=raw["name"],
        timezone=raw["timezone"],
        campus_lat=float(raw["campus_lat"]),
        campus_lng=float(raw["campus_lng"]),
        allowed_domains=domains,
        source_hints=raw.get("source_hints", []),
        model=raw.get("model", "sonar"),
        search_context_size=raw.get("search_context_size", "medium"),
        pub_lookback_days=int(raw.get("pub_lookback_days", 30)),
        feed_urls=raw.get("feed_urls", []) or [],
    )


# --------------------------------------------------------------------------- #
# Schema + prompt
# --------------------------------------------------------------------------- #
def _nullable(desc: str, typ: str = "string") -> dict:
    return {"anyOf": [{"type": typ}, {"type": "null"}], "description": desc}


EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "What the freebie/event is, as titled on the source page."},
                    "description": _nullable("One or two sentences from the source page."),
                    "location_text": _nullable(
                        "Venue/building/room EXACTLY as written on the source page. "
                        "null if the page does not state one. Never guess."
                    ),
                    "start_datetime": _nullable("Start, ISO 8601 with UTC offset, in the campus time zone."),
                    "end_datetime": _nullable("End, ISO 8601 with UTC offset. null if not stated."),
                    "free_evidence": _nullable(
                        "Short verbatim quote (<= 25 words) from the source page showing it is free "
                        "(e.g. 'free pizza', 'Free Event'). null if none."
                    ),
                    "source_url": {"type": "string", "description": "Exact URL of the page the details came from."},
                    "image_url": _nullable("Image URL from the source page, if one exists."),
                    "organizer": _nullable("Hosting org/department as stated on the page."),
                },
                "required": ["title", "source_url"],
            },
        }
    },
    "required": ["events"],
}

SYSTEM_PROMPT = (
    "You extract free campus events and giveaways for a student app. Rules:\n"
    "1. Use ONLY information found on pages from the allowed sources. Never invent events, URLs, "
    "times, or locations.\n"
    "2. Include an event only if the page itself indicates it is FREE (free food, free merchandise, "
    "giveaway, a 'Free Event' tag, etc.). Discounts, paid events, and events with one free item "
    "among paid ones do not count.\n"
    "3. Copy the location exactly as written on the page. If none is stated, use null. Do not infer "
    "a building from the event topic.\n"
    "4. If a field is not on the page, use null. Missing data is better than a guess.\n"
    "5. If you find nothing that qualifies, return {\"events\": []}.\n"
    "6. Return only JSON matching the provided schema."
)


def build_user_prompt(college: CollegeConfig, window_start: date, window_end: date) -> str:
    hints = ""
    if college.source_hints:
        hints = "Good places to look:\n" + "\n".join(f"- {h}" for h in college.source_hints) + "\n\n"
    return (
        f"Find free events and giveaways at {college.name} with a START date between "
        f"{window_start.isoformat()} and {window_end.isoformat()} (inclusive). "
        f"Campus time zone: {college.timezone}.\n\n"
        f"{hints}"
        "List each one separately, with the source page URL for each."
    )


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class RawCandidate:
    college_id: str
    title: str
    source_url: str
    description: str | None = None
    location_text: str | None = None
    start_datetime: str | None = None
    end_datetime: str | None = None
    free_evidence: str | None = None
    image_url: str | None = None
    organizer: str | None = None
    flags: list[str] = field(default_factory=list)
    needs_review: bool = True  # this client never asserts confidence


@dataclass
class DiscoveryResult:
    college_id: str
    run_id: str
    window_start: str
    window_end: str
    request: dict
    candidates: list[RawCandidate]
    rejected: list[dict]
    parse_errors: list[str]
    search_results: list[dict]
    raw_response: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def _extract_json(text: str) -> Any:
    text = _THINK_RE.sub("", text or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost {...}
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _host_allowed(url: str, allowed_domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    for d in allowed_domains:
        d_host = (urlparse(d if "//" in d else f"//{d}").hostname or "").lower()
        if d_host and (host == d_host or host.endswith("." + d_host)):
            return True
    return False


def _parse_dt(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def parse_candidates(
    content: str,
    college: CollegeConfig,
    window_start: date,
    window_end: date,
) -> tuple[list[RawCandidate], list[dict], list[str]]:
    """Turn model text into candidates + flags. Returns (candidates, rejected, errors)."""
    try:
        payload = _extract_json(content)
    except (json.JSONDecodeError, TypeError) as e:
        return [], [], [f"response was not valid JSON: {e}"]

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return [], [], ["JSON missing top-level 'events' list"]

    tz = ZoneInfo(college.timezone)
    candidates: list[RawCandidate] = []
    rejected: list[dict] = []

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            rejected.append({"index": i, "reason": "event_not_object", "event": ev})
            continue
        title = (ev.get("title") or "").strip()
        url = (ev.get("source_url") or "").strip()
        if not title:
            rejected.append({"index": i, "reason": "missing_title", "event": ev})
            continue
        if not url:
            rejected.append({"index": i, "reason": "missing_source_url", "event": ev})
            continue

        def s(key: str) -> str | None:
            v = ev.get(key)
            v = v.strip() if isinstance(v, str) else None
            return v or None

        c = RawCandidate(
            college_id=college.id,
            title=title,
            source_url=url,
            description=s("description"),
            location_text=s("location_text"),
            start_datetime=s("start_datetime"),
            end_datetime=s("end_datetime"),
            free_evidence=s("free_evidence"),
            image_url=s("image_url"),
            organizer=s("organizer"),
        )

        if not _host_allowed(url, college.allowed_domains):
            c.flags.append("off_allowlist")
        if not c.location_text:
            c.flags.append("missing_location")
        if not c.image_url:
            c.flags.append("missing_image")
        if not c.free_evidence:
            c.flags.append("no_free_evidence")

        start_dt = _parse_dt(c.start_datetime, tz)
        if c.start_datetime is None:
            c.flags.append("missing_start")
        elif start_dt is None:
            c.flags.append("unparseable_start")
        else:
            local_day = start_dt.astimezone(tz).date()
            if not (window_start <= local_day <= window_end):
                c.flags.append("outside_window")
            if start_dt < datetime.now(timezone.utc):
                c.flags.append("already_started_or_past")
        if c.end_datetime and _parse_dt(c.end_datetime, tz) is None:
            c.flags.append("unparseable_end")

        candidates.append(c)

    return candidates, rejected, []


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _to_dict(obj: Any) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    return json.loads(json.dumps(obj, default=lambda o: getattr(o, "__dict__", str(o))))


class SonarClient:
    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 90.0,  # first request with a new schema can take 10-30s
        max_retries: int = 3,
        client: Any = None,  # injectable for tests
    ):
        if client is None:
            from perplexity import Perplexity  # pip install perplexityai

            client = Perplexity(api_key=api_key or os.environ["PERPLEXITY_API_KEY"], timeout=timeout)
        self._client = client
        self.max_retries = max_retries

    # -- request ----------------------------------------------------------- #
    @staticmethod
    def build_request(college: CollegeConfig, window_start: date, window_end: date) -> dict:
        pub_after = window_start - timedelta(days=college.pub_lookback_days)
        return {
            "model": college.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(college, window_start, window_end)},
            ],
            "temperature": 0,
            "search_domain_filter": college.allowed_domains,
            # Filters on PUBLICATION date, so look back; the event window lives in the prompt.
            "search_after_date_filter": pub_after.strftime(DATE_FMT),
            "web_search_options": {"search_context_size": college.search_context_size},
            "response_format": {"type": "json_schema", "json_schema": {"schema": EVENT_SCHEMA}},
        }

    # -- call -------------------------------------------------------------- #
    def _call_with_retries(self, request: dict) -> Any:
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._client.chat.completions.create(**request)
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt == self.max_retries:
                    raise
                log.warning("Sonar call failed (attempt %d/%d): %s; retrying in %.0fs",
                            attempt, self.max_retries, e, delay)
                time.sleep(delay)
                delay *= 2

    def discover(self, college: CollegeConfig, window_start: date, window_end: date) -> DiscoveryResult:
        request = self.build_request(college, window_start, window_end)
        resp = self._call_with_retries(request)
        raw = _to_dict(resp)

        content = ""
        try:
            content = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            pass

        candidates, rejected, errors = parse_candidates(content, college, window_start, window_end)

        # Sources Sonar actually retrieved, useful for checking cited URLs later.
        search_results = raw.get("search_results") or []
        if not search_results and raw.get("citations"):
            search_results = [{"url": u} for u in raw["citations"]]

        return DiscoveryResult(
            college_id=college.id,
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            request=request,
            candidates=candidates,
            rejected=rejected,
            parse_errors=errors,
            search_results=search_results,
            raw_response=raw,
        )


def save_run(result: DiscoveryResult, out_dir: str | Path = "runs") -> Path:
    """Persist the full request/response so evals can replay it without re-calling the API."""
    path = Path(out_dir) / result.college_id / f"{result.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return path
