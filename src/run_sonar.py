"""Run Sonar discovery for one college and save the raw run.

Examples:
  python -m src.run_sonar --college ut-austin --dry-run        # print request, no API call
  python -m src.run_sonar --college ut-austin --days 7         # real call (needs PERPLEXITY_API_KEY)
  python -m src.run_sonar --college ut-austin --start 2026-10-05 --end 2026-10-11
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .sonar_client import SonarClient, load_college_config, save_run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--college", required=True, help="config id, e.g. ut-austin")
    ap.add_argument("--config-dir", default="config/colleges")
    ap.add_argument("--days", type=int, default=7, help="window length starting today (campus tz)")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides --days)")
    ap.add_argument("--end", help="YYYY-MM-DD (overrides --days)")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--dry-run", action="store_true", help="print the request and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    college = load_college_config(Path(args.config_dir) / f"{args.college}.yaml")

    today = datetime.now(ZoneInfo(college.timezone)).date()
    start = date.fromisoformat(args.start) if args.start else today
    end = date.fromisoformat(args.end) if args.end else start + timedelta(days=args.days)

    if args.dry_run:
        print(json.dumps(SonarClient.build_request(college, start, end), indent=2))
        return

    result = SonarClient().discover(college, start, end)
    path = save_run(result, args.out_dir)

    flagged = sum(1 for c in result.candidates if c.flags)
    print(f"{college.name}: {len(result.candidates)} candidates "
          f"({flagged} with flags), {len(result.rejected)} rejected, "
          f"{len(result.parse_errors)} parse errors")
    print(f"Raw run saved to {path}")
    for c in result.candidates:
        print(f"- {c.title} | {c.start_datetime} | {c.location_text} | flags={c.flags}")


if __name__ == "__main__":
    main()
