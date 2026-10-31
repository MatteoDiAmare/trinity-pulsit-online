"""Parent side of the strategy sandbox: starts the isolated worker and drives it bar by bar.

The worker only ever receives bars up to the current one (history first, then one bar per decision), so a strategy
cannot read the future even in principle. Provenance: params() carries the full source and its sha256, which end up
in the experiment configuration and spec hash (code version is stored by the experiment layer).
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..strategies import Decision, MAX_LOOKBACK, STRATEGIES, Strategy, _mandatory_stop
from .policy import PolicyError, validate_code

WORKER = Path(__file__).resolve().with_name("worker.py")
BAR = struct.Struct("<c5dqbddi")
DEFAULT_LIMITS = {"memory_mb": 1024, "cpu_seconds": 120, "bar_timeout_seconds": 5.0, "startup_timeout_seconds": 20.0,
                  "max_total_seconds": 900.0}
CLEAN_ENV = {"PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
             "NUMEXPR_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"}


class SandboxError(ValueError):
    """Subclass of ValueError so the causality gate / experiment layer treat it as a rejected strategy."""


def code_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class SandboxedStrategy(Strategy):
    name = "sandboxed"

    def __init__(self, code: str, seed: int = 0, params: dict | None = None, limits: dict | None = None,
                 code_sha256: str | None = None):
        try:
            validate_code(code)
        except PolicyError as exc:
            raise SandboxError(f"policy: {exc}") from None
        self.code, self.seed = code, int(seed)
        self.sha = globals()["code_sha256"](code)
        if code_sha256 is not None and code_sha256 != self.sha:
            raise SandboxError("code_sha256 does not match the source")
        self.user_params = json.loads(json.dumps(params or {}, allow_nan=False))
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self._proc, self._seen, self._started = None, 0, 0.0

    def params(self):
        return {"code": self.code, "code_sha256": self.sha, "seed": self.seed, "params": self.user_params,
                "limits": {k: v for k, v in self.limits.items() if DEFAULT_LIMITS.get(k) != v}}

    # ---- process management --------------------------------------------------
    def _start(self, offset=0):
        cwd = tempfile.mkdtemp(prefix="saba-sandbox-")
        self._cwd = cwd
        self._proc = subprocess.Popen([sys.executable, "-I", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, cwd=cwd, env=dict(CLEAN_ENV), close_fds=True,
                                      start_new_session=True)
        self._started = time.monotonic()
        init = {"code": self.code, "params": self.user_params, "seed": self.seed, "lookback": MAX_LOOKBACK, "offset": offset,
                "limits": {k: self.limits[k] for k in ("memory_mb", "cpu_seconds")}}
        self._proc.stdin.write((json.dumps(init, allow_nan=False) + "\n").encode())
        self._proc.stdin.flush()
        reply = self._read_reply(self.limits["startup_timeout_seconds"])
        if not reply.get("ok"):
            msg = reply.get("fatal") or reply.get("error") or "worker failed to start"
            self.close()
            raise SandboxError(f"worker: {msg}")

    def _read_reply(self, timeout: float) -> dict:
        proc = self._proc
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            self.close()
            raise SandboxError(f"strategy exceeded the {timeout:g}s time limit and was killed")
        line = proc.stdout.readline()
        if not line:
            code = proc.poll()
            self.close()
            raise SandboxError(f"worker terminated unexpectedly (exit {code}); likely a resource limit or a blocked system call")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            self.close()
            raise SandboxError("malformed reply from the worker") from None

    def close(self):
        proc, self._proc = getattr(self, "_proc", None), None
        if proc is not None:
            try:
                if proc.poll() is None:
                    try:
                        proc.stdin.write(BAR.pack(b"Q", 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0))
                        proc.stdin.flush()
                        proc.wait(timeout=1)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
            finally:
                for f in (proc.stdin, proc.stdout):
                    try:
                        f.close()
                    except Exception:
                        pass
        cwd = getattr(self, "_cwd", None)
        if cwd:
            try:
                os.rmdir(cwd)
            except OSError:
                pass
            self._cwd = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- Strategy API ------------------------------------------------------------
    def _send_bar(self, op: bytes, view, i: int, position):
        side, entry, qty, held = (0, 0.0, 0.0, 0) if position is None else (position.side, position.entry_price, position.quantity, position.bars_held)
        ts = int(view.timestamp.value)
        self._proc.stdin.write(BAR.pack(op, float(view.open[i]), float(view.high[i]), float(view.low[i]), float(view.close[i]),
                                        float(view.volume[i]), ts, side, entry, qty, held))

    def on_bar(self, view, position):
        if self._proc is None:
            if self._started:
                raise SandboxError("sandbox was closed")
            self._start(view.total - view.n)
            for i in range(view.n - 1):                       # history only: bars the strategy may already know
                self._proc.stdin.write(BAR.pack(b"H", float(view.open[i]), float(view.high[i]), float(view.low[i]), float(view.close[i]),
                                                float(view.volume[i]), 0, 0, 0.0, 0.0, 0))
            self._seen = view.total - 1
        if view.total != self._seen + 1:
            self.close()
            raise SandboxError("bars must be delivered one at a time")
        if time.monotonic() - self._started > self.limits["max_total_seconds"]:
            self.close()
            raise SandboxError("total run time limit exceeded")
        try:
            self._send_bar(b"B", view, view.n - 1, position)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.close()
            raise SandboxError("worker pipe closed unexpectedly") from None
        self._seen = view.total
        reply = self._read_reply(self.limits["bar_timeout_seconds"])
        if not reply.get("ok"):
            self.close()
            raise SandboxError(f"strategy error: {reply.get('error') or reply.get('fatal')}")
        d = reply["decision"]
        try:
            if d["action"] in ("BUY", "SELL") and d["stop_loss_pct"] is not None:
                _mandatory_stop(d["stop_loss_pct"])
            return Decision(d["action"], d["size"], d["stop_loss_pct"], d["take_profit_pct"], d["reason"])
        except ValueError as exc:
            self.close()
            raise SandboxError(f"invalid decision from strategy: {exc}") from None


STRATEGIES["sandboxed"] = SandboxedStrategy
