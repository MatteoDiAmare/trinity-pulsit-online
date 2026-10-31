import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pandas as pd
    from market_data import academy_bridge, academy_cli
    from market_data.archive import MarketArchive
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import load_specs_dir
    from market_data.academy.ledger import LedgerStore
    from market_data.academy.memory import ResearchMemory
    from market_data.academy.orchestrator import Budget, CycleDenied, Orchestrator, mutate_spec
    from market_data.academy.spec_strategy import validate_spec
    from tests.test_academy import random_walk
except ImportError:
    pd = None

SPEC_DIR = Path(__file__).resolve().parents[1] / "strategies"
EXPS = [{"strategy": "sma_cross", "params": {"short": 5, "long": 20}, "label": "sma_fast"},
        {"strategy": "breakout", "params": {"window": 15}, "label": "breakout15"}]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for sym, seed in (("GC=F", 1), ("EURUSD=X", 2)):
            d = self.root / sym / "1d"
            d.mkdir(parents=True)
            random_walk(700, seed=seed).assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")
        self.archive = MarketArchive(self.root)
        self.reg = ExperimentRegistry(self.root / "experiments.sqlite3")
        self.mem = ResearchMemory(self.root / "academy_memory.sqlite3")
        self.clock = Clock()

    def tearDown(self):
        self.mem.close()
        self.reg.close()
        self.tmp.cleanup()

    def orch(self, **budget):
        return Orchestrator(self.root, self.reg, self.archive, self.mem, LedgerStore(self.root), Budget(**budget) if budget else Budget(),
                            clock=self.clock)

    def hyp(self, budget=10, instruments=("GOLD", "EURUSD"), exps=EXPS, family="trend"):
        return self.mem.register_hypothesis("Trend rules work", family, budget, {"experiments": exps, "instruments": list(instruments), "interval": "1d"})


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class OrchestratorTests(Base):
    def test_default_budget_is_20_experiments_and_4_cycles(self):
        b = Budget()
        self.assertEqual((b.max_experiments_per_cycle, b.max_cycles_per_day), (20, 4))
        for bad in ({"max_experiments_per_cycle": 0}, {"max_cycles_per_day": 0}, {"max_seconds_per_cycle": 5}):
            with self.assertRaises(ValueError):
                Budget(**bad)
        b2 = Budget(10, 2, 600)
        b2.save(self.root / "academy_budget.json")
        self.assertEqual(Budget.load(self.root / "academy_budget.json"), b2)
        self.assertEqual(Orchestrator(self.root, self.reg, self.archive, self.mem).budget, b2)      # picked up from the volume

    def test_cycles_per_rolling_24h(self):
        o = self.orch(max_cycles_per_day=2)
        o.start_cycle()
        self.clock.advance(hours=6)
        o.start_cycle()
        self.clock.advance(hours=6)
        with self.assertRaises(CycleDenied) as cm:
            o.start_cycle()
        self.assertIn("2/2", str(cm.exception))
        self.clock.advance(hours=13)                       # first cycle is now > 24 h old
        self.assertEqual(o.start_cycle()["cycles_used_24h"], 2)

    def test_kill_switch_blocks_and_only_operator_resumes(self):
        o = self.orch()
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        with self.assertRaises(ValueError):
            o.halt("  ")
        o.halt("drawdown looks wrong")
        self.assertEqual(o.halted()["by"], "saba")
        with self.assertRaises(CycleDenied):
            o.start_cycle()
        with self.assertRaises(CycleDenied):
            o.run_hypothesis(cyc, hid)
        o.resume()
        self.assertIsNone(o.halted())
        self.assertTrue(o.start_cycle())
        (self.root / "academy_halt.json").write_text("{not json")
        self.assertTrue(o.halted()["halted"])                # unreadable halt file fails closed

    def test_run_hypothesis_executes_exactly_the_registered_plan(self):
        o = self.orch()
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        out = o.run_hypothesis(cyc, hid)
        self.assertEqual(out["new_experiments"], 4)
        self.assertEqual(set(out["report"]["matrix"]), {"sma_fast", "breakout15"})
        self.assertEqual(out["report"]["markets"], ["GOLD", "EURUSD"])
        h = self.mem.hypothesis(hid)
        self.assertEqual((h["status"], h["trials_used"]), ("testing", 4))
        self.assertEqual(o._cycle(cyc)["experiments_run"], 4)
        linked = {r[0] for r in self.mem.db.execute("SELECT experiment_id FROM hypothesis_experiments WHERE hypothesis_id=?", (hid,))}
        self.assertEqual(len(linked), 4)
        self.assertEqual({a["strategy"] for a in out["analysis"]}, {"sma_fast", "breakout15"})

    def test_rerun_reuses_results_and_costs_nothing(self):
        o = self.orch()
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        o.run_hypothesis(cyc, hid)
        out = o.run_hypothesis(cyc, hid)
        self.assertEqual(out["new_experiments"], 0)
        self.assertEqual(self.mem.hypothesis(hid)["trials_used"], 4)
        self.assertEqual(o._cycle(cyc)["experiments_run"], 4)

    def test_cycle_experiment_budget_enforced_before_running(self):
        o = self.orch(max_experiments_per_cycle=3)
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        before = self.reg.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        with self.assertRaises(CycleDenied) as cm:
            o.run_hypothesis(cyc, hid)
        self.assertIn("4 experiments", str(cm.exception))
        self.assertEqual(self.reg.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], before)    # nothing ran
        exact = self.orch(max_experiments_per_cycle=4, max_cycles_per_day=9)
        cyc2 = exact.start_cycle()["cycle_id"]
        self.assertEqual(exact.run_hypothesis(cyc2, hid)["new_experiments"], 4)

    def test_budget_is_shared_across_hypotheses_in_a_cycle(self):
        o = self.orch(max_experiments_per_cycle=6)
        cyc = o.start_cycle()["cycle_id"]
        a = self.hyp()
        b = self.hyp(exps=[{"strategy": "mean_reversion", "params": {"window": 25, "z_entry": 2.0}, "label": "mr"},
                           {"strategy": "sma_cross", "params": {"short": 8, "long": 40}, "label": "sma_slow"}])
        o.run_hypothesis(cyc, a)
        with self.assertRaises(CycleDenied):
            o.run_hypothesis(cyc, b)            # needs 4, only 2 left

    def test_hypothesis_trial_budget_enforced(self):
        o = self.orch()
        cyc = o.start_cycle()["cycle_id"]
        with self.assertRaises(ValueError) as cm:
            o.run_hypothesis(cyc, self.hyp(budget=2))
        self.assertIn("trial budget", str(cm.exception))

    def test_plan_cannot_change_after_preregistration(self):
        o = self.orch()
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        self.mem.db.execute("UPDATE hypotheses SET plan_json=? WHERE id=?", (json.dumps({"experiments": EXPS[:1], "instruments": ["GOLD"]}), hid))
        with self.assertRaises(ValueError) as cm:
            o.run_hypothesis(cyc, hid)
        self.assertIn("pre-registration", str(cm.exception))

    def test_closed_hypothesis_and_closed_cycle_cannot_run(self):
        o = self.orch()
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        self.mem.set_status(hid, "refuted")
        with self.assertRaises(ValueError):
            o.run_hypothesis(cyc, hid)
        o.finish_cycle(cyc)
        with self.assertRaises(CycleDenied):
            o.run_hypothesis(cyc, self.hyp())
        with self.assertRaises(ValueError):
            o.run_hypothesis(999, hid)

    def test_cycle_time_budget(self):
        o = self.orch(max_seconds_per_cycle=60)
        cyc = o.start_cycle()["cycle_id"]
        hid = self.hyp()
        self.clock.advance(seconds=61)
        with self.assertRaises(CycleDenied) as cm:
            o.run_hypothesis(cyc, hid)
        self.assertIn("time budget", str(cm.exception))

    def test_analysis_suggestions(self):
        cell = lambda ret, sharpe, trades, noise, eid: {"status": "ok", "experiment_id": eid, "exceeds_noise": noise,
                                                        "validation": {"net_return_pct": ret, "sharpe_annualized": sharpe, "trades": trades}}
        report = {"matrix": {
            "winner": {"per_market": {"GOLD": cell(5, 2.0, 12, True, 1), "EURUSD": cell(-1, -0.2, 9, False, 2)}},
            "maybe": {"per_market": {"GOLD": cell(3, 0.8, 10, False, 3)}},
            "loser": {"per_market": {"GOLD": cell(-4, -1, 15, False, 4)}},
            "thin": {"per_market": {"GOLD": cell(9, 3.0, 2, False, 5)}},
            "gated": {"per_market": {"GOLD": {"status": "rejected_by_causality_gate"}}}}}
        got = {a["strategy"]: a for a in Orchestrator.analyze(report)}
        self.assertEqual({k: v["suggestion"] for k, v in got.items()},
                         {"winner": "confirm", "maybe": "vary", "loser": "drop", "thin": "drop", "gated": "drop"})
        self.assertEqual(got["winner"]["best_market"], "GOLD")
        self.assertIn("ONE candidate", got["winner"]["reason"])

    def test_propose_next_uses_exploration_policy(self):
        o = self.orch()
        a = self.hyp(family="trend")
        b = self.hyp(family="reversion", budget=4)
        scores = {r["hypothesis_id"]: r for r in o.propose_next()}
        self.assertEqual(set(scores), {a, b})
        ranked = [r["hypothesis_id"] for r in o.propose_next()]
        self.assertEqual(ranked, sorted(ranked, key=lambda i: -scores[i]["score"]))
        self.assertTrue(scores[a]["fits_remaining_budget"])
        # a candidate lesson in a family raises that family's expected value
        eid = self.reg.record("academy:sma_cross", random_walk(100, seed=1), {"strategy": "sma_cross", "params": {}}, {"validation": {}})
        self.mem.add_lesson(self.reg, claim="reversion looks promising", verdict="candidate", confidence=0.4,
                            evidence=[{"experiment_id": eid}], scope={"market": "GOLD"}, tags="reversion")
        after = {r["hypothesis_id"]: r["score"] for r in o.propose_next()}
        self.assertGreater(after[b], scores[b]["score"])
        self.assertEqual(after[a], scores[a]["score"])

    def test_test_windows_report(self):
        self.assertEqual(self.orch().test_windows(), {"GOLD": None, "EURUSD": None, "SP500": None})


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class MutateSpecTests(unittest.TestCase):
    def setUp(self):
        self.spec = load_specs_dir(SPEC_DIR)["ema_trend"]

    def test_valid_deterministic_distinct(self):
        a, b = mutate_spec(self.spec, 6, seed=1), mutate_spec(self.spec, 6, seed=1)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 6)
        self.assertNotEqual(a, mutate_spec(self.spec, 6, seed=2))
        names = {v["name"] for v in a}
        self.assertEqual(len(names), 6)
        keyed = {json.dumps({**v, "name": ""}, sort_keys=True) for v in a}
        self.assertEqual(len(keyed), 6)
        for v in a:
            validate_spec(v)
            self.assertIsNotNone(v["risk"]["stop_loss_pct"])
            self.assertNotEqual(json.dumps({**v, "name": ""}, sort_keys=True), json.dumps({**self.spec, "name": ""}, sort_keys=True))
        self.assertEqual(self.spec["indicators"]["fast"]["window"], 12)       # original untouched

    def test_time_feature_literals_are_not_jittered(self):
        spec = load_specs_dir(SPEC_DIR)["channel_breakout_filtered"]
        for v in mutate_spec(spec, 5, seed=3):
            self.assertIn({"lt": ["weekday", 5]}, v["entry_long"]["all"])


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class BridgeTests(Base):
    def call(self, req, **kw):
        return academy_bridge.handle(req, root=self.root, strategies_dir=SPEC_DIR, **kw)

    def main(self, req):
        out, stdin = io.StringIO(), io.StringIO(json.dumps(req))
        with contextlib.redirect_stdout(out):
            import sys
            old, sys.stdin = sys.stdin, stdin
            try:
                code = academy_bridge.main(["--root", str(self.root), "--strategies-dir", str(SPEC_DIR)])
            finally:
                sys.stdin = old
        return code, json.loads(out.getvalue())

    def test_allowlist_and_input_validation(self):
        for bad in ({}, {"command": "resume"}, {"command": "rm -rf"}, {"command": "unhalt"}, [1]):
            with self.assertRaises(ValueError):
                self.call(bad)
        self.assertNotIn("resume", academy_bridge.COMMANDS)
        base = {"command": "register_hypothesis", "statement": "s", "family": "f", "trial_budget": 4, "instruments": ["GOLD"],
                "experiments": [{"label": "a", "strategy": "sma_cross", "params": {}}]}
        for patch in ({"trial_budget": "4"}, {"trial_budget": True}, {"instruments": ["DOGE"]}, {"instruments": []}, {"experiments": []},
                      {"experiments": [{"label": "a", "strategy": "os.system"}]},
                      {"experiments": [{"label": "a", "strategy": "sma_cross", "params": {"stop_loss_pct": None}}]},
                      {"experiments": [{"label": "a", "strategy": "sma_cross"}, {"label": "a", "strategy": "breakout"}]},
                      {"experiments": [{"strategy": "sma_cross"}]}, {"statement": ""}):
            with self.assertRaises(ValueError, msg=str(patch)):
                self.call({**base, **patch})

    def test_spec_file_cannot_escape_strategies_dir(self):
        for name in ("../experiments.json", "a/b.json", "/etc/passwd", "..", "x.json.exe", "", 5, "ema_trend.JSON", "nope.json"):
            with self.assertRaises(ValueError, msg=str(name)):
                self.call({"command": "variants", "spec_file": name, "n": 2})

    def test_end_to_end_research_cycle(self):
        st = self.call({"command": "status"})
        self.assertEqual(st["budget"]["max_experiments_per_cycle"], 20)
        self.assertEqual(sorted(st["instruments"]), ["EURUSD", "GOLD", "SP500"])
        reg = self.call({"command": "register_hypothesis", "statement": "Trend rules transfer between gold and EUR/USD", "family": "trend",
                         "trial_budget": 6, "instruments": ["GOLD", "EURUSD"], "interval": "1d",
                         "experiments": [{"label": "sma", "strategy": "sma_cross", "params": {"short": 6, "long": 25}},
                                         {"label": "ema_trend", "spec_file": "ema_trend.json"}]})
        hid = reg["hypothesis_id"]
        self.assertEqual(reg["cells"], 4)
        cyc = self.call({"command": "start_cycle"})["cycle_id"]
        run = self.call({"command": "run_hypothesis", "cycle_id": cyc, "hypothesis_id": hid})
        self.assertEqual(run["new_experiments"], 4)
        ids = [i for m in run["report"]["market_data"].values() for i in m["experiment_ids"]]
        lid = self.call({"command": "add_lesson", "hypothesis_id": hid, "claim": "SMA 6/25 shows no durable edge on gold or EUR/USD",
                         "verdict": "inconclusive", "confidence": 0.3, "evidence": [{"experiment_id": ids[0]}],
                         "scope": {"market": "GOLD", "interval": "1d"}, "tags": "trend sma"})["lesson_id"]
        found = self.call({"command": "search_lessons", "text": "edge gold"})["lessons"]
        self.assertEqual([x["id"] for x in found], [lid])
        self.assertTrue(self.call({"command": "verify_lesson", "lesson_id": lid})["ok"])
        nb = self.call({"command": "notebook", "write": True})
        self.assertIn(f"H{hid}", nb["notebook"])
        self.assertTrue((self.root / "research_notebook.md").is_file())
        variants = self.call({"command": "variants", "spec_file": "ema_trend.json", "n": 3, "seed": 5})["variants"]
        self.assertEqual(len(variants), 3)
        self.assertTrue(self.call({"command": "propose_next"})["ranking"])
        self.call({"command": "conclude_hypothesis", "hypothesis_id": hid, "status": "abandoned"})
        self.call({"command": "finish_cycle", "cycle_id": cyc})
        # the lesson can only cite real evidence
        with self.assertRaises(ValueError):
            self.call({"command": "add_lesson", "claim": "invented", "verdict": "candidate", "confidence": 0.9,
                       "evidence": [{"experiment_id": 424242}], "scope": {}})
        # self-halt stops further cycles; the bridge has no way to lift it
        self.call({"command": "halt", "reason": "testing the stop"})
        with self.assertRaises(CycleDenied):
            self.call({"command": "start_cycle"})

    def test_final_test_through_bridge_is_single_use(self):
        self.call({"command": "register_hypothesis", "statement": "s", "family": "f", "trial_budget": 4, "instruments": ["GOLD"],
                   "experiments": [{"label": "sma", "strategy": "sma_cross", "params": {"short": 5, "long": 20}}]})
        cyc = self.call({"command": "start_cycle"})["cycle_id"]
        run = self.call({"command": "run_hypothesis", "cycle_id": cyc, "hypothesis_id": 1})
        eid = run["report"]["market_data"]["GOLD"]["experiment_ids"][0]
        out = self.call({"command": "final_test", "experiment_id": eid})
        self.assertTrue(out["ok"])
        self.assertIn("final_test", out)
        with self.assertRaises(ValueError):
            self.call({"command": "final_test", "experiment_id": eid})
        with self.assertRaises(ValueError):
            self.call({"command": "final_test", "experiment_id": 99999})
        st = self.call({"command": "status"})
        self.assertIsNotNone(st["test_windows_consumed_until"]["GOLD"])

    def test_process_interface_exit_codes(self):
        code, out = self.main({"command": "status"})
        self.assertEqual((code, out["ok"]), (0, True))
        code, out = self.main({"command": "nope"})
        self.assertEqual((code, out["ok"], out["execution"]), (1, False, "DISABLED"))

    def test_operator_cli_can_halt_and_resume(self):
        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = academy_cli.main(["--root", str(self.root), *argv])
            return code, json.loads(buf.getvalue())
        code, out = run("halt", "--reason", "operator check")
        self.assertEqual(out["halted"]["by"], "operator")
        with self.assertRaises(CycleDenied):
            self.call({"command": "start_cycle"})
        code, out = run("resume")
        self.assertIsNone(out["halted"])
        self.assertTrue(self.call({"command": "start_cycle"})["cycle_id"])


if __name__ == "__main__":
    unittest.main()
