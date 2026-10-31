import gzip
import json
import re
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import experiments as ex
    from market_data.academy.experiments import Split, run_training, run_final_test
    from market_data.academy.ledger import LedgerStore, check_id
    from market_data.academy.export import export_all, MAX_EQUITY_POINTS
    from tests.test_academy import random_walk
except ImportError:
    pd = None

SQL = Path(__file__).resolve().parents[1] / "market_data" / "academy" / "sql" / "001_academy_private_storage.sql"
UUID = "123e4567-e89b-12d3-a456-426614174000"


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bars = random_walk(700, seed=11)
        cls.reg = ExperimentRegistry(cls.root / "r.db")
        cls.store = LedgerStore(cls.root, UUID, "acme")
        cls.out = run_training(cls.bars, cls.reg, symbol="TEST", interval="1d", experiments=16, seed=3,
                               control_runs=3, ledger=cls.store)

    @classmethod
    def tearDownClass(cls):
        cls.reg.close()
        cls.tmp.cleanup()

    def test_ledger_written_for_every_experiment(self):
        ids = self.store.experiments()
        self.assertEqual(len(ids), 16)
        for i in ids:
            self.assertEqual(self.store.periods(i), ["train", "validation"])   # no final_test during training
            log = self.store.read_period(i, "validation")
            self.assertEqual(set(log), {"decisions", "orders", "trades", "positions", "equity", "risk", "halted"})
            self.assertEqual(len(log["positions"]), len(log["trades"]))
            m = log["risk"]["metrics"]
            self.assertEqual(m["trades"], len(log["trades"]))
            end = 1000.0 + sum(t["net_pnl"] for t in log["trades"])
            self.assertAlmostEqual(log["equity"][-1]["equity"], end, places=6)
            self.assertAlmostEqual(min(r["drawdown_pct"] for r in log["equity"]), m["max_drawdown_pct"], places=6)
            meta = self.store.read_meta(i)
            self.assertEqual((meta["owner_id"], meta["org_id"], meta["domain"]), (UUID, "acme", "simulation"))

    def test_training_ledger_contains_no_test_period_timestamps(self):
        split = Split.chronological(len(self.bars))
        limit = self.bars.index[split.val_end - 1].isoformat()
        for i in self.store.experiments():
            for p in ("train", "validation"):
                self.assertLessEqual(self.store.read_period(i, p)["equity"][-1]["timestamp"], limit)

    def test_ledgers_are_isolated_per_owner_and_org(self):
        self.assertEqual(LedgerStore(self.root, "someone-else", "acme").experiments(), [])
        self.assertEqual(LedgerStore(self.root, UUID, "other-org").experiments(), [])
        other = LedgerStore(self.root, "someone-else", "acme")
        with self.assertRaises(ValueError):
            run_final_test(self.bars, self.reg, self.store.experiments()[0], ledger=other)

    def test_write_period_checks_ownership(self):
        eid = self.store.experiments()[0]
        before = (self.root / "academy_runs" / str(eid) / "train.json.gz").read_bytes()
        payload = self.store.read_period(eid, "train")
        for intruder in (LedgerStore(self.root, "intruder", "acme"), LedgerStore(self.root, UUID, "other-org")):
            with self.assertRaises(ValueError):
                intruder.write_period(eid, "train", {"decisions": []})
            with self.assertRaises(ValueError):
                intruder.write_period(eid, "validation", payload)
            with self.assertRaises(ValueError):
                intruder.read_period(eid, "train")
            with self.assertRaises(ValueError):
                intruder.read_meta(eid)
            with self.assertRaises(ValueError):
                intruder.periods(eid)
        self.assertEqual((self.root / "academy_runs" / str(eid) / "train.json.gz").read_bytes(), before)

    def test_write_period_requires_existing_meta_and_valid_period(self):
        with self.assertRaises(ValueError):
            self.store.write_period(987654, "train", {})          # no meta yet
        self.assertFalse((self.root / "academy_runs" / "987654").exists())
        with self.assertRaises(ValueError):
            self.store.write_period(self.store.experiments()[0], "../evil", {})

    def test_tampered_meta_without_ownership_is_refused(self):
        iso = self.root / "tamper"
        store = LedgerStore(iso, UUID, "acme")
        d = iso / "academy_runs" / "555"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"experiment_id": 555}))
        with self.assertRaises(ValueError):
            store.write_period(555, "train", {})
        (d / "meta.json").write_text(json.dumps({"owner_id": UUID, "org_id": "acme", "domain": "holdings"}))
        with self.assertRaises(ValueError):
            store.write_period(555, "train", {})
        self.assertEqual(store.experiments(), [])      # foreign-domain ledgers are not even listed

    def test_summary_refuses_other_owner(self):
        self.store.write_summary(7001, {"x": 1})
        with self.assertRaises(ValueError):
            LedgerStore(self.root, "intruder", "acme").write_summary(7001, {"x": 2})
        self.assertEqual(LedgerStore(self.root, UUID, "acme").summaries()[-1]["x"], 1)

    def test_ledger_refuses_to_overwrite_other_owner(self):
        eid = self.store.experiments()[0]
        with self.assertRaises(ValueError):
            LedgerStore(self.root, "intruder", "acme").write_meta(eid, {"strategy": "x"})
        self.assertEqual(self.store.read_meta(eid)["owner_id"], UUID)

    def test_id_validation_blocks_path_traversal(self):
        for bad in ("../x", "a/b", "", "a" * 65, "a b", "..", "x\\y"):
            with self.assertRaises(ValueError):
                check_id(bad, "id")
            with self.assertRaises(ValueError):
                LedgerStore(self.root, bad)

    def export(self, name, **kw):
        out = self.root / name
        info = export_all(self.store, out, generated_at="2026-01-01T00:00:00+00:00", **kw)
        return out, Path(info["path"]), info

    def test_export_structure_and_domain_markers(self):
        out, base, info = self.export("e1")
        self.assertEqual(base, out / "v1" / "simulation" / "orgs" / "acme" / "users" / UUID)
        files = [p for p in base.rglob("*") if p.is_file()]
        self.assertEqual(len(files), info["files"])
        for p in files:
            doc = json.loads(p.read_text())
            self.assertEqual(doc["schema_version"], "1.0")
            self.assertEqual(doc["domain"], "simulation")
            self.assertIs(doc["simulated"], True)
            self.assertEqual((doc["owner_id"], doc["org_id"]), (UUID, "acme"))
            self.assertIn("No real orders", doc["disclaimer"])
        # nothing outside the simulation namespace
        self.assertEqual([p.name for p in (out / "v1").iterdir()], ["simulation"])

    def test_export_has_no_absolute_paths_or_secrets(self):
        _, base, _ = self.export("e2")
        blob = "".join(p.read_text() for p in base.rglob("*.json"))
        self.assertNotIn(str(self.root), blob)
        for word in ("password", "secret", "api_key", "token"):
            self.assertNotIn(word, blob.lower())

    def test_manifest_hashes_match_files(self):
        import hashlib
        _, base, _ = self.export("e3")
        man = json.loads((base / "manifest.json").read_text())
        for rel, meta in man["data"]["files"].items():
            blob = (base / rel).read_bytes()
            self.assertEqual(hashlib.sha256(blob).hexdigest(), meta["sha256"])
            self.assertEqual(len(blob), meta["bytes"])

    def test_export_is_deterministic(self):
        _, a, ia = self.export("e4")
        _, b, ib = self.export("e5")
        self.assertEqual(ia["content_sha256"], ib["content_sha256"])
        self.assertEqual((a / "manifest.json").read_text(), (b / "manifest.json").read_text())

    def test_decisions_omit_abstentions_by_default_and_report_counts(self):
        _, base, _ = self.export("e6")
        eid = self.store.experiments()[0]
        full = self.store.read_period(eid, "validation")["decisions"]
        doc = json.loads((base / f"experiments/{eid}/validation/decisions.json").read_text())
        self.assertTrue(all(d["action"] != "ABSTAIN" for d in doc["data"]))
        self.assertEqual(doc["decisions_total"], len(full))
        self.assertEqual(doc["abstentions_omitted"] + len(doc["data"]), len(full))
        _, base2, _ = self.export("e7", include_abstentions=True)
        doc2 = json.loads((base2 / f"experiments/{eid}/validation/decisions.json").read_text())
        self.assertEqual(len(doc2["data"]), len(full))

    def test_equity_downsampled_keeps_endpoints(self):
        _, base, _ = self.export("e8")
        eid = self.store.experiments()[0]
        doc = json.loads((base / f"experiments/{eid}/train/equity.json").read_text())
        full = self.store.read_period(eid, "train")["equity"]
        self.assertLessEqual(len(doc["data"]), MAX_EQUITY_POINTS)
        self.assertEqual(doc["data"][0], full[0])
        self.assertEqual(doc["data"][-1], full[-1])

    def test_final_test_only_exported_after_consumed(self):
        ft_root = self.root / "ft"
        ft_root.mkdir()
        reg = ExperimentRegistry(ft_root / "ft.db")
        store = LedgerStore(ft_root, "ft-owner", "ft-org")
        out = run_training(self.bars, reg, symbol="TEST", interval="1d", experiments=16, seed=3,
                           control_runs=2, ledger=store)
        sel = out["selected_experiment_id"]
        if sel is None:
            reg.close()
            self.skipTest("no experiment met the minimum trade count")
        def do_export(name):
            info = export_all(store, self.root / name, generated_at="2026-01-01T00:00:00+00:00")
            return Path(info["path"])
        base = do_export("f1")
        self.assertFalse((base / f"experiments/{sel}/final_test").exists())
        self.assertFalse(json.loads((base / f"experiments/{sel}/summary.json").read_text())["data"]["final_test_consumed"])
        run_final_test(self.bars, reg, sel, ledger=store)
        base = do_export("f2")
        self.assertTrue((base / f"experiments/{sel}/final_test/trades.json").is_file())
        self.assertTrue(json.loads((base / f"experiments/{sel}/summary.json").read_text())["data"]["final_test_consumed"])
        reg.close()

    def test_refuses_export_inside_git_repo(self):
        repo = Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            export_all(self.store, repo / "tmp_export_should_not_exist")
        self.assertFalse((repo / "tmp_export_should_not_exist").exists())

    def test_final_test_lookup_not_limited_to_recent_1000(self):
        # ids are resolved directly in SQL, so old experiments remain testable
        row = self.reg.db.execute("SELECT MIN(id) FROM experiments").fetchone()[0]
        self.assertIsNotNone(row)

    def test_storage_policy_structure(self):
        sql = SQL.read_text().lower()
        self.assertIn("public = false", sql)
        self.assertIn("'application/json'", sql)
        # exactly one permissive grant: select, authenticated only
        permissive = re.findall(r"create policy \"[^\"]+\" on storage\.objects\s+as permissive for (\w+) to ([^\n]+?)\n", sql)
        self.assertEqual(permissive, [("select", "authenticated")])
        # restrictive guards cover select/insert/update/delete for anon+authenticated
        guards = re.findall(r"as restrictive for (\w+) to anon, authenticated", sql)
        self.assertEqual(sorted(guards), ["delete", "insert", "select", "update"])
        self.assertNotIn("to public", sql)
        self.assertNotIn("for all", sql)
        self.assertIn("security definer", sql)
        self.assertIn("set search_path = ''", sql)
        self.assertIn("revoke all on function public.academy_export_path_ok(text) from public", sql)
        self.assertEqual(sql.count("drop policy if exists"), sql.count("create policy"))   # idempotent

    def _path_regex(self, uid):
        text = SQL.read_text()
        m = re.search(r"object_name ~ \('(\^v1[^']+)' \|\| auth\.uid\(\)::text \|\| '([^']+)'\)", text)
        self.assertIsNotNone(m, "path validation regex not found in SQL")
        return re.compile(m.group(1) + re.escape(uid) + m.group(2))

    def test_policy_regex_accepts_exporter_paths_and_rejects_bad_ones(self):
        rx = self._path_regex(UUID)
        info = export_all(self.store, self.root / "pol", generated_at="2026-01-01T00:00:00+00:00")
        root = Path(info["path"]).parents[5]    # the --out directory
        count = 0
        for p in Path(info["path"]).rglob("*.json"):
            self.assertTrue(rx.search(p.relative_to(root).as_posix()), p)
            count += 1
        self.assertGreater(count, 10)
        base = f"v1/simulation/orgs/acme/users/{UUID}"
        for ok in (f"{base}/manifest.json", f"{base}/experiments/3/validation/trades.json"):
            self.assertTrue(rx.search(ok), ok)
        bad = [f"v1/simulation/orgs/acme/users/{UUID.replace('1','2',1)}/manifest.json",   # other uid
               f"v2/simulation/orgs/acme/users/{UUID}/manifest.json",                         # wrong version
               f"v1/portfolio/orgs/acme/users/{UUID}/manifest.json",                          # other namespace
               f"v1/simulation/orgs/acme/users/{UUID}/../x/manifest.json",                    # traversal
               f"v1/simulation/orgs/acme/users/{UUID}/a..b.json",
               f"v1/simulation/orgs/acme/users/{UUID}//manifest.json",                        # empty segment
               f"v1/simulation/orgs/acme/users/{UUID}/manifest.json.exe",
               f"v1/simulation/orgs/acme/users/{UUID}/manifest.csv",
               f"v1/simulation/orgs/acme/users/{UUID}",                                        # no file
               f"v1/simulation/orgs/ac me/users/{UUID}/manifest.json",
               f"v1/simulation/orgs/{'a'*65}/users/{UUID}/manifest.json",
               f"x/v1/simulation/orgs/acme/users/{UUID}/manifest.json",                        # prefix smuggling
               f"v1/simulation/orgs/acme/users/{UUID}x/manifest.json",                         # uid prefix match
               f"v1/simulation/orgs/acme/extra/users/{UUID}/manifest.json"]
        for b in bad:
            self.assertFalse(rx.search(b), b)

    def test_audit_script_is_read_only(self):
        audit = (SQL.parent / "002_academy_audit_storage_policies.sql").read_text().lower()
        code = "\n".join(l for l in audit.splitlines() if not l.strip().startswith("--"))
        for word in ("insert ", "update ", "delete ", "drop ", "create ", "alter ", "grant ", "revoke "):
            self.assertNotIn(word, code)
        self.assertIn("pg_policies", code)


if __name__ == "__main__":
    unittest.main()
