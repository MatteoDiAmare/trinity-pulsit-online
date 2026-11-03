import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "integration" / "trinity_archive_key.sh"
SECRET_KEY = "trinity_mcp_" + "k" * 40
PASSWORD = "correct-horse-battery-staple"


class Mock(BaseHTTPRequestHandler):
    log = []
    behaviour = {"key_status": 200, "scope": "ops", "revoke_status": 200}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        Mock.log.append((self.path, self.headers.get("Authorization"), raw))
        if self.path == "/api/token":
            ok = "password=" + PASSWORD in raw
            return self._send(200 if ok else 401, {"access_token": "jwt-for-test-only", "token_type": "bearer"} if ok else {"detail": "bad"})
        if self.headers.get("Authorization") != "Bearer jwt-for-test-only":
            return self._send(401, {"detail": "no"})
        if self.path == "/api/mcp/keys":
            return self._send(Mock.behaviour["key_status"], {"id": "key-123", "name": "saba-audit-archiver", "scope": Mock.behaviour["scope"], "api_key": SECRET_KEY})
        if self.path == "/api/mcp/keys/key-123/revoke":
            return self._send(Mock.behaviour["revoke_status"], {"status": "revoked"})
        self._send(404, {})


class KeyScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Mock)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pw = self.dir / "pw"
        self.pw.write_text(PASSWORD)
        os.chmod(self.pw, 0o600)
        self.key = self.dir / "cfg" / "archive.hdr"
        Mock.log.clear()
        Mock.behaviour.update({"key_status": 200, "scope": "ops", "revoke_status": 200})

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, pw=True):
        env = {**os.environ, "TRINITY_URL": f"http://127.0.0.1:{self.port}", "SABA_ARCHIVE_KEY_FILE": str(self.key)}
        a = list(args) + (["--password-file", str(self.pw)] if pw else [])
        return subprocess.run([str(SCRIPT), *a], capture_output=True, text=True, env=env, timeout=60)

    def test_create_writes_a_private_header_file_and_never_prints_the_key(self):
        r = self.run_script("create")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(SECRET_KEY, r.stdout + r.stderr)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        self.assertEqual(self.key.read_text(), f"Authorization: Bearer {SECRET_KEY}\n")
        meta = self.key.with_suffix(".meta")
        self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(meta.stat().st_mode), 0o600)
        m = json.loads(meta.read_text())
        self.assertEqual((m["key_id"], m["scope"]), ("key-123", "ops"))
        self.assertNotIn(SECRET_KEY, meta.read_text())                  # the metadata file holds no secret
        created = [x for x in Mock.log if x[0] == "/api/mcp/keys"][0]
        self.assertEqual(json.loads(created[2])["scope"], "ops")        # exactly the bounded scope is requested
        self.assertEqual(created[1], "Bearer jwt-for-test-only")

    def test_refuses_to_overwrite_and_refuses_a_non_ops_key(self):
        self.assertEqual(self.run_script("create").returncode, 0)
        again = self.run_script("create")
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)
        self.key.unlink()
        self.key.with_suffix(".meta").unlink()
        Mock.behaviour["scope"] = "user"                                  # a broader key must never be stored as the archiver key
        r = self.run_script("create")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.key.exists())
        self.assertNotIn(SECRET_KEY, r.stdout + r.stderr)

    def test_failures_do_not_echo_the_response(self):
        Mock.behaviour["key_status"] = 403
        r = self.run_script("create")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 403", r.stderr)
        self.assertFalse(self.key.exists())
        bad = Path(self.tmp.name) / "bad"
        bad.write_text("wrong")
        os.chmod(bad, 0o600)
        env = {**os.environ, "TRINITY_URL": f"http://127.0.0.1:{self.port}", "SABA_ARCHIVE_KEY_FILE": str(self.key)}
        r2 = subprocess.run([str(SCRIPT), "create", "--password-file", str(bad)], capture_output=True, text=True, env=env, timeout=60)
        self.assertIn("login failed", r2.stderr)

    def test_password_file_must_be_private_and_plain_http_to_a_remote_host_is_refused(self):
        os.chmod(self.pw, 0o644)
        self.assertEqual(self.run_script("create").returncode, 2)
        os.chmod(self.pw, 0o600)
        env = {**os.environ, "TRINITY_URL": "http://example.com", "SABA_ARCHIVE_KEY_FILE": str(self.key)}
        r = subprocess.run([str(SCRIPT), "create", "--password-file", str(self.pw)], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing", r.stderr)

    def test_info_and_revoke(self):
        self.assertEqual(self.run_script("info", pw=False).returncode, 1)         # nothing recorded yet
        self.run_script("create")
        info = self.run_script("info", pw=False)
        d = json.loads(info.stdout)
        self.assertEqual((d["key_id"], d["age_days"], d["key_file_mode"]), ("key-123", 0, "0o600"))
        self.assertNotIn(SECRET_KEY, info.stdout)
        r = self.run_script("revoke")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(p == "/api/mcp/keys/key-123/revoke" for p, _, _ in Mock.log))
        self.assertFalse(self.key.exists())
        self.assertTrue(Path(str(self.key) + ".revoked").exists())


if __name__ == "__main__":
    unittest.main()
