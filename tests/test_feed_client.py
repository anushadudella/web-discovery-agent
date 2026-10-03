import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from src.feed_client import candidates_from_ics, find_free_evidence, parse_ics
from src.sonar_client import CollegeConfig

# NOTE: the fixture is SYNTHETIC and only exercises parser edge cases.
# Accuracy evals must use real saved feeds (see runs/<college>/*.ics).
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_localist.ics"

COLLEGE = CollegeConfig(
    id="ut-austin",
    name="The University of Texas at Austin",
    timezone="America/Chicago",
    campus_lat=30.2849,
    campus_lng=-97.7341,
    allowed_domains=["calendar.utexas.edu", "utexas.edu"],
)
START, END = date(2099, 1, 5), date(2099, 1, 12)
NOW = datetime(2098, 12, 1, tzinfo=timezone.utc)


class FeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text()
        cls.cands, cls.rejected, cls.stats = candidates_from_ics(cls.text, COLLEGE, START, END, now=NOW)
        cls.by_uid_title = {c.title: c for c in cls.cands}

    def test_parses_all_events_and_ignores_alarm_description(self):
        events = parse_ics(self.text)
        self.assertEqual(len(events), 8)
        lunch = [e for e in events if e["UID"][1] == "evt-7@test"][0]
        self.assertTrue(lunch["DESCRIPTION"][1].startswith("Free lunch"))  # not 'Reminder'

    def test_stats_account_for_every_event(self):
        s = self.stats
        self.assertEqual(s["events_in_feed"], 8)
        self.assertEqual(s["skipped_cancelled"], 1)
        self.assertEqual(s["skipped_not_free"], 2)  # gluten-free + free speech
        self.assertEqual(s["skipped_outside_window"], 1)
        self.assertEqual(s["skipped_duplicate"], 1)
        self.assertEqual(len(self.cands), 3)
        self.assertEqual(self.rejected, [])

    def test_clean_event_fields_and_timezone(self):
        c = self.by_uid_title["Free Pizza Night, Welcome Back"]  # escaped comma unescaped
        self.assertEqual(c.start_datetime, "2099-01-06T18:00:00-06:00")
        self.assertEqual(c.end_datetime, "2099-01-06T20:00:00-06:00")
        self.assertEqual(c.location_text, "Student Services Building (SSB) 3.406")
        self.assertEqual(c.image_url, "https://calendar.utexas.edu/img/pizza.jpg")
        self.assertIn("free pizza", c.free_evidence.lower())
        self.assertEqual(c.flags, [])
        self.assertTrue(c.needs_review)

    def test_all_day_recurring_missing_fields_flagged(self):
        c = self.by_uid_title["Free T-Shirt Giveaway"]
        self.assertEqual(c.start_datetime, "2099-01-07")
        self.assertEqual(
            set(c.flags),
            {"all_day_no_time", "missing_location", "missing_image", "recurring_rule_present"},
        )
        self.assertIn("free shirt", c.description)  # line folding handled

    def test_conditional_and_off_allowlist_flagged(self):
        c = self.by_uid_title["Free Lunch (sponsored)"]
        self.assertIn("possible_paid_or_conditional", c.flags)
        self.assertIn("off_allowlist", c.flags)
        self.assertEqual(c.start_datetime, "2099-01-08T17:00:00+00:00")

    def test_past_event_skipped_unless_still_running(self):
        later = datetime(2099, 1, 7, tzinfo=timezone.utc)
        _, _, stats = candidates_from_ics(self.text, COLLEGE, START, END, now=later)
        self.assertGreaterEqual(stats["skipped_past"], 1)


class FreeHeuristicTests(unittest.TestCase):
    def test_true_positives(self):
        for t in ["Free pizza in the Union", "Giveaway at the Plaza", "Admission is free.", "No cost to attend"]:
            ev, _ = find_free_evidence(t)
            self.assertIsNotNone(ev, t)

    def test_false_positives(self):
        for t in ["Gluten-free options", "Freedom of speech", "Free speech panel", "Sugar-free drinks",
                  "This event is not free", "Free-for-all debate"]:
            ev, _ = find_free_evidence(t)
            self.assertIsNone(ev, t)

    def test_evidence_is_short(self):
        ev, _ = find_free_evidence("word " * 50 + "free pizza " + "word " * 50)
        self.assertLessEqual(len(ev.split()), 25)

    def test_price_flags_possible_paid(self):
        _, flags = find_free_evidence("Free entry for students, $15 for guests")
        self.assertIn("possible_paid_or_conditional", flags)


if __name__ == "__main__":
    unittest.main()
