# Saba Academy: isolated execution of strategy code written by Saba

Saba may write Python strategies herself (`strategies/code/<name>.py` in her repo, versioned in git) and improve them by editing and
re-testing. They run **only** in a separate worker process (`academy/sandbox/`). Verified in this repository's tests and inside Saba's
container (Python 3.13, Docker's own seccomp profile): see ACADEMY_INTEGRATION.md.

## What a strategy can and cannot do
Write exactly one `class Strategy` with `__init__(self, params)` and `on_bar(self, view, position)`; return `Decision(...)` or `None`.
Available: `np` (curated numpy subset: mean, std, percentile, polyfit, where, ...), `math`, `statistics`, `rng.uniform()/normal()/integers()` (seeded),
`view.open/high/low/close/volume` (read-only arrays), `view.n/total`, `view.timestamp` (`.hour`, `.weekday()`, `.isoformat()`), `position`
(`side`, `entry_price`, `quantity`, `bars_held`). Not available: files, network, processes, `os`/`sys`/`subprocess`/`ctypes`, `eval/exec/open`,
`getattr`/`type`, dunder access, decorators, class bases, `with`, `yield`, `str.format`, `np.random/load/ctypeslib/...`.

## Layers (defence in depth)
| Layer | What it stops | Where |
|---|---|---|
| Closed AST subset | imports other than math/statistics/numpy, dunder and frame attributes, dynamic code, I/O names, format-string tricks | `sandbox/policy.py` (checked in the parent AND again in the worker) |
| Restricted builtins + curated `np` | everything not listed, incl. numpy file/ctypes/random APIs | `sandbox/worker.py` |
| Audit hook (after arming) | `open` of any file, socket, subprocess, `os.*` mutations, ctypes, shutil, tempfile, pickle/marshal, dynamic `exec`/`compile` | `worker.py` `_hook` |
| seccomp-bpf filter | socket/connect/bind/..., execve/fork/vfork, clone without CLONE_THREAD, ptrace, kill, mount/unshare/setuid, any write-open, rename/unlink/mkdir/chmod | `worker.py` `_x86_64_filter` (x86_64 only; fails closed elsewhere) |
| Resource limits | memory (RLIMIT_AS, default 1 GiB), CPU seconds, no file writes (FSIZE=0), 16 descriptors, no core dumps; per-bar timeout (5 s) and total time limit; process killed on violation | `worker.arm`, `runner.py` |
| Process hygiene | empty environment (no tokens), empty temp cwd, no inherited descriptors, own session, single-threaded BLAS, `PYTHONHASHSEED=0` | `runner.py` `CLEAN_ENV` |
| Data protocol | the worker only receives bars up to the current one (history first, then one bar per decision): there is no future data to read | `runner.py` / `worker.py` |

## Causality, reproducibility, traceability
* Causal by construction (above) and still run through the causality gate (determinism, truncation and future-tamper invariance). Because the
  worker has no file or network access, the gate's earlier blind spot (side-channel reads) does not exist for sandboxed code.
* Unseeded randomness cannot be imported; `rng` is seeded from `params.seed`. Two runs give identical decisions (tested).
* The experiment's params contain the full source, `code_sha256`, seed and non-default limits; they are part of the spec hash, so any change of code
  is a new, separately counted experiment, and an identical one is reused. `code_version` (Academy commit) is stored as well. A hash mismatch is refused.
* Runtime errors, policy violations, time/memory violations and invalid decisions (non-finite numbers, missing stop) reject the strategy before anything is recorded.

## Honest limits
* This is one process in the same container and kernel, not a VM. It is built for buggy or curious code and raises the bar for malicious code a lot,
  but a native-code or interpreter exploit (e.g. in numpy) is not ruled out. Do not treat it as a boundary against a determined attacker.
* File reads are blocked by the Python audit hook, not by the kernel filter (seccomp cannot filter by path). In the container, `~/.mcp.json` is
  world-readable and holds a Trinity key; the sandbox keeps strategy code away from it, but the file itself should be protected (see ACADEMY_INTEGRATION.md).
* `test_strategy` shows TRAIN-period results only. Validation data is reachable solely through registered (counted) experiments; otherwise a strategy can be tuned on
  validation by repeated dry runs without the trial counter noticing (this happened in Saba's second cycle and was closed afterwards).
* Linux x86_64 only; other platforms refuse to run strategy code.
* Allowed numpy functions are a short list; more can be added deliberately in `worker.py`.
* Per-bar IPC makes sandboxed strategies slower than built-ins (about 1 ms/bar including start-up for daily data; more for compute-heavy code).
