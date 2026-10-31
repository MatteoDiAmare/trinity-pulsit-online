import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from market_data import academy_bridge
    from market_data.laboratory import ExperimentRegistry
    from market_data.academy import (Decision, SandboxError, SandboxedStrategy, SimConfig, Simulator, build_strategy, load_instrument,
                                     StressModel)
    from market_data.academy.causality import check_causal
    from market_data.academy.experiments import run_training
    from market_data.academy.integrity import spec_hash
    from market_data.academy.sandbox import PolicyError, code_sha256, validate_code
    from market_data.academy.sandbox import runner
    from tests.test_academy import random_walk
except ImportError:
    pd = None

WORKER = Path(runner.WORKER) if pd is not None else None
HARNESS = Path(__file__).with_name("sandbox_harness.py")
X86 = platform.machine() == "x86_64" and sys.platform.startswith("linux")

SMA = '''
import numpy as np
class Strategy:
    def __init__(self, params):
        self.fast = params.get("fast", 10)
        self.slow = params.get("slow", 30)
    def on_bar(self, view, position):
        if view.n < self.slow:
            return None
        f = np.mean(view.close[-self.fast:])
        s = np.mean(view.close[-self.slow:])
        want = 1 if f > s else -1
        if position is not None and position.side == want:
            return None
        return Decision("BUY" if want == 1 else "SELL", stop_loss_pct=2.0, reason="sma")
'''


def strat(body, init="pass"):
    return f"class Strategy:\n    def __init__(self, params):\n        {init}\n    def on_bar(self, view, position):\n" + \
           "".join(f"        {line}\n" for line in body.strip("\n").splitlines())


def harness(mode, timeout=60, env=None):
    p = subprocess.run([sys.executable, "-I", str(HARNESS), mode, str(WORKER)], capture_output=True, text=True, timeout=timeout,
                       env=env or dict(runner.CLEAN_ENV))
    return p


def children_running():
    out = subprocess.run(["pgrep", "-f", "sandbox/worker.py"], capture_output=True, text=True).stdout.split()
    return [pid for pid in out if pid != str(os.getpid())]


@unittest.skipIf(pd is None, "optional market-data dependencies not installed")
class PolicyTests(unittest.TestCase):
    def test_accepts_a_normal_strategy(self):
        validate_code(SMA)
        validate_code(strat("return None", "self.x = [i * 2 for i in range(3)]"))

    def test_rejects_hostile_or_unsupported_code(self):
        S = strat("return None")
        bad = {
            "import os": "import os\n" + S, "import sys": "import sys\n" + S, "subprocess": "import subprocess\n" + S,
            "socket": "import socket\n" + S, "ctypes": "import ctypes\n" + S, "importlib": "import importlib\n" + S,
            "from os": "from os import path\n" + S, "numpy.random": "import numpy.random\n" + S, "relative": "from . import x\n" + S,
            "private import": "from math import _x\n" + S,
            "__import__": strat("m = __import__('os')"), "eval": strat("return eval('1')"), "exec": strat("exec('x=1')"),
            "open": strat("open('/etc/passwd')"), "getattr": strat("return getattr(view, 'close')"),
            "globals": strat("return globals()"), "type": strat("return type(view)"), "dir": strat("return dir(view)"),
            "dunder attr": strat("return view.__class__"), "dunder chain": strat("return ().__class__.__bases__[0].__subclasses__()"),
            "func globals": strat("return (lambda: 0).__globals__"), "frame": strat("return view.gi_frame"),
            "str.format": strat("return '{0.__class__}'.format(view)"), "format_map": strat("return '{x}'.format_map({})"),
            "fstring dunder": strat("return f'{view.__class__}'"),
            "np.random": strat("return np.random.random()"), "np.load": strat("return np.load('x')"),
            "tofile": strat("view.close.tofile('x')"), "arr.ctypes": strat("return view.close.ctypes"),
            "arr.data": strat("return view.close.data"), "arr.base": strat("return view.close.base"),
            "np.ctypeslib": strat("return np.ctypeslib"), "np.os": strat("return np.os"),
            "decorator": "def deco(f):\n    return f\n@deco\ndef g():\n    return 1\n" + S,
            "class bases": "class A(Exception):\n    pass\n" + S, "metaclass": "class A(metaclass=type):\n    pass\n" + S,
            "with": strat("with view as v:\n    pass"), "yield": "def g():\n    yield 1\n" + S, "async": "async def g():\n    return 1\n" + S,
            "global": "x = 1\ndef g():\n    global x\n    x = 2\n" + S, "dunder def": "class Other:\n    def __getattr__(self, n):\n        return 1\n" + S,
            "private class": "class _Evil:\n    pass\n" + S, "dunder arg": "def g(__x):\n    return __x\n" + S,
            "next/iter": strat("return next(iter([1]))"), "super": strat("return super()"),
            "no strategy": "x = 1\n", "two strategies": S + S, "empty": "   ", "syntax": "def (:\n",
            "huge": S + "\n#" + "x" * 50_000,
        }
        for label, src in bad.items():
            with self.assertRaises(PolicyError, msg=label):
                validate_code(src)

    def test_policy_error_surfaces_as_sandbox_error_before_any_process(self):
        before = children_running()
        with self.assertRaises(SandboxError) as cm:
            SandboxedStrategy("import os\n" + strat("return None"))
        self.assertIn("policy", str(cm.exception))
        self.assertEqual(children_running(), before)


@unittest.skipIf(pd is None or not X86, "sandbox layers are implemented for Linux x86_64 only (and fail closed elsewhere)")
class RuntimeLayerTests(unittest.TestCase):
    def test_kernel_filter_blocks_network_exec_fork_and_writes(self):
        p = harness("kernel")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        for name in ("socket", "open_write", "openat_trunc", "mkdir", "unlink", "rename", "chmod", "fork", "execve", "ptrace", "kill",
                     "mount", "setuid", "unshare"):
            self.assertEqual(out[name], {"rc": -1, "errno": 1}, name)               # EPERM from the seccomp filter
        self.assertTrue(out["open_read_kernel_level"])        # honest: reads are stopped by the Python layer, not the kernel filter
        self.assertFalse(Path("/tmp/sbx_should_not_exist").exists())

    def test_threads_still_work_under_the_filter(self):
        out = json.loads(harness("threads").stdout.strip().splitlines()[-1])
        self.assertTrue(out["thread_created"])

    def test_audit_layer_blocks_files_processes_network_dynamic_code_and_hides_secrets(self):
        env = {**runner.CLEAN_ENV}
        p = harness("audit", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        for name in ("open_read", "open_write", "os_open", "listdir", "socket", "subprocess", "os_system", "fork", "ctypes_call",
                     "compile", "exec", "import_new"):
            self.assertEqual(out[name], "blocked", name)
        self.assertFalse(Path("/tmp/sbx_x").exists())
        self.assertNotIn("SBX_SECRET", out["environ_keys"])

    def test_resource_limits_are_applied(self):
        out = json.loads(harness("rlimits").stdout.strip().splitlines()[-1])
        self.assertEqual(out["as"], 700 * 1024 * 1024)
        self.assertEqual((out["cpu"], out["fsize"], out["nofile"], out["core"]), (30, 0, 16, 0))

    def test_memory_bomb_fails_instead_of_exhausting_the_host(self):
        out = json.loads(harness("memory").stdout.strip().splitlines()[-1])
        self.assertEqual(out["alloc_2g"], "memoryerror")
        self.assertEqual(out["alloc_small"], "ok")

    def test_cpu_limit_kills_a_busy_loop(self):
        t = time.time()
        p = harness("cpu", timeout=30)
        self.assertLess(time.time() - t, 15)
        self.assertNotEqual(p.returncode, 0)                  # killed by SIGXCPU / SIGKILL


@unittest.skipIf(pd is None or not X86, "sandbox layers are implemented for Linux x86_64 only")
class BehaviourTests(unittest.TestCase):
    def setUp(self):
        self.bars = random_walk(260, seed=7)
        self.started = children_running()

    def tearDown(self):
        time.sleep(0.2)
        self.assertEqual(children_running(), self.started, "worker processes were left behind")

    def run_code(self, code, **kw):
        limits = kw.pop("limits", None)
        return Simulator(SimConfig()).run(self.bars, SandboxedStrategy(code, limits=limits, **kw))

    def test_matches_the_builtin_strategy_decision_for_decision(self):
        a = self.run_code(SMA)
        b = Simulator(SimConfig()).run(self.bars, build_strategy("sma_cross", {"short": 10, "long": 30, "stop_loss_pct": 2.0}))
        key = lambda r: [(d["timestamp"], d["action"], d["stop_loss_pct"]) for d in r.decisions if d["action"] != "ABSTAIN"]
        self.assertEqual(key(a), key(b))
        self.assertEqual([t["net_pnl"] for t in a.trades], [t["net_pnl"] for t in b.trades])

    def test_strategy_sees_exactly_the_bars_up_to_now_and_nothing_later(self):
        code = strat("return Decision('ABSTAIN', reason=str(view.total) + ':' + str(view.close[-1]) + ':' + str(view.n))")
        res = self.run_code(code)
        close = self.bars["Close"].to_numpy()
        for i, d in enumerate(res.decisions):
            total, last, n = d["reason"].split(":")
            self.assertEqual(int(total), i + 1)
            self.assertEqual(float(last), float(close[i]))
            self.assertEqual(int(n), i + 1)

    def test_lookback_is_capped_at_3000_bars(self):
        bars = random_walk(3300, seed=1)
        code = strat("return Decision('ABSTAIN', reason=str(view.n) + ':' + str(view.total))")
        res = Simulator(SimConfig()).run(bars, SandboxedStrategy(code), trade_start=3200)
        n, total = res.decisions[-1]["reason"].split(":")
        self.assertEqual((int(n), int(total)), (3000, 3299))

    def test_causality_and_reproducibility(self):
        report = check_causal("sandboxed", SandboxedStrategy(SMA).params(), self.bars)
        self.assertTrue(report["ok"], report)
        r1, r2 = self.run_code(SMA), self.run_code(SMA)
        self.assertEqual(r1.trades, r2.trades)
        self.assertEqual(r1.decisions, r2.decisions)

    def test_unseeded_randomness_is_caught_and_seeded_rng_is_reproducible(self):
        code = strat("return Decision('BUY', stop_loss_pct=2.0) if rng.uniform() < 0.1 and position is None else None")
        a, b = self.run_code(code, seed=5), self.run_code(code, seed=5)
        self.assertEqual(a.trades, b.trades)
        self.assertNotEqual(a.trades, self.run_code(code, seed=6).trades)
        self.assertTrue(check_causal("sandboxed", SandboxedStrategy(code, seed=5).params(), self.bars)["ok"])

    def test_errors_are_reported_with_location_and_stop_the_run(self):
        with self.assertRaises(SandboxError) as cm:
            self.run_code(strat("return 1 / 0"))
        self.assertIn("ZeroDivisionError", str(cm.exception))
        self.assertIn("on_bar", str(cm.exception))
        with self.assertRaises(SandboxError):
            self.run_code(strat("return 'BUY'"))                  # wrong return type
        with self.assertRaises(SandboxError):
            self.run_code(strat("return Decision('BUY', stop_loss_pct=float('nan'))"))

    def test_infinite_loop_is_killed_at_the_time_limit(self):
        t = time.time()
        with self.assertRaises(SandboxError) as cm:
            self.run_code(strat("while True:\n    pass"), limits={"bar_timeout_seconds": 0.7})
        self.assertIn("time limit", str(cm.exception))
        self.assertLess(time.time() - t, 15)

    def test_memory_hog_inside_a_strategy_fails_cleanly(self):
        with self.assertRaises(SandboxError):
            self.run_code(strat("x = [0] * (10 ** 9)\nreturn None"), limits={"memory_mb": 256})

    def test_strategy_cannot_do_forbidden_things_at_runtime_even_via_allowed_names(self):
        # names that look harmless but must not work: print is a no-op, imports are refused at runtime too
        code = strat("print('hello')\nreturn Decision('ABSTAIN', reason='quiet')")
        self.assertTrue(self.run_code(code).decisions)
        with self.assertRaises(SandboxError):
            self.run_code(strat("import math\nm = math.sqrt(4.0)\nreturn Decision('ABSTAIN', reason=str(m))\n"
                                ).replace("import math", "from statistics import mean\n        import numpy"))  # statistics+numpy ok, still fine
        # the worker re-validates, so policy bypass at construction time is impossible: simulate a lying parent
        bad_init = {"code": "import os\n" + strat("return None"), "params": {}, "seed": 0, "lookback": 3000,
                    "limits": {"memory_mb": 512, "cpu_seconds": 5}}
        p = subprocess.Popen([sys.executable, "-I", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={})
        out, _ = p.communicate((json.dumps(bad_init) + "\n").encode(), timeout=60)
        self.assertIn(b"not allowed", out)

    def test_worker_environment_cwd_and_descriptors_are_clean(self):
        os.environ["SBX_SECRET_TEST"] = "do-not-leak"
        s = SandboxedStrategy(SMA)
        try:
            Simulator(SimConfig()).run(self.bars.iloc[:60], _Spy(s))
        finally:
            os.environ.pop("SBX_SECRET_TEST", None)
        env, cwd, fds = s_probe
        self.assertNotIn(b"SBX_SECRET_TEST", env)
        self.assertNotIn(b"do-not-leak", env)
        self.assertLessEqual({e.split(b"=")[0].decode() for e in env.split(b"\0") if e}, set(runner.CLEAN_ENV))
        self.assertTrue(cwd.startswith(tempfile.gettempdir()) and "saba-sandbox-" in cwd)
        self.assertEqual(os.listdir(cwd) if os.path.isdir(cwd) else [], [])
        self.assertLessEqual(len(fds), 6)                      # stdin/stdout/stderr + interpreter internals, nothing inherited


s_probe = None


class _Spy:
    """Wraps a SandboxedStrategy and snapshots the worker's /proc entries after it has started."""
    def __init__(self, inner):
        self.inner, self.n = inner, 0

    def on_bar(self, view, position):
        out = self.inner.on_bar(view, position)
        global s_probe
        pid = self.inner._proc.pid
        s_probe = (Path(f"/proc/{pid}/environ").read_bytes(), os.readlink(f"/proc/{pid}/cwd"), os.listdir(f"/proc/{pid}/fd"))
        return out

    def close(self):
        self.inner.close()


@unittest.skipIf(pd is None or not X86, "sandbox layers are implemented for Linux x86_64 only")
class ProvenanceAndIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bars = random_walk(520, seed=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_params_carry_source_hash_and_tampering_is_refused(self):
        s = SandboxedStrategy(SMA, params={"fast": 5, "slow": 20}, seed=3)
        p = s.params()
        self.assertEqual(p["code_sha256"], code_sha256(SMA))
        self.assertEqual(p["code"], SMA)
        rebuilt = build_strategy("sandboxed", p)
        self.assertEqual(rebuilt.params(), p)
        with self.assertRaises(SandboxError):
            build_strategy("sandboxed", {**p, "code": SMA + "\n# changed"})        # sha no longer matches

    def test_code_change_changes_the_spec_hash_and_is_recorded_with_code_version(self):
        reg = ExperimentRegistry(self.root / "r.db")
        plan = [{"strategy": "sandboxed", "params": SandboxedStrategy(SMA).params(), "label": "sma_code"}]
        out = run_training(self.bars, reg, symbol="T", interval="1d", plan=plan, control_runs=0)
        self.assertEqual(out["rejected_strategies"], [])
        rec = [r for r in reg.recent(50) if r["name"] == "academy:sandboxed"][0]
        cfg = rec["configuration"]
        self.assertEqual(cfg["params"]["code_sha256"], code_sha256(SMA))
        self.assertTrue(cfg["causality_checked"])
        self.assertTrue(cfg["code_version"] and cfg["spec_sha256"])
        again = run_training(self.bars, reg, symbol="T", interval="1d", plan=plan, control_runs=0)
        self.assertEqual((again["new_trials"], again["reused_experiments"]), (0, 1))          # identical code => reused
        changed = [{"strategy": "sandboxed", "params": SandboxedStrategy(SMA + "\n# comment").params(), "label": "sma_code"}]
        third = run_training(self.bars, reg, symbol="T", interval="1d", plan=changed, control_runs=0)
        self.assertEqual(third["new_trials"], 1)
        reg.close()

    def test_training_rejects_hostile_slow_and_broken_strategies_without_recording_them(self):
        reg = ExperimentRegistry(self.root / "r.db")
        plan = [{"strategy": "sandboxed", "params": SandboxedStrategy(SMA).params(), "label": "good"},
                {"strategy": "sandboxed", "params": {"code": "import os\n" + strat("return None")}, "label": "hostile"},
                {"strategy": "sandboxed", "params": SandboxedStrategy(strat("while True:\n    pass"), limits={"bar_timeout_seconds": 0.5}).params(),
                 "label": "spinner"},
                {"strategy": "sandboxed", "params": SandboxedStrategy(strat("return 1 / 0")).params(), "label": "broken"}]
        out = run_training(self.bars, reg, symbol="T", interval="1d", plan=plan, control_runs=0)
        self.assertEqual(sorted(r["label"] for r in out["rejected_strategies"]), ["broken", "hostile", "spinner"])
        recorded = [r for r in reg.recent(50) if r["name"] == "academy:sandboxed"]
        self.assertEqual(len(recorded), 1)
        reg.close()

    def test_bridge_loads_code_files_safely_and_dry_runs_on_development_data_only(self):
        root = self.root / "data"
        for sym, seed in (("GC=F", 1),):
            d = root / sym / "1d"
            d.mkdir(parents=True)
            random_walk(700, seed=seed).assign(Dividends=0.0, **{"Stock Splits": 0.0}).to_parquet(d / "bars.parquet")
        sdir = self.root / "strategies"
        (sdir / "code").mkdir(parents=True)
        (sdir / "code" / "sma_momentum.py").write_text(SMA)
        call = lambda req: academy_bridge.handle(req, root=root, strategies_dir=sdir)
        out = call({"command": "test_strategy", "code_file": "sma_momentum.py", "instrument": "GOLD", "params": {"fast": 8, "slow": 25}})
        self.assertTrue(out["causality"]["ok"])
        self.assertIn("first 560 of 700", out["data_used"])
        self.assertEqual(out["validation"]["shortfall"], 0.0)
        reg = call({"command": "register_hypothesis", "statement": "own momentum code", "family": "momentum", "trial_budget": 4,
                    "instruments": ["GOLD"], "experiments": [{"label": "mom", "code_file": "sma_momentum.py", "params": {"fast": 8, "slow": 25}}]})
        self.assertEqual(reg["cells"], 1)
        for bad in ("../x.py", "a/b.py", "x.py.exe", "", 3, "nope.py", "sma_momentum.PY"):
            with self.assertRaises(ValueError, msg=str(bad)):
                call({"command": "test_strategy", "code_file": bad, "instrument": "GOLD"})
        with self.assertRaises(ValueError):
            call({"command": "test_strategy", "code": "import os\n" + strat("return None"), "instrument": "GOLD"})
        # no experiments were recorded by the dry run
        self.assertEqual(call({"command": "status"})["open_hypotheses"], 1)


if __name__ == "__main__":
    unittest.main()
