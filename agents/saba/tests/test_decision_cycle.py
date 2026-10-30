import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from market_data.decision_cycle import Thesis, evaluate, ExperienceJournal

TZ = ZoneInfo("Europe/Stockholm")


def thesis(hour=14, minute=0, side="LONG"):
    return Thesis("XAUUSD", datetime(2026, 10, 30, hour, minute, tzinfo=TZ),
                  side, "Trend hypothesis with conflicting evidence considered",
                  2600.0 if side != "ABSTAIN" else None, 20, 0.6, "archive:sample-001")


class DecisionCycleTests(unittest.TestCase):
    def test_research_candidate_not_order(self):
        result = evaluate(thesis())
        self.assertEqual(result["decision"], "RESEARCH_CANDIDATE")
        self.assertEqual(result["execution"], "DISABLED")

    def test_abstention_is_recorded(self):
        result = evaluate(thesis(side="ABSTAIN"))
        self.assertEqual(result["decision"], "ABSTAIN")
        with tempfile.TemporaryDirectory() as directory:
            journal = ExperienceJournal(Path(directory) / "events.sqlite")
            journal.record(result)
            self.assertEqual(journal.read()[0]["decision"], "ABSTAIN")

    def test_late_entry_rejected(self):
        self.assertEqual(evaluate(thesis(21, 29))["decision"], "ABSTAIN")
        self.assertEqual(evaluate(thesis(21, 45))["time_gate"]["decision"], "FLATTEN_NOW")

    def test_invalid_confidence_rejected(self):
        from dataclasses import replace
        with self.assertRaises(ValueError):
            evaluate(replace(thesis(), confidence=1.5))

    def test_duplicate_record_is_idempotent(self):
        result = evaluate(thesis())
        with tempfile.TemporaryDirectory() as directory:
            journal = ExperienceJournal(Path(directory) / "events.sqlite")
            self.assertEqual(journal.record(result), journal.record(result))
            self.assertEqual(len(journal.read()), 1)
