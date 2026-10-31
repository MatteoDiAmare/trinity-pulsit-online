import json
import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy.memory import ResearchMemory
    from memory.experience_store import ExperienceStore
    from tests.test_academy import random_walk
except ImportError:
    pd = None

PLAN = {"experiments": [{"strategy": "sma_cross", "params": {"short": 5, "long": 20}, "label": "sma"}],
        "instruments": ["GOLD"], "interval": "1d"}


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class ResearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reg = ExperimentRegistry(self.root / "r.db")
        self.mem = ResearchMemory(self.root / "m.sqlite3")
        self.bars = random_walk(300, seed=2)
        self.val_id = self.reg.record("academy:sma_cross", self.bars, {"strategy": "sma_cross", "params": {}},
                                      {"validation": {"net_return_pct": 4.0, "trades": 12}})
        self.neg_id = self.reg.record("academy:breakout", self.bars, {"strategy": "breakout", "params": {}},
                                      {"validation": {"net_return_pct": -6.0, "trades": 20}})

    def tearDown(self):
        self.mem.close()
        self.reg.close()
        self.tmp.cleanup()

    def final(self, **m):
        metrics = {"net_return_pct": 3.0, "trades": 9, "shortfall": 0.0, **m}
        return self.reg.record("academy:final_test", self.bars, {"experiment_id": self.val_id}, {"final_test": metrics})

    # ---- hypotheses ----
    def test_register_and_preregistration_hash(self):
        hid = self.mem.register_hypothesis("Trend following works on gold", "trend", 10, PLAN)
        h = self.mem.hypothesis(hid)
        self.assertEqual((h["status"], h["trials_remaining"], h["plan_intact"]), ("open", 10, True))
        self.db_tamper = self.mem.db.execute("UPDATE hypotheses SET plan_json=? WHERE id=?", (json.dumps({**PLAN, "instruments": ["EURUSD"]}), hid))
        self.assertFalse(self.mem.hypothesis(hid)["plan_intact"])
        self.assertIn("PLAN TAMPERED", self.mem.notebook())

    def test_hypothesis_validation(self):
        for kw in ({"statement": " "}, {"trial_budget": 0}, {"trial_budget": "x"}, {"plan": {}}, {"plan": {"experiments": [1]}}, {"parent_id": 99}):
            args = dict(statement="s", family="f", trial_budget=5, plan=PLAN)
            args.update(kw)
            with self.assertRaises(ValueError):
                self.mem.register_hypothesis(**args)

    def test_trial_budget_is_enforced(self):
        hid = self.mem.register_hypothesis("s", "f", 5, PLAN)
        self.mem.spend_trials(hid, 3)
        with self.assertRaises(ValueError):
            self.mem.spend_trials(hid, 3)
        self.mem.spend_trials(hid, 2)
        self.assertEqual(self.mem.hypothesis(hid)["trials_remaining"], 0)
        with self.assertRaises(ValueError):
            self.mem.spend_trials(999, 1)

    def test_status_and_children(self):
        a = self.mem.register_hypothesis("parent", "f", 5, PLAN)
        b = self.mem.register_hypothesis("child", "f", 5, PLAN, parent_id=a)
        self.mem.set_status(a, "refuted")
        self.assertEqual([h["id"] for h in self.mem.open_hypotheses()], [b])
        with self.assertRaises(ValueError):
            self.mem.set_status(a, "winning")

    # ---- lessons ----
    def lesson(self, **kw):
        args = dict(claim="SMA cross shows a validation edge on gold", verdict="candidate", confidence=0.4,
                    evidence=[{"experiment_id": self.val_id}], scope={"market": "GOLD", "interval": "1d"})
        args.update(kw)
        return self.mem.add_lesson(self.reg, **args)

    def test_lesson_requires_real_evidence(self):
        for ev in ([], [{"experiment_id": 9999}], [{"experiment_id": "1"}], [{}]):
            with self.assertRaises(ValueError):
                self.lesson(evidence=ev)
        other = self.reg.record("unrelated", self.bars, {}, {})
        with self.assertRaises(ValueError):
            self.lesson(evidence=[{"experiment_id": other}])         # only Academy records count as evidence

    def test_lesson_field_validation(self):
        for kw in ({"claim": " "}, {"verdict": "great"}, {"confidence": 1.5}, {"confidence": -0.1}, {"hypothesis_id": 404}):
            with self.assertRaises(ValueError):
                self.lesson(**kw)

    def test_supported_needs_a_positive_consumed_final_test(self):
        with self.assertRaises(ValueError):
            self.lesson(verdict="supported")                               # validation evidence only
        for bad in (self.final(net_return_pct=-1.0), self.final(trades=2), self.final(shortfall=5.0)):
            with self.assertRaises(ValueError):
                self.lesson(verdict="supported", evidence=[{"experiment_id": self.val_id}, {"experiment_id": bad}])
        good = self.final()
        lid = self.lesson(verdict="supported", confidence=0.7, evidence=[{"experiment_id": self.val_id}, {"experiment_id": good}])
        self.assertEqual(self.mem.lesson(lid)["verdict"], "supported")

    def test_refuted_and_candidate_from_validation_evidence(self):
        self.assertTrue(self.lesson(verdict="refuted", claim="Breakout loses on gold", evidence=[{"experiment_id": self.neg_id}]))
        self.assertTrue(self.lesson(verdict="candidate"))
        self.assertTrue(self.lesson(verdict="inconclusive", claim="Too few trades"))

    def test_evidence_tampering_is_detected(self):
        lid = self.lesson()
        self.assertTrue(self.mem.verify_lesson(self.reg, lid)["ok"])
        self.reg.db.execute("UPDATE experiments SET results_json=? WHERE id=?", (json.dumps({"validation": {"net_return_pct": 99.0}}), self.val_id))
        out = self.mem.verify_lesson(self.reg, lid)
        self.assertFalse(out["ok"])
        self.assertEqual(out["problems"][0]["problem"], "results changed")
        self.reg.db.execute("DELETE FROM experiments WHERE id=?", (self.val_id,))
        self.assertEqual(self.mem.verify_lesson(self.reg, lid)["problems"][0]["problem"], "missing")
        with self.assertRaises(ValueError):
            self.mem.verify_lesson(self.reg, 999)

    def test_search_filters_and_supersede(self):
        a = self.lesson(claim="Mean reversion works on EURUSD in quiet regimes", scope={"market": "EURUSD"}, tags="regime quiet")
        b = self.lesson(claim="Breakout fails on SP500", verdict="refuted", evidence=[{"experiment_id": self.neg_id}], scope={"market": "SP500"})
        self.assertEqual([x["id"] for x in self.mem.search("mean reversion")], [a])
        self.assertEqual([x["id"] for x in self.mem.search("quiet")], [a])
        self.assertEqual([x["id"] for x in self.mem.search(verdict="refuted")], [b])
        self.assertEqual([x["id"] for x in self.mem.search(market="SP500")], [b])
        self.assertEqual(self.mem.search("zzz-nothing"), [])
        self.assertEqual([x["id"] for x in self.mem.search("'; DROP TABLE lessons; --")], [])      # input is never SQL
        c = self.lesson(claim="Mean reversion on EURUSD fails out of sample", verdict="refuted", evidence=[{"experiment_id": self.neg_id}], scope={"market": "EURUSD"})
        self.mem.supersede(a, c)
        self.assertNotIn(a, [x["id"] for x in self.mem.search("mean reversion")])
        self.assertIn(a, [x["id"] for x in self.mem.search("mean reversion", include_superseded=True)])
        with self.assertRaises(ValueError):
            self.mem.supersede(a, a)
        with self.assertRaises(ValueError):
            self.mem.search(limit=0)

    def test_persistence_across_instances(self):
        hid = self.mem.register_hypothesis("persisted", "f", 4, PLAN)
        lid = self.lesson()
        self.mem.close()
        again = ResearchMemory(self.root / "m.sqlite3")
        self.assertEqual(again.hypothesis(hid)["statement"], "persisted")
        self.assertEqual(again.lesson(lid)["verdict"], "candidate")
        again.close()
        self.mem = ResearchMemory(self.root / "m.sqlite3")

    def test_notebook_digest(self):
        hid = self.mem.register_hypothesis("Gold trends persist", "trend", 8, PLAN)
        self.lesson(claim="SMA edge candidate on gold")
        self.lesson(claim="Breakout loses on gold", verdict="refuted", evidence=[{"experiment_id": self.neg_id}])
        nb = self.mem.notebook(status={"cycles_today": "1/4"})
        for text in ("cycles_today: 1/4", f"H{hid}", "trials left 8/8", "CANDIDATE", "Dead ends", "Breakout loses on gold"):
            self.assertIn(text, nb)
        for i in range(60):
            self.mem.register_hypothesis("x" * 200 + str(i), "f", 5, PLAN)
        short = self.mem.notebook(max_chars=1500)
        self.assertLessEqual(len(short), 1500)
        self.assertIn("truncated", short)
        p = self.mem.write_notebook(self.root / "nb" / "research_notebook.md")
        self.assertTrue(p.read_text().startswith("# Saba research notebook"))

    def test_events_mirrored_to_existing_experience_store(self):
        store = ExperienceStore(str(self.root / "exp.sqlite3"))
        mem = ResearchMemory(self.root / "m2.sqlite3", journal=store)
        mem.register_hypothesis("mirrored", "f", 3, PLAN)
        kinds = [e["kind"] for e in store.recent()]
        self.assertIn("academy.hypothesis", kinds)
        mem.close()
        store.close()


if __name__ == "__main__":
    unittest.main()
