"""no_evidence vs refuted, lesson correction, study/hypothesis linkage without double counting, candidate vs calibration,
the durable audit log and data refresh."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import pandas as pd
    from market_data import academy_bridge
    from market_data.archive import MarketArchive
    from market_data.academy.audit import AuditLog, GENESIS
    from market_data.academy.forward import ForwardTracker
    from market_data.academy.memory import ResearchMemory
    from market_data.academy.orchestrator import Orchestrator
    from market_data.laboratory import ExperimentRegistry
    from tests.test_academy_orchestrator import Base, SPEC_DIR
except ImportError:
    pd = None

STUDY = {"name": "gov", "strategy": "sma_cross", "base_params": {"stop_loss_pct": 2.0}, "grid": {"short": [5, 10], "long": [40, 80]},
         "instruments": ["GOLD", "EURUSD"], "interval": "1d", "train_bars": 250, "test_bars": 100, "respect_usable_from": False}
FORECAST = {"p_positive": 0.3, "expected_hit_rate": 0.35, "expected_expectancy": 0.5}


def tiny():
    return pd.DataFrame({"Open": [1.0, 1.1], "High": [1.2, 1.2], "Low": [0.9, 1.0], "Close": [1.1, 1.15], "Volume": [1.0, 1.0]},
                        index=pd.bdate_range("2020-01-01", periods=2, tz="UTC"))


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class Gov(Base):
    def record(self, name, result, cfg=None):
        return self.reg.record(name, tiny(), cfg or {"k": name}, result)

    def call(self, req, **kw):
        return academy_bridge.handle(req, root=self.root, strategies_dir=SPEC_DIR, **kw)

    def lesson(self, verdict, rid, **kw):
        return self.mem.add_lesson(self.reg, claim="c", verdict=verdict, confidence=0.5, evidence=[{"experiment_id": rid}], scope={"interval": "1d"}, **kw)


class NoEvidenceTests(Gov):
    def test_no_evidence_study_cannot_be_refuted_but_can_be_inconclusive(self):
        sid = self.record("academy:study", {"verdict": "no_evidence"})
        with self.assertRaisesRegex(ValueError, "absence of evidence"):
            self.lesson("refuted", sid)
        self.assertEqual(self.mem.db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0], 0)
        self.assertGreater(self.lesson("inconclusive", sid), 0)

    def test_negative_non_study_evidence_can_still_refute_and_candidate_study_is_untouched(self):
        val = self.record("academy:validation", {"net_return_pct": -3.0})
        self.assertGreater(self.lesson("refuted", val), 0)
        cand = self.record("academy:study", {"verdict": "candidate"})
        self.assertGreater(self.lesson("candidate", cand), 0)
        mixed = self.mem.add_lesson(self.reg, claim="m", verdict="refuted", confidence=0.5, scope={},
                                    evidence=[{"experiment_id": self.record("academy:study", {"verdict": "no_evidence"}, {"k": 2})}, {"experiment_id": val}])
        self.assertGreater(mixed, 0)

    def test_correction_keeps_the_original_and_its_traceability(self):
        sid = self.record("academy:study", {"verdict": "no_evidence"})
        # the legacy wrong lesson, written the way it was before the rule existed
        with mock.patch.object(ResearchMemory, "_refuse_refuted_from_no_evidence", staticmethod(lambda *a: None)):
            old = self.lesson("refuted", sid)
        before = dict(self.mem.db.execute("SELECT * FROM lessons WHERE id=?", (old,)).fetchone())
        new = self.mem.correct_lesson(self.reg, old, verdict="inconclusive", reason="no_evidence is not refuted", confidence=0.6)
        after_old = dict(self.mem.db.execute("SELECT * FROM lessons WHERE id=?", (old,)).fetchone())
        self.assertEqual({k: v for k, v in after_old.items() if k != "superseded_by"}, {k: v for k, v in before.items() if k != "superseded_by"})
        self.assertEqual(after_old["superseded_by"], new)
        a, b = self.mem.lesson(old), self.mem.lesson(new)
        self.assertEqual(a["evidence"], b["evidence"])                      # identical experiment ids and result hashes
        self.assertEqual(b["verdict"], "inconclusive")
        self.assertIn(f"Correction of L{old}", b["claim"])
        self.assertIn(f"corrects:L{old}", b["tags"])
        self.assertTrue(self.mem.verify_lesson(self.reg, old)["ok"] and self.mem.verify_lesson(self.reg, new)["ok"])
        self.assertEqual([x["id"] for x in self.mem.search()], [new])
        self.assertEqual({x["id"] for x in self.mem.search(include_superseded=True)}, {old, new})
        self.assertNotIn(f"L{old} ", self.mem.notebook())                    # no longer listed as a dead end
        with self.assertRaisesRegex(ValueError, "already superseded"):
            self.mem.correct_lesson(self.reg, old, verdict="inconclusive", reason="again")
        with self.assertRaisesRegex(ValueError, "reason"):
            self.mem.correct_lesson(self.reg, new, verdict="candidate", reason=" ")

    def test_correction_refuses_when_evidence_changed(self):
        sid = self.record("academy:study", {"verdict": "no_evidence"})
        with mock.patch.object(ResearchMemory, "_refuse_refuted_from_no_evidence", staticmethod(lambda *a: None)):
            old = self.lesson("refuted", sid)
        self.reg.db.execute("UPDATE experiments SET results_json=? WHERE id=?", (json.dumps({"verdict": "candidate"}), sid))
        self.reg.db.commit()
        with self.assertRaisesRegex(ValueError, "no longer verifies"):
            self.mem.correct_lesson(self.reg, old, verdict="inconclusive", reason="x")


class StudyLinkTests(Gov):
    def start(self):
        return self.call({"command": "start_cycle"})["cycle_id"]

    def counts(self):
        db = self.reg.db
        return {"registry": db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0],
                "spec_index": db.execute("SELECT COUNT(*) FROM academy_spec_index").fetchone()[0],
                "links": self.mem.db.execute("SELECT COUNT(*) FROM hypothesis_experiments").fetchone()[0],
                "trials_used": [r[0] for r in self.mem.db.execute("SELECT trials_used FROM hypotheses ORDER BY id")]}

    def test_study_only_hypothesis_and_no_double_counting(self):
        h = self.call({"command": "register_hypothesis", "statement": "sma", "family": "trend", "trial_budget": 4, "instruments": ["GOLD", "EURUSD"],
                       "experiments": [], "study_only": True})
        self.assertTrue(h["study_only"] and h["cells"] == 0)
        hid = h["hypothesis_id"]
        cyc_id = self.start()
        with self.assertRaisesRegex(ValueError, "evidenced by studies"):
            self.call({"command": "run_hypothesis", "cycle_id": cyc_id, "hypothesis_id": hid})
        out = self.call({"command": "run_study", "cycle_id": cyc_id, "study": STUDY, "hypothesis_id": hid})
        self.assertFalse(out["reused"])
        self.assertEqual(out["hypothesis_id"], hid)
        c1 = self.counts()
        self.assertEqual(c1["spec_index"], 4 * 2)                            # 4 cells x 2 markets, counted once, globally
        self.assertEqual(c1["trials_used"], [0])                             # the hypothesis' own trial budget is not charged
        self.assertEqual(c1["links"], 1)
        spent = self.mem.db.execute("SELECT experiments_run FROM cycles WHERE id=?", (cyc_id,)).fetchone()[0]
        self.assertEqual(spent, 1)
        # the same study again: reused from the registry, nothing recorded, charged or linked twice
        again = self.call({"command": "run_study", "cycle_id": cyc_id, "study": STUDY, "hypothesis_id": hid})
        self.assertTrue(again["reused"])
        self.assertEqual(again["registry_id"], out["registry_id"])
        self.assertEqual(self.counts(), c1)
        self.assertEqual(self.mem.db.execute("SELECT experiments_run FROM cycles WHERE id=?", (cyc_id,)).fetchone()[0], 1)
        # citing the same study in a lesson does not link it a second time either
        self.call({"command": "add_lesson", "hypothesis_id": hid, "claim": "no edge found", "verdict": "inconclusive", "confidence": 0.5,
                   "evidence": [{"experiment_id": out["registry_id"]}], "scope": {"interval": "1d"}})
        self.assertEqual(self.counts()["links"], 1)
        self.assertIn("inconclusive", out["how_to_record"])
        with self.assertRaisesRegex(ValueError, "absence of evidence"):
            self.call({"command": "add_lesson", "hypothesis_id": hid, "claim": "dead", "verdict": "refuted", "confidence": 0.9,
                       "evidence": [{"experiment_id": out["registry_id"]}], "scope": {}})

    def test_study_validation_errors_charge_nothing(self):
        cyc_id = self.start()
        bad = {**STUDY, "instruments": ["GOLD", "SP500"]}                      # SP500 has no archived data here
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.call({"command": "run_study", "cycle_id": cyc_id, "study": bad})
        self.assertEqual(self.mem.db.execute("SELECT experiments_run FROM cycles WHERE id=?", (cyc_id,)).fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "unknown hypothesis_id"):
            self.call({"command": "run_study", "cycle_id": cyc_id, "study": STUDY, "hypothesis_id": 99})

    def test_instruments_without_standard_data_are_refused_for_run_hypothesis(self):
        with self.assertRaisesRegex(ValueError, "no archived"):
            self.call({"command": "register_hypothesis", "statement": "s", "family": "f", "trial_budget": 2, "instruments": ["NASDAQ100"],
                       "experiments": [{"label": "a", "strategy": "sma_cross", "params": {"short": 5, "long": 20, "stop_loss_pct": 2.0}}]})

    def test_extended_archive_is_used_by_studies_only(self):
        ext = Path(self.tmp.name + "_ext")
        for sym in ("GC=F", "EURUSD=X"):
            d = ext / sym / "1d"
            d.mkdir(parents=True)
            pd.read_parquet(self.root / sym / "1d" / "bars.parquet").to_parquet(d / "bars.parquet")
        cyc_id = self.start()
        out = self.call({"command": "run_study", "cycle_id": cyc_id, "study": STUDY}, extended_root=ext)
        self.assertEqual(out["data_archive"], "extended")
        self.assertEqual(self.call({"command": "status"}, extended_root=ext)["study_data"], "extended (long history)")
        self.assertEqual(self.call({"command": "status"}, extended_root=Path(self.tmp.name + "_none"))["study_data"], "standard")


class IntradayBridgeTests(Gov):
    def start(self):
        return self.call({"command": "start_cycle"})["cycle_id"]

    def test_intraday_study_uses_only_the_intraday_archive_and_is_refused_without_it(self):
        import numpy as np
        from market_data.academy.sessions import expected_index
        idx = expected_index("24x7", "2023-01-01", "2023-11-30", "60m")
        r = np.random.default_rng(1).normal(0, 0.003, len(idx))
        close = 100 * np.cumprod(1 + r)
        op = np.concatenate([[100.0], close[:-1]])
        df = pd.DataFrame({"Open": op, "High": np.maximum(op, close) * 1.002, "Low": np.minimum(op, close) * 0.998, "Close": close, "Volume": 10.0,
                           "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
        intra = Path(self.tmp.name + "_intra")
        d = intra / "BTCUSD-BITSTAMP" / "60m"
        d.mkdir(parents=True)
        df.to_parquet(d / "bars.parquet")
        study = {"name": "hourly", "strategy": "sma_cross", "base_params": {"stop_loss_pct": 1.5}, "grid": {"short": [6, 12], "long": [24, 48]},
                 "instruments": ["BTCUSD_BITSTAMP"], "interval": "60m", "fold_mode": "days", "train_days": 40, "test_days": 10, "respect_usable_from": False}
        cyc = self.start()
        with self.assertRaisesRegex(ValueError, "no intraday archive"):
            self.call({"command": "run_study", "cycle_id": cyc, "study": study}, intraday_root=Path(self.tmp.name + "_missing"))
        self.assertEqual(self.mem.db.execute("SELECT experiments_run FROM cycles WHERE id=?", (cyc,)).fetchone()[0], 0)       # nothing charged
        self.assertEqual(self.call({"command": "status"}, intraday_root=intra)["intraday_data"], "installed")
        out = self.call({"command": "run_study", "cycle_id": cyc, "study": study}, intraday_root=intra)
        self.assertEqual(out["data_archive"], "intraday")
        self.assertFalse(out["intraday"]["allow_overnight"])
        self.assertEqual(out["wfo"]["trades_held_overnight"], 0)
        # a daily study never reads the intraday archive; the daily registry keys (market + interval) stay separate
        daily = {**study, "interval": "1d", "fold_mode": "dates", "train_days": None, "test_days": None, "train_bars": 250, "test_bars": 100, "instruments": ["GOLD", "EURUSD"],
                 "name": "daily"}
        self.assertEqual(self.call({"command": "run_study", "cycle_id": cyc, "study": daily}, intraday_root=intra)["data_archive"], "standard")
        with self.assertRaisesRegex(ValueError, "not implemented yet"):
            self.call({"command": "freeze_candidate", "label": "x", "strategy": "sma_cross", "params": {"short": 6, "long": 24, "stop_loss_pct": 1.5},
                       "instruments": ["BTCUSD_BITSTAMP"], "interval": "60m", "forecast": FORECAST})


class CandidateVsCalibrationTests(Gov):
    PLAN = {"command": "freeze_candidate", "label": "sma", "strategy": "sma_cross", "params": {"short": 5, "long": 40, "stop_loss_pct": 2.0},
            "instruments": ["GOLD"], "interval": "1d", "forecast": FORECAST, "rules": {"min_trades": 10, "min_days": 20}}

    def study_row(self, verdict, **spec):
        sp = {"strategy": "sma_cross", "base_params": {"stop_loss_pct": 2.0}, "grid": {"short": [5, 10], "long": [40, 80]},
              "instruments": ["GOLD", "EURUSD"], **spec}
        return self.record("academy:study", {"verdict": verdict, "study_sha256": "s" * 64}, {"spec": sp, "k": verdict + json.dumps(spec, sort_keys=True)})

    def test_default_is_calibration_and_never_candidate(self):
        out = self.call(self.PLAN)
        self.assertEqual(out["purpose"], "calibration")
        self.assertIn("CALIBRATION", out["meaning"])
        with self.assertRaisesRegex(ValueError, "needs study_registry_id"):
            self.call({**self.PLAN, "label": "x", "purpose": "candidate"})

    def test_no_evidence_study_gives_calibration_even_when_linked(self):
        rid = self.study_row("no_evidence")
        out = self.call({**self.PLAN, "study_registry_id": rid})
        self.assertEqual(out["purpose"], "calibration")
        with self.assertRaisesRegex(ValueError, "not 'candidate'"):
            self.call({**self.PLAN, "label": "y", "params": {"short": 10, "long": 40, "stop_loss_pct": 2.0}, "study_registry_id": rid, "purpose": "candidate"})
        with self.assertRaisesRegex(ValueError, "not a study"):
            self.call({**self.PLAN, "label": "z", "study_registry_id": self.record("academy:validation", {})})

    def test_candidate_needs_a_candidate_study_and_one_of_its_cells(self):
        rid = self.study_row("candidate")
        out = self.call({**self.PLAN, "study_registry_id": rid})
        self.assertEqual(out["purpose"], "candidate")
        for patch, msg in (({"params": {"short": 7, "long": 40, "stop_loss_pct": 2.0}}, "not one of the study's cells"),
                           ({"params": {"short": 5, "long": 40, "stop_loss_pct": 3.0}}, "not one of the study's cells"),
                           ({"strategy": "breakout", "params": {"window": 10, "stop_loss_pct": 2.0}}, "not the study's"),
                           ({"instruments": ["SP500"]}, "subset")):
            with self.assertRaisesRegex(ValueError, msg):
                self.call({**self.PLAN, "label": "n" + msg, "study_registry_id": rid, **patch})

    def test_report_separates_candidates_from_calibration(self):
        self.call({**self.PLAN, "study_registry_id": self.study_row("candidate")})
        self.call({**self.PLAN, "label": "cal", "params": {"short": 10, "long": 80, "stop_loss_pct": 2.0}})
        rep = self.call({"command": "forward_report", "update": True, "fetch": False})["forecast_report"]
        self.assertEqual((rep["qualified_candidates"], rep["calibration_only"]), (1, 1))
        by = {c["label"]: c for c in rep["candidates"]}
        self.assertTrue(by["sma"]["counts_as_candidate"])
        self.assertFalse(by["cal"]["counts_as_candidate"])
        self.assertIn("never candidates", rep["note"])

    def test_legacy_frozen_rows_become_calibration_with_plan_untouched(self):
        path = self.root / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE frozen (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, label TEXT NOT NULL, plan_json TEXT NOT NULL,
          plan_sha256 TEXT NOT NULL UNIQUE, cutoffs_json TEXT NOT NULL, forecast_json TEXT NOT NULL, rules_json TEXT NOT NULL,
          study_sha256 TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'active', code_version TEXT NOT NULL)""")
        db.execute("INSERT INTO frozen(created_at,label,plan_json,plan_sha256,cutoffs_json,forecast_json,rules_json,study_sha256,note,code_version) "
                   "VALUES ('2026-11-02','legacy','{\"p\":1}','abc','{}','{\"p_positive\":0.3}','{}',NULL,'','v')")
        db.commit()
        db.close()
        tr = ForwardTracker(path)
        try:
            row = tr.get(1)
            self.assertEqual((row["purpose"], row["plan_sha256"], row["plan"]), ("calibration", "abc", {"p": 1}))
        finally:
            tr.close()


class AuditTests(Gov):
    def run_main(self, req, *extra):
        out, stdin = io.StringIO(), io.StringIO(json.dumps(req))
        with contextlib.redirect_stdout(out):
            import sys
            old, sys.stdin = sys.stdin, stdin
            try:
                code = academy_bridge.main(["--root", str(self.root), "--strategies-dir", str(SPEC_DIR), *extra])
            finally:
                sys.stdin = old
        return code, json.loads(out.getvalue())

    def log(self):
        return AuditLog(self.root / "academy_audit.sqlite3")

    def test_every_call_is_recorded_chained_and_verifiable(self):
        self.run_main({"command": "status"})
        self.run_main({"command": "start_cycle"})
        code, bad = self.run_main({"command": "run_hypothesis", "cycle_id": 1})                 # an error is recorded too
        self.assertEqual(code, 1)
        self.run_main({"command": "nonsense"})                                                 # even an unknown command
        log = self.log()
        try:
            rows = log.entries()
            self.assertEqual([r["command"] for r in rows], ["status", "start_cycle", "run_hypothesis", "nonsense"])
            self.assertEqual([r["ok"] for r in rows], [1, 1, 0, 0])
            self.assertTrue(rows[2]["error"])
            self.assertEqual(json.loads(rows[0]["request_json"]), {"command": "status"})
            self.assertEqual(json.loads(rows[1]["response_json"])["ok"], True)
            self.assertEqual(log.verify()["ok"], True)
            self.assertEqual(log.verify()["entries"], 4)
        finally:
            log.close()

    def test_append_only_and_tamper_detection(self):
        for _ in range(3):
            self.run_main({"command": "status"})
        log = self.log()
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                log.db.execute("UPDATE calls SET command='x' WHERE id=1")
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                log.db.execute("DELETE FROM calls WHERE id=1")
            # someone drops the triggers and removes the middle entry: the chain exposes it
            log.db.execute("DROP TRIGGER calls_no_delete")
            log.db.execute("DELETE FROM calls WHERE id=2")
            log.db.commit()
            res = log.verify()
            self.assertFalse(res["ok"])
            self.assertEqual(res["first_bad_id"], 3)
        finally:
            log.close()
        log = self.log()                      # editing an answer in place is detected as well
        try:
            log.db.execute("DROP TRIGGER calls_no_update")
            log.db.execute("UPDATE calls SET response_json='{\"ok\": true, \"x\": 1}' WHERE id=1")
            log.db.commit()
            self.assertFalse(log.verify()["ok"])
        finally:
            log.close()

    def test_secrets_are_masked_and_audit_failure_never_changes_the_reply(self):
        self.run_main({"command": "notebook", "note": "Bearer abcdefghijklmnop ghp_abcdefghijklmnopqrstuv"})
        log = self.log()
        try:
            text = log.entries()[0]["request_json"]
            self.assertNotIn("abcdefghijklmnop", text)
            self.assertIn("Bearer ***", text)
        finally:
            log.close()
        with mock.patch.object(AuditLog, "append", side_effect=sqlite3.OperationalError("disk full")):
            code, out = self.run_main({"command": "status"})
        self.assertEqual((code, out["ok"]), (0, True))

    def test_export_roundtrip_and_genesis(self):
        self.run_main({"command": "status"})
        log = self.log()
        try:
            n = log.export_jsonl(self.root / "audit.jsonl")
            first = json.loads((self.root / "audit.jsonl").read_text().splitlines()[0])
            self.assertEqual((n, first["prev_hash"]), (1, GENESIS))
        finally:
            log.close()


class CliTests(Gov):
    def cli(self, *args):
        from market_data import academy_cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = academy_cli.main(["--root", str(self.root), *args])
        return code, json.loads(out.getvalue())

    def test_audit_command_verifies_tails_and_exports(self):
        academy_bridge.main  # noqa: B018 - make sure the module is loaded
        AuditLog(self.root / "academy_audit.sqlite3").append(command="status", request={"command": "status"}, response={"ok": True}, ok=True, duration_ms=1)
        code, out = self.cli("audit", "--tail", "5", "--export", str(self.root / "a.jsonl"))
        self.assertEqual((code, out["verify"]["ok"], out["exported"], len(out["last_calls"])), (0, True, 1, 1))

    def test_correct_lesson_command(self):
        sid = self.record("academy:study", {"verdict": "no_evidence"})
        self.mem.close()
        mem = ResearchMemory(self.root / "academy_memory.sqlite3")
        with mock.patch.object(ResearchMemory, "_refuse_refuted_from_no_evidence", staticmethod(lambda *a: None)):
            old = mem.add_lesson(self.reg, claim="c", verdict="refuted", confidence=0.8, evidence=[{"experiment_id": sid}], scope={})
        mem.close()
        code, out = self.cli("correct-lesson", "--lesson-id", str(old), "--verdict", "inconclusive", "--reason", "no_evidence is not refuted")
        self.assertEqual((code, out["original"]), (0, "kept, marked superseded"))
        self.mem = ResearchMemory(self.root / "academy_memory.sqlite3")        # tearDown closes it
        self.assertEqual(self.mem.lesson(old)["superseded_by"], out["new_lesson_id"])


class CompletedBarsTests(Gov):
    def test_provider_drops_the_forming_day_and_archive_appends_only_complete_days(self):
        from market_data.archive import completed_bars_provider
        idx = pd.to_datetime(["2026-10-30 04:00", "2026-11-02 05:00", "2026-11-03 05:00"], utc=True)
        frame = pd.DataFrame({"Open": [1.0, 1.1, 1.2], "High": [1.2, 1.2, 1.3], "Low": [0.9, 1.0, 1.1], "Close": [1.1, 1.15, 1.25], "Volume": [1.0] * 3,
                              "Dividends": [0.0] * 3, "Stock Splits": [0.0] * 3}, index=idx)

        class T:
            def history(self, **kw):
                return frame
        prov = completed_bars_provider(now=pd.Timestamp("2026-11-03 12:00", tz="UTC"), factory=lambda s: T())
        got = prov("X").history(period="1mo", interval="1d")
        self.assertEqual([str(i.date()) for i in got.index], ["2026-10-30", "2026-11-02"])
        info = MarketArchive(self.root).ingest("NQ=F", "1d", period="1mo", provider=prov)
        self.assertEqual(info["last_timestamp_utc"][:10], "2026-11-02")

    def test_stderr_noise_never_reaches_the_reply_channel(self):
        import sys

        def noisy(*a, **k):
            print("Pandas4Warning: inplace is deprecated", file=sys.stderr)
            return {"ok": True}
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(academy_bridge, "handle", noisy), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            old, sys.stdin = sys.stdin, io.StringIO('{"command": "status"}')
            try:
                code = academy_bridge.main(["--root", str(self.root)])
            finally:
                sys.stdin = old
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"ok": True}))
        self.assertNotIn("Pandas4Warning", err.getvalue())


class RejectedBarsTests(Gov):
    def test_malformed_provider_bars_are_reported_not_hidden(self):
        idx = pd.to_datetime(["2027-03-01", "2027-03-02", "2027-03-03"], utc=True)
        frame = pd.DataFrame({"Open": [1.0, 1.1, 1.1157], "High": [1.2, 1.2, 1.1237], "Low": [0.9, 1.0, 1.1157], "Close": [1.1, 1.15, 1.1156],
                              "Volume": [0.0] * 3}, index=idx)            # last bar: Low above Close, as Yahoo delivered for EURUSD on 2026-11-02

        class T:
            def history(self, **kw):
                return frame
        from market_data.archive import completed_bars_provider
        prov = completed_bars_provider(now=pd.Timestamp("2027-03-04 12:00", tz="UTC"), factory=lambda s: T())
        a = MarketArchive(self.root)
        with mock.patch.object(academy_bridge, "completed_bars_provider", lambda: prov):
            out = academy_bridge._refresh_data(a, a)
        e = out["refreshed"]["EURUSD"]["standard"]
        self.assertEqual(e["rejected_bars"], ["2027-03-03"])
        self.assertEqual(e["last_bar"], "2027-03-02")


class RefreshTests(Gov):
    def test_refresh_appends_to_each_archive_that_holds_the_instrument_and_survives_errors(self):
        calls = []

        def fake(self_, symbol, interval="1d", period="10y", *, provider=None):
            calls.append((str(self_.root), symbol, period))
            if symbol == "EURUSD=X":
                raise RuntimeError("network down")
            return {"last_timestamp_utc": "2026-11-02T00:00:00+00:00", "row_count": 700}
        with mock.patch.object(MarketArchive, "ingest", fake):
            out = self.call({"command": "refresh_data"})
        r = out["refreshed"]
        self.assertEqual(r["GOLD"]["standard"]["last_bar"], "2026-11-02")
        self.assertIn("error", r["EURUSD"]["standard"])
        self.assertEqual(r["NASDAQ100"], {})                                # not in any archive: nothing is created
        self.assertTrue(all(p == "1mo" for _, _, p in calls))              # only appends recent bars; never re-downloads or replaces history


if __name__ == "__main__":
    unittest.main()
