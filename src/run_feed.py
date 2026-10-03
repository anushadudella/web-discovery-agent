"""Run the ICS feed reader for one college and save the raw run.

Examples:
  python -m src.run_feed --college ut-austin --file path/to/saved.ics     # offline, no network
  python -m src.run_feed --college ut-austin                              # uses feed_urls in config
  python -m src.run_feed --college ut-austin --feed-url https://.../x.ics --days 14
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .feed_client import discover_from_ics, fetch_feed
from .sonar_client import load_college_config, save_run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--college", required=True)
    ap.add_argument("--config-dir", default="config/colleges")
    ap.add_argument("--feed-url", help="overrides feed_urls in the college config")
    ap.add_argument("--file", help="read a local .ics file instead of downloading")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--out-dir", default="runs")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    college = load_college_config(Path(args.config_dir) / f"{args.college}.yaml")

    today = datetime.now(ZoneInfo(college.timezone)).date()
    start = date.fromisoformat(args.start) if args.start else today
    end = date.fromisoformat(args.end) if args.end else start + timedelta(days=args.days)

    sources: list[tuple[str, str]] = []  # (label, ics text)
    if args.file:
        sources.append((args.file, Path(args.file).read_text(errors="replace")))
    else:
        urls = [args.feed_url] if args.feed_url else college.feed_urls
        if not urls:
            raise SystemExit(
                "No feed URL. Add feed_urls to the college config, pass --feed-url, or use --file."
            )
        for u in urls:
            sources.append((u, fetch_feed(u)))

    for label, text in sources:
        result, raw_ics = discover_from_ics(text, college, start, end, source=label)
        path = save_run(result, args.out_dir)
        path.with_suffix(".ics").write_text(raw_ics)  # raw feed saved for reproducible evals

        stats = result.raw_response["stats"]
        print(f"\n{college.name} <- {label}")
        print(f"  {len(result.candidates)} candidates | feed had {stats['events_in_feed']} events | {stats}")
        print(f"  saved: {path}")
        for c in result.candidates:
            print(f"  - {c.title} | {c.start_datetime} | {c.location_text} | flags={c.flags}")


if __name__ == "__main__":
    main()
