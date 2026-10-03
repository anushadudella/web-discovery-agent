import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.sonar_client import (
    CollegeConfig,
    SonarClient,
    load_college_config,
    parse_candidates,
    save_run,
)

COLLEGE = CollegeConfig(
    id="ut-austin",
    name="The University of Texas at Austin",
    timezone="America/Chicago",
    campus_lat=30.2849,
    campus_lng=-97.7341,
    allowed_domains=["calendar.utexas.edu", "utexas.edu"],
)
START, END = date(2099, 1, 5), date(2099, 1, 12)


def ev(**kw):
    base = {
        "title": "Free Pizza Night",
        "source_url": "https://calendar.utexas.edu/event/pizza",
        "start_datetime": "2099-01-07T18:00:00-06:00",
        "location_text": "SSB 3.406",
        "free_evidence": "Free pizza",
        "image_url": "https://calendar.utexas.edu/img.jpg",
    }
    base.update(kw)
    return base


class FakeClient:
    """Mimics client.chat.completions.create(...)."""

    def __init__(self, content, fail_times=0):
        self.content, self.fail_times, self.calls = content, fail_times, 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("boom")
        return {
            "choices": [{"message": {"content": self.content}}],
            "search_results": [{"url": "https://calendar.utexas.edu/event/pizza"}],
        }


class ParseTests(unittest.TestCase):
    def run_parse(self, events, wrap=lambda s: s):
        return parse_candidates(wrap(json.dumps({"events": events})), COLLEGE, START, END)

    def test_clean_event_has_no_flags(self):
        cands, rej, err = self.run_parse([ev()])
        self.assertEqual(err, [])
        self.assertEqual(rej, [])
        self.assertEqual(cands[0].flags, [])

    def test_always_needs_review(self):
        cands, _, _ = self.run_parse([ev()])
        self.assertTrue(cands[0].needs_review)

    def test_fenced_and_think_wrapped_json(self):
        wrap = lambda s: f"<think>hmm</think>\n```json\n{s}\n```"
        cands, _, err = self.run_parse([ev()], wrap)
        self.assertEqual(err, [])
        self.assertEqual(len(cands), 1)

    def test_off_allowlist_flagged_not_dropped(self):
        cands, _, _ = self.run_parse([ev(source_url="https://evil-utexas.edu.example.com/x")])
        self.assertIn("off_allowlist", cands[0].flags)

    def test_subdomain_is_allowed(self):
        cands, _, _ = self.run_parse([ev(source_url="https://recsports.utexas.edu/x")])
        self.assertNotIn("off_allowlist", cands[0].flags)

    def test_missing_fields_flagged(self):
        cands, _, _ = self.run_parse(
            [ev(location_text=None, image_url=None, free_evidence=None)]
        )
        self.assertEqual(
            set(cands[0].flags), {"missing_location", "missing_image", "no_free_evidence"}
        )

    def test_unparseable_and_missing_start(self):
        cands, _, _ = self.run_parse([ev(start_datetime="next tuesday"), ev(start_datetime=None)])
        self.assertIn("unparseable_start", cands[0].flags)
        self.assertIn("missing_start", cands[1].flags)

    def test_outside_window_and_past(self):
        cands, _, _ = self.run_parse([ev(start_datetime="2020-01-07T18:00:00-06:00")])
        self.assertIn("outside_window", cands[0].flags)
        self.assertIn("already_started_or_past", cands[0].flags)

    def test_no_title_or_url_rejected_with_reason(self):
        cands, rej, _ = self.run_parse([ev(title=""), ev(source_url="")])
        self.assertEqual(cands, [])
        self.assertEqual({r["reason"] for r in rej}, {"missing_title", "missing_source_url"})

    def test_garbage_response_is_an_error_not_a_crash(self):
        cands, rej, err = parse_candidates("I couldn't find anything!", COLLEGE, START, END)
        self.assertEqual((cands, rej), ([], []))
        self.assertTrue(err)

    def test_empty_list_is_valid(self):
        cands, rej, err = self.run_parse([])
        self.assertEqual((cands, rej, err), ([], [], []))


class RequestTests(unittest.TestCase):
    def test_request_shape(self):
        req = SonarClient.build_request(COLLEGE, START, END)
        self.assertEqual(req["search_domain_filter"], COLLEGE.allowed_domains)
        self.assertEqual(req["search_after_date_filter"], "12/06/2098")  # 30-day lookback
        self.assertEqual(req["response_format"]["type"], "json_schema")
        self.assertEqual(req["temperature"], 0)

    def test_ut_config_loads_and_is_valid(self):
        cfg = load_college_config(Path("config/colleges/ut-austin.yaml"))
        self.assertLessEqual(len(cfg.allowed_domains), 20)
        self.assertEqual(cfg.timezone, "America/Chicago")

    def test_denylist_entries_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.yaml"
            p.write_text(
                "id: x\nname: X\ntimezone: America/Chicago\ncampus_lat: 1\ncampus_lng: 1\n"
                "allowed_domains: ['-reddit.com']\n"
            )
            with self.assertRaises(ValueError):
                load_college_config(p)


class DiscoverTests(unittest.TestCase):
    def test_discover_and_save_roundtrip(self):
        fake = FakeClient(json.dumps({"events": [ev()]}))
        res = SonarClient(client=fake).discover(COLLEGE, START, END)
        self.assertEqual(len(res.candidates), 1)
        with tempfile.TemporaryDirectory() as d:
            path = save_run(res, d)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["candidates"][0]["title"], "Free Pizza Night")
            self.assertIn("request", saved)
            self.assertIn("raw_response", saved)

    def test_retries_then_succeeds(self):
        import src.sonar_client as sc

        sc.time.sleep = lambda s: None  # don't actually wait
        fake = FakeClient(json.dumps({"events": []}), fail_times=2)
        SonarClient(client=fake, max_retries=3).discover(COLLEGE, START, END)
        self.assertEqual(fake.calls, 3)

    def test_gives_up_after_max_retries(self):
        import src.sonar_client as sc

        sc.time.sleep = lambda s: None
        fake = FakeClient("{}", fail_times=99)
        with self.assertRaises(TimeoutError):
            SonarClient(client=fake, max_retries=2).discover(COLLEGE, START, END)


if __name__ == "__main__":
    unittest.main()
