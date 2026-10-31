"""Isolated strategy worker. Started by runner.py as:  python -I worker.py   (empty environment, empty cwd).

What untrusted strategy code can reach here, and what stops it:
  files      - the 'open' audit event is denied for everything once armed (all needed modules are imported first);
               the kernel filter additionally refuses any open for writing and every create/rename/unlink/chmod.
  network    - socket events are denied by the audit hook and the socket syscalls by the kernel filter.
  processes  - exec/fork/vfork/clone(non-thread)/ptrace/kill are denied by the kernel filter; subprocess/os.* events by the hook.
  env/secrets- the worker is started with an empty environment; it never receives credentials or paths.
  data       - it receives one bar at a time over a pipe, only bars up to 'now'. There is nothing to read ahead.
  resources  - RLIMIT_AS / CPU / FSIZE / NOFILE / CORE; the parent also enforces a per-bar timeout and kills the process.
  language   - a closed AST subset (policy.py), restricted builtins, a curated numpy subset and no dunder/attribute escapes.
This is defence in depth inside one container and one kernel - not a virtual machine. It is built for code that is
buggy or curious, and raises the bar for malicious code considerably, but it is not a proof against a native-code exploit.
"""
import ast
import json
import math
import os
import platform
import resource
import statistics
import struct
import sys
import traceback

import numpy

# lazily imported numpy parts must be loaded BEFORE the file barrier goes up
import numpy.linalg  # noqa: F401
import numpy.lib.stride_tricks  # noqa: F401,E402

HERE = os.path.dirname(os.path.abspath(__file__))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("sandbox_policy", os.path.join(HERE, "policy.py"))   # by path: no pandas, no package import
_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_policy)
validate_code = _policy.validate_code

ALLOW_EXEC = [False]
_RD = os.fdopen(0, "rb", buffering=0)
_WR = os.fdopen(1, "wb", buffering=0)
BAR = struct.Struct("<c5dqbddi")      # op, o,h,l,c,v, ts_ns, side, entry, qty, bars_held


def _send(obj):
    _WR.write((json.dumps(obj, allow_nan=False, separators=(",", ":")) + "\n").encode())


def _read_exact(n):
    buf = b""
    while len(buf) < n:
        chunk = _RD.read(n - len(buf))
        if not chunk:
            raise SystemExit(0)
        buf += chunk
    return buf


# ---------------------------------------------------------------- curated numpy
class _NP:
    """Whitelisted numpy surface: no I/O, no ctypes, no unseeded randomness."""
    pass


NP = _NP()

for _name in ("abs", "absolute", "arange", "argmax", "argmin", "argsort", "array", "asarray", "ceil", "clip", "concatenate", "convolve",
              "corrcoef", "cos", "cumprod", "cumsum", "diff", "dot", "exp", "floor", "inf", "isfinite", "isinf", "isnan", "linspace",
              "log", "log1p", "maximum", "mean", "median", "minimum", "nan", "nanmean", "nanstd", "nansum", "ones", "percentile",
              "polyfit", "polyval", "power", "prod", "quantile", "round", "sign", "sin", "sort", "sqrt", "std", "sum", "tanh",
              "var", "where", "zeros", "float64", "int64", "pi", "e", "max", "min", "all", "any", "full", "searchsorted",
              "nan_to_num", "ptp", "average", "cov", "sum", "histogram"):
    setattr(NP, _name, getattr(numpy, _name))      # on the instance: class attributes would bind as methods


class _Ts:
    def __init__(self, ns):
        t = numpy.datetime64(int(ns), "ns")
        s = str(t.astype("datetime64[s]"))
        self.year, self.month, self.day = int(s[0:4]), int(s[5:7]), int(s[8:10])
        self.hour, self.minute = int(s[11:13]), int(s[14:16])
        self.iso = s + "+00:00"
        self.dow = int(((t.astype("datetime64[D]").astype("int64") + 3) % 7))     # Monday = 0

    def weekday(self):
        return self.dow

    def isoformat(self):
        return self.iso


class View:
    def __init__(self, block, total, ts):
        self.open, self.high, self.low, self.close, self.volume = block
        self.n, self.total, self.timestamp = block.shape[1], total, ts


class Position:
    def __init__(self, side, entry, qty, held):
        self.side, self.entry_price, self.quantity, self.bars_held = side, entry, qty, held


class Decision:
    def __init__(self, action="ABSTAIN", size=1.0, stop_loss_pct=None, take_profit_pct=None, reason=""):
        self.action, self.size, self.stop_loss_pct, self.take_profit_pct, self.reason = action, size, stop_loss_pct, take_profit_pct, reason


class Rng:
    """Seeded generator handed to strategies (np.random is not exposed)."""
    def __init__(self, seed):
        self._g = numpy.random.default_rng(seed)

    def uniform(self):
        return float(self._g.random())

    def normal(self, mu=0.0, sigma=1.0):
        return float(self._g.normal(mu, sigma))

    def integers(self, lo, hi):
        return int(self._g.integers(lo, hi))


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in ("math", "statistics", "numpy"):
        raise ImportError(f"import of {name!r} is not allowed")
    return {"math": math, "statistics": statistics, "numpy": NP}[name]


_BUILTINS = {n: __builtins__[n] if isinstance(__builtins__, dict) else getattr(__builtins__, n) for n in (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len", "list", "map", "max", "min", "range",
    "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip", "ValueError", "Exception", "ZeroDivisionError", "ArithmeticError",
    "IndexError", "KeyError", "TypeError", "RuntimeError", "FloatingPointError", "OverflowError", "AssertionError", "StopIteration",
    "True", "False", "None", "__build_class__")}
_BUILTINS["__import__"] = _safe_import
_BUILTINS["print"] = lambda *a, **k: None


# ---------------------------------------------------------------- arming
def _x86_64_filter():
    import ctypes
    EPERM = 1
    RET_ERRNO = 0x00050000 | EPERM
    RET_ALLOW, RET_KILL = 0x7FFF0000, 0x80000000
    LD, ABS, JMP, JEQ, JSET, RET, K = 0x00, 0x20, 0x05, 0x10, 0x40, 0x06, 0x00
    code = []

    def stmt(c, k):
        code.append((c, 0, 0, k))

    def jump(c, k, jt, jf):
        code.append((c, jt, jf, k))

    nr = lambda: stmt(LD | ABS, 0)
    stmt(LD | ABS, 4)
    jump(JMP | JEQ | K, 0xC000003E, 1, 0)
    stmt(RET | K, RET_KILL)                                   # wrong architecture: kill
    nr()
    deny = [41, 42, 43, 44, 46, 47, 49, 50, 53, 57, 58, 59, 62, 76, 77, 82, 83, 84, 85, 86, 87, 88, 90, 91, 92, 93, 94,
            101, 105, 106, 113, 114, 117, 119, 155, 161, 165, 166, 169, 200, 234, 250, 260, 263, 264, 265, 266, 268, 272, 280, 288,
            298, 308, 310, 311, 316, 319, 321, 322, 425, 426, 427, 437]
    for n in deny:
        jump(JMP | JEQ | K, n, 0, 1)
        stmt(RET | K, RET_ERRNO)
    jump(JMP | JEQ | K, 435, 0, 1)                             # clone3: ENOSYS makes glibc fall back to clone (threads keep working)
    stmt(RET | K, 0x00050000 | 38)
    WRITE_FLAGS = 0o3 | 0o100 | 0o1000 | 0o2000 | 0o20000000 | 0o40000   # O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND|__O_TMPFILE|O_DIRECT? (superset ok)
    for syscall, arg_off in ((2, 16 + 8 * 1), (257, 16 + 8 * 2)):       # open(flags=arg1), openat(flags=arg2)
        jump(JMP | JEQ | K, syscall, 0, 4)
        stmt(LD | ABS, arg_off)
        jump(JMP | JSET | K, WRITE_FLAGS, 0, 1)
        stmt(RET | K, RET_ERRNO)
        nr()
    jump(JMP | JEQ | K, 56, 0, 4)                              # clone: only thread creation (CLONE_THREAD = 0x10000)
    stmt(LD | ABS, 16)
    jump(JMP | JSET | K, 0x10000, 1, 0)
    stmt(RET | K, RET_ERRNO)
    nr()
    stmt(RET | K, RET_ALLOW)

    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]

    arr = (SockFilter * len(code))(*[SockFilter(*c) for c in code])
    return SockFprog(len(code), arr), arr


def install_seccomp():
    import ctypes
    if platform.machine() != "x86_64":
        raise RuntimeError("sandbox requires x86_64 (fails closed on other architectures)")
    libc = ctypes.CDLL(None, use_errno=True)
    prog, keepalive = _x86_64_filter()
    if libc.prctl(38, 1, 0, 0, 0) != 0:                         # PR_SET_NO_NEW_PRIVS
        raise RuntimeError("cannot set no_new_privs")
    if libc.prctl(22, 2, ctypes.byref(prog), 0, 0) != 0:        # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
        raise RuntimeError(f"cannot install seccomp filter (errno {ctypes.get_errno()})")
    return keepalive


DENY_PREFIX = ("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.system", "os.remove", "os.rename",
               "os.mkdir", "os.rmdir", "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate", "os.listdir", "os.scandir",
               "os.chdir", "os.chroot", "os.kill", "os.putenv", "os.unsetenv", "os.walk", "ctypes.", "shutil.", "tempfile.", "urllib.",
               "http.", "ftplib.", "smtplib.", "glob.", "pathlib.", "mmap.", "signal.", "marshal.", "pickle.", "code.", "gc.",
               "sys.setprofile", "sys.settrace")


def _hook(event, args):
    if event == "open":
        raise PermissionError("file access is not allowed in the strategy sandbox")
    if event in ("exec", "compile") and not ALLOW_EXEC[0]:
        raise PermissionError("dynamic code execution is not allowed")
    if event.startswith(DENY_PREFIX):
        raise PermissionError(f"{event} is not allowed in the strategy sandbox")


def arm(limits):
    mb = 1024 * 1024
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (limits["memory_mb"] * mb, limits["memory_mb"] * mb))
    resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"] + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    keep = install_seccomp()
    sys.addaudithook(_hook)
    return keep


def _warm_up():
    a = numpy.arange(1.0, 40.0)
    numpy.polyfit(a, a * 2.0, 1), numpy.polyval([1.0, 0.0], a), numpy.corrcoef(a, a)
    numpy.percentile(a, 50), numpy.quantile(a, 0.5), numpy.convolve(a, a[:3]), numpy.cov(a, a), numpy.histogram(a, 5)
    numpy.median(a), numpy.nanmean(a), numpy.average(a), numpy.argsort(a), numpy.searchsorted(a, 3.0), numpy.nan_to_num(a)
    numpy.datetime64(0, "ns").astype("datetime64[s]")
    json.dumps({"a": 1}), statistics.mean([1.0, 2.0]), numpy.random.default_rng(0).random()


def _normalise(res):
    if isinstance(res, Decision):
        d = {"action": res.action, "size": res.size, "stop_loss_pct": res.stop_loss_pct, "take_profit_pct": res.take_profit_pct,
             "reason": res.reason}
    elif type(res) is dict:
        d = {k: res.get(k) for k in ("action", "size", "stop_loss_pct", "take_profit_pct", "reason")}
        d["action"] = d["action"] or "ABSTAIN"
        d["size"] = 1.0 if d["size"] is None else d["size"]
    elif res is None:
        d = {"action": "ABSTAIN", "size": 1.0, "stop_loss_pct": None, "take_profit_pct": None, "reason": ""}
    else:
        raise TypeError("on_bar must return Decision(...), a dict or None")
    for key in ("size", "stop_loss_pct", "take_profit_pct"):
        v = d[key]
        if v is not None:
            if type(v) not in (int, float, numpy.float64) or not math.isfinite(v):
                raise TypeError(f"{key} must be a finite number")
            d[key] = float(v)
    if type(d["action"]) is not str or type(d["reason"]) is not str:
        raise TypeError("action and reason must be text")
    d["reason"] = d["reason"][:200]
    return d


def main():
    init = json.loads(_RD.readline().decode())
    limits, code, user_params, seed = init["limits"], init["code"], init["params"], init["seed"]
    cap, offset = init["lookback"], init.get("offset", 0)
    _warm_up()
    tree = validate_code(code)                                   # the worker re-checks: never trust the parent's word
    compiled = compile(tree, "<strategy>", "exec")
    keep = arm(limits)                                           # from here on: no files, network, processes
    ns = {"__builtins__": _BUILTINS, "__name__": "strategy", "np": NP, "Decision": Decision, "rng": Rng(seed)}
    ALLOW_EXEC[0] = True
    try:
        exec(compiled, ns)
    finally:
        ALLOW_EXEC[0] = False
    strat = ns["Strategy"](dict(user_params))
    if not callable(getattr(strat, "on_bar", None)):
        raise TypeError("Strategy needs an on_bar(self, view, position) method")
    _send({"ok": True})
    buf = numpy.empty((5, cap * 2))
    n, total = 0, offset
    while True:
        raw = _read_exact(BAR.size)
        op, o, h, l, c, v, ts, side, entry, qty, held = BAR.unpack(raw)
        if op == b"Q":
            return
        if n == buf.shape[1]:
            buf[:, :cap] = buf[:, -cap:]
            n = cap
        buf[:, n] = (o, h, l, c, v)
        n += 1
        total += 1
        if op == b"H":
            continue
        block = buf[:, max(0, n - cap):n].copy()
        block.setflags(write=False)
        try:
            res = strat.on_bar(View(block, total, _Ts(ts)), None if side == 0 else Position(side, entry, qty, held))
            _send({"ok": True, "decision": _normalise(res)})
        except Exception as exc:                                  # reported, never raised into the loop
            tb = traceback.extract_tb(exc.__traceback__)
            where = "; ".join(f"{f.name}:{f.lineno}" for f in tb[-3:])
            _send({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]} [{where}]"})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except BaseException as exc:
        try:
            _send({"ok": False, "fatal": f"{type(exc).__name__}: {str(exc)[:300]}"})
        except Exception:
            pass
        os._exit(3)
