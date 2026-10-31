"""Subprocess harness for the sandbox runtime-layer tests (not collected as a test module).

Modes (argv[1]):  kernel | audit | rlimits | cpu | memory | threads
It loads worker.py by path, arms the requested layer and reports what the process can still do as one JSON line.
"""
import ctypes
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading

WORKER = sys.argv[2]
spec = importlib.util.spec_from_file_location("sbx_worker", WORKER)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
libc = ctypes.CDLL(None, use_errno=True)
mode = sys.argv[1]
LIMITS = {"memory_mb": 700, "cpu_seconds": 30}
out = {}


def attempt(name, fn):
    try:
        r = fn()
        out[name] = "ok" if r is None or r is True else f"ret={r}"
    except PermissionError as exc:
        out[name] = "blocked"
    except OSError as exc:
        out[name] = f"oserror:{exc.errno}"
    except MemoryError:
        out[name] = "memoryerror"
    except Exception as exc:
        out[name] = f"{type(exc).__name__}"


if mode == "kernel":
    keep = w.install_seccomp()                                   # kernel filter ONLY (no audit hook)
    AF_INET, SOCK_STREAM = 2, 1
    for name, call in {
        "socket": lambda: libc.socket(AF_INET, SOCK_STREAM, 0),
        "open_write": lambda: libc.open(b"/tmp/sbx_should_not_exist", os.O_WRONLY | os.O_CREAT, 0o600),
        "openat_trunc": lambda: libc.openat(-100, b"/etc/hostname", os.O_RDONLY | os.O_TRUNC),
        "mkdir": lambda: libc.mkdir(b"/tmp/sbx_dir", 0o700),
        "unlink": lambda: libc.unlink(b"/tmp/nope"),
        "rename": lambda: libc.rename(b"/tmp/a", b"/tmp/b"),
        "chmod": lambda: libc.chmod(b"/tmp", 0o777),
        "fork": lambda: libc.fork(),
        "execve": lambda: libc.execve(b"/bin/true", None, None),
        "ptrace": lambda: libc.ptrace(0, 0, 0, 0),
        "kill": lambda: libc.kill(os.getpid(), 0),
        "mount": lambda: libc.mount(b"x", b"/mnt", b"tmpfs", 0, None),
        "setuid": lambda: libc.setuid(0),
        "unshare": lambda: libc.unshare(0x10000000),
    }.items():
        rc = call()
        out[name] = {"rc": rc, "errno": ctypes.get_errno()} if rc == -1 else {"rc": rc}
        ctypes.set_errno(0)
    fd = libc.open(b"/etc/hostname", os.O_RDONLY)                 # reading stays possible at kernel level (Python layer blocks it)
    out["open_read_kernel_level"] = fd >= 0
elif mode == "threads":
    keep = w.install_seccomp()
    res = []
    t = threading.Thread(target=lambda: res.append(1))
    t.start()
    t.join()
    out["thread_created"] = bool(res)
elif mode == "audit":
    s_ = socket.socket()                                         # created before arming on purpose
    s_.close()
    keep = w.arm(LIMITS)
    attempt("open_read", lambda: open(WORKER).read(1))
    attempt("open_write", lambda: open("/tmp/sbx_x", "w"))
    attempt("os_open", lambda: os.open(WORKER, os.O_RDONLY))
    attempt("listdir", lambda: os.listdir("/"))
    attempt("socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    attempt("subprocess", lambda: subprocess.run(["true"]))
    attempt("os_system", lambda: os.system("true"))
    attempt("fork", lambda: os.fork())
    attempt("ctypes_call", lambda: libc.getpid())
    attempt("compile", lambda: compile("1+1", "x", "eval"))
    attempt("exec", lambda: exec("x=1"))
    attempt("import_new", lambda: __import__("colorsys"))
    attempt("environ_has_secret", lambda: "SBX_SECRET" in os.environ and "leak")
    out["environ_keys"] = sorted(os.environ)
elif mode == "rlimits":
    import resource
    keep = w.arm(LIMITS)
    out["as"] = resource.getrlimit(resource.RLIMIT_AS)[0]
    out["cpu"] = resource.getrlimit(resource.RLIMIT_CPU)[0]
    out["fsize"] = resource.getrlimit(resource.RLIMIT_FSIZE)[0]
    out["nofile"] = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    out["core"] = resource.getrlimit(resource.RLIMIT_CORE)[0]
elif mode == "memory":
    keep = w.arm({"memory_mb": 700, "cpu_seconds": 30})
    attempt("alloc_2g", lambda: bytearray(2 * 1024 ** 3))
    attempt("alloc_small", lambda: len(bytearray(10 * 1024 ** 2)) and None)
elif mode == "cpu":
    keep = w.arm({"memory_mb": 256, "cpu_seconds": 1})
    print(json.dumps({"started": True}), flush=True)
    while True:
        pass
print(json.dumps(out), flush=True)
