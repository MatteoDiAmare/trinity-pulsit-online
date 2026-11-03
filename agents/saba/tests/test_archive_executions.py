import argparse
import gzip
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "integration" / "archive_executions.py"
spec = importlib.util.spec_from_file_location("archive_executions", SCRIPT)
ax = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ax)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.abcdefghijklmnop"


class FakeTrinity:
    def __init__(self, executions, logs):
        self.executions, self.logs, self.calls = executions, logs, []

    def __call__(self, path):
        self.calls.append(path)
        if "/executions?" in path:
            return self.executions
        eid = path.split("/executions/")[1].split("/")[0]
        if isinstance(self.logs.get(eid), Exception):
            raise self.logs[eid]
        return {"execution_id": eid, "log": self.logs[eid]}


def ex(eid, status="success"):
    return {"id": eid, "schedule_id": "__manual__", "status": status, "started_at": "2026-11-02T08:00:00Z", "completed_at": "2026-11-02T08:05:00Z",
            "duration_ms": 1, "cost": 0.1, "message": "/academy-cycle"}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "audit" / "trinity-executions"
        ax.prepare(self.out)
        self.args = argparse.Namespace(agent="saba", limit=500)
        self.logger = mock.patch.object(ax.subprocess, "run")       # no real syslog lines from tests
        self.logger.start()
        self.devnull = open(os.devnull, "w")
        self.quiet = mock.patch("sys.stdout", new=self.devnull)
        self.quiet2 = mock.patch("sys.stderr", new=self.devnull)
        self.quiet.start(), self.quiet2.start()

    def tearDown(self):
        self.quiet2.stop(), self.quiet.stop()
        self.logger.stop()
        self.devnull.close()
        self.tmp.cleanup()

    def run_with(self, fake):
        with mock.patch.object(ax, "api", fake):
            return ax.archive(self.args, self.out)

    def test_archives_finished_skips_running_and_never_duplicates(self):
        fake = FakeTrinity([ex("a"), ex("b", "failed"), ex("c", "running")], {"a": [{"type": "result"}], "b": [], "c": []})
        s = self.run_with(fake)
        self.assertEqual((s["archived_new"], s["still_running"], s["failed"]), (2, 1, 0))
        self.assertEqual(sorted(p.name for p in self.out.glob("*.json.gz")), ["a.json.gz", "b.json.gz"])
        again = self.run_with(FakeTrinity([ex("a"), ex("b", "failed"), ex("c")], {"c": []}))             # c finished meanwhile
        self.assertEqual((again["archived_new"], again["already_archived"]), (1, 2))
        self.assertEqual(len(ax.manifest_lines(self.out)), 3)
        self.assertTrue(ax.verify(self.out)["ok"])

    def test_secrets_are_masked_but_ordinary_words_are_not(self):
        log = [{"type": "user", "text": f"token ghp_abcdefghijklmnopqrstu Bearer abcdefghijklmnop {JWT} password=hunter2hunter2hunter2 "
                                        "ADMIN_PASSWORD: s3cretvaluexyz123 sk-ant-api03-abcdefghijklmnop -----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY----- "
                                        "risk-adjusted task-management"}]
        self.run_with(FakeTrinity([ex("s")], {"s": log}))
        text = gzip.open(self.out / "s.json.gz").read().decode()
        for leak in ("abcdefghijklmnopqrstu", JWT, "hunter2hunter2hunter2", "s3cretvaluexyz123", "api03-abcdef", "MIIabc"):
            self.assertNotIn(leak, text)
        self.assertIn("risk-adjusted", text)
        self.assertIn("task-management", text)
        self.assertEqual(ax.find_secrets(text), [])

    def test_unmaskable_secret_is_not_archived(self):
        with mock.patch.object(ax, "mask", lambda t: t):
            s = self.run_with(FakeTrinity([ex("x")], {"x": [{"t": "ghp_abcdefghijklmnopqrstu"}]}))
        self.assertEqual((s["archived_new"], s["failed"]), (0, 1))
        self.assertFalse((self.out / "x.json.gz").exists())

    def test_expired_login_is_a_clear_error_and_main_raises_the_alert(self):
        def fake(path):
            raise ax.ArchiveError("Trinity login expired or missing: run integration/trinity_login.sh")
        with mock.patch.object(ax, "api", fake):
            code = ax.main(["--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertTrue((self.out / "ALERT").exists())
        st = ax.read_status(self.out)
        self.assertEqual(st["last_result"], "failed")
        self.assertIn("login expired", st["last_error"])
        self.assertEqual(ax.main(["--out", str(self.out), "--status"]), 1)

    def test_success_clears_alert_and_records_status(self):
        ax.raise_alert(self.out, "old failure")
        with mock.patch.object(ax, "api", FakeTrinity([ex("a")], {"a": []})):
            self.assertEqual(ax.main(["--out", str(self.out)]), 0)
        self.assertFalse((self.out / "ALERT").exists())
        self.assertEqual(ax.main(["--out", str(self.out), "--status"]), 0)

    def test_stale_status_is_reported(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
        ax.write_status(self.out, last_success=old, last_result="ok")
        res = ax.status(self.out, 36)
        self.assertFalse(res["ok"])
        self.assertIn("old", res["problems"][0])

    def test_if_stale_skips_when_recent_and_runs_when_not(self):
        ax.write_status(self.out, last_success=datetime.now(timezone.utc).isoformat(), last_result="ok")
        fake = FakeTrinity([ex("a")], {"a": []})
        with mock.patch.object(ax, "api", fake):
            self.assertEqual(ax.main(["--out", str(self.out), "--if-stale", "20"]), 0)
        self.assertEqual(fake.calls, [])
        ax.write_status(self.out, last_success=(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat())
        with mock.patch.object(ax, "api", fake):
            self.assertEqual(ax.main(["--out", str(self.out), "--if-stale", "20"]), 0)
        self.assertTrue(fake.calls)

    def test_parallel_run_is_refused_by_the_lock(self):
        import fcntl
        fh = open(self.out / ".lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fake = FakeTrinity([ex("a")], {"a": []})
        with mock.patch.object(ax, "api", fake):
            self.assertEqual(ax.main(["--out", str(self.out)]), 0)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.out / "a.json.gz").exists())
        fh.close()

    def test_listing_that_hits_the_limit_is_an_error(self):
        self.args.limit = 2
        with mock.patch.object(ax, "api", FakeTrinity([ex("a"), ex("b")], {"a": [], "b": []})):
            self.assertEqual(ax.main(["--out", str(self.out), "--limit", "2"]), 1)
        self.assertIn("limit", ax.read_status(self.out)["last_error"])

    def test_one_unreadable_log_fails_the_run_but_keeps_the_others(self):
        fake = FakeTrinity([ex("a"), ex("bad")], {"a": [], "bad": ax.ArchiveError("boom")})
        with mock.patch.object(ax, "api", fake):
            self.assertEqual(ax.main(["--out", str(self.out)]), 1)
        self.assertTrue((self.out / "a.json.gz").exists())
        self.assertTrue((self.out / "ALERT").exists())


class VerifyTests(ArchiveTests):
    def make(self):
        self.run_with(FakeTrinity([ex("a"), ex("b"), ex("c")], {"a": [1], "b": [2], "c": [3]}))
        self.assertTrue(ax.verify(self.out)["ok"])

    def problems(self):
        res = ax.verify(self.out)
        self.assertFalse(res["ok"])
        return " | ".join(res["problems"])

    def test_detects_modified_missing_and_orphan_files(self):
        self.make()
        with gzip.open(self.out / "a.json.gz", "wb") as fh:
            fh.write(b"{}")
        self.assertIn("a.json.gz: content differs", self.problems())
        (self.out / "b.json.gz").unlink()
        self.assertIn("b.json.gz: file missing", self.problems())
        (self.out / "zzz.json.gz").write_bytes(gzip.compress(b"x"))
        os.chmod(self.out / "zzz.json.gz", 0o600)
        self.assertIn("zzz.json.gz: archive file not listed", self.problems())

    def test_detects_removed_manifest_line_and_edited_entry(self):
        self.make()
        lines = ax.manifest_lines(self.out)
        (self.out / "manifest.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
        self.assertIn("chain broken", self.problems())
        e = json.loads(lines[1])
        e["status"] = "forged"
        (self.out / "manifest.jsonl").write_text("\n".join([lines[0], json.dumps(e, sort_keys=True), lines[2]]) + "\n")
        self.assertIn("entry hash mismatch", self.problems())

    def test_detects_open_permissions_and_secret_in_stored_text(self):
        self.make()
        os.chmod(self.out / "a.json.gz", 0o644)
        self.assertIn("permissions too open", self.problems())
        os.chmod(self.out / "a.json.gz", 0o600)
        os.chmod(self.out / "manifest.jsonl", 0o644)
        self.assertIn("manifest.jsonl: permissions too open", self.problems())
        os.chmod(self.out / "manifest.jsonl", 0o600)
        os.chmod(self.out, 0o755)
        self.assertIn("directory", self.problems())
        os.chmod(self.out, 0o700)
        self.assertTrue(ax.verify(self.out)["ok"])

    def test_legacy_unchained_manifest_is_accepted_and_extended_with_a_link(self):
        legacy = []
        for eid in ("old1", "old2"):
            raw = json.dumps({"execution": {"id": eid}, "log": []}).encode()
            with gzip.open(self.out / f"{eid}.json.gz", "wb") as fh:
                fh.write(raw)
            os.chmod(self.out / f"{eid}.json.gz", 0o600)
            legacy.append(json.dumps({"execution_id": eid, "status": "success", "started_at": "x", "bytes": len(raw), "sha256": ax._sha(raw), "file": f"{eid}.json.gz"}))
        (self.out / "manifest.jsonl").write_text("\n".join(legacy) + "\n")
        self.assertIn("manifest.jsonl: permissions too open", self.problems())          # a legacy manifest with open permissions is flagged
        os.chmod(self.out / "manifest.jsonl", 0o600)
        self.assertTrue(ax.verify(self.out)["ok"])
        self.run_with(FakeTrinity([ex("old1"), ex("new1"), ex("new2")], {"new1": [], "new2": []}))
        self.assertTrue(ax.verify(self.out)["ok"])
        lines = ax.manifest_lines(self.out)
        self.assertEqual(len(lines), 4)
        self.assertEqual(json.loads(lines[2])["prev"], ax._sha(("\n".join(legacy) + "\n").encode()))
        self.assertEqual(json.loads(lines[3])["prev"], json.loads(lines[2])["entry_sha256"])
        (self.out / "manifest.jsonl").write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n")        # drop a legacy line
        self.assertIn("chain broken", self.problems())


if __name__ == "__main__":
    unittest.main()
