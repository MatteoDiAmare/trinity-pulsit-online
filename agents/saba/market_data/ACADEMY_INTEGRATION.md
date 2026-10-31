# Saba Academy: integration into the deployed Trinity agent - what is verified and what is not

Checked on 2026-10-31 against the local Trinity (docker) and the container `agent-saba` (agent `saba-pulsit-online`).
"Verified" means observed directly in that environment, not inferred.

## Verified in the running environment
| Item | Evidence |
|---|---|
| Real container configuration | `trinity-agent-base`, user `developer`, 2 CPU / 4 GiB, Python 3.13.15, network `trinity-agent-network`, restart `unless-stopped`; workspace = named volume `agent-saba-pulsit-online-workspace` mounted at `/home/developer`; config files mounted read-only |
| Her repo and sync | `/home/developer` is a git checkout of `saba-pulsit-online` (main); Trinity auto-sync is enabled (`GIT_SYNC_ENABLED=true`) and commits working-tree changes to main |
| Installation | Academy code (pinned commit, stamped in `ACADEMY_CODE_VERSION`), a self-contained venv (numpy, pandas, pyarrow, yfinance) and the fixed entry point `~/.local/share/saba/academy-bridge` are installed in the volume by `integration/deploy_academy_to_container.sh` (only under `~/.local/share/saba`, plus seed strategy specs in `~/strategies`) |
| Runtime compatibility | the full test suite (263 tests) passes inside the container with her interpreter; data fetch for GC=F, EURUSD=X, ES=F works from the container |
| Bridge in her runtime | `status` and a complete research cycle (register hypothesis, start cycle, run 4 experiments over 2 instruments incl. a JSON spec with causality gate, add evidence-linked lesson, notebook) ran in the container |
| Persistence | after `docker restart agent-saba` a new bridge process found the stored lesson and `verify_lesson` passed; venv and code survived. A fresh container from the same image mounting the same volume read-only saw the code, venv and state (rebuild simulation) |
| Isolated Python | the 23 sandbox tests pass inside the container, including the kernel-filter, audit-hook, rlimit, environment/descriptor and process-cleanup tests |

## Done since the first report (2026-10-31, later)
* Mandate PR #1 (`docs/saba-autonomy-two-accounts`) and integration PR #2 (`feature/academy-integration`) in `saba-pulsit-online` were reviewed against
  current `main` (clean merge; the Trinity sync had meanwhile pushed the seed strategies and policy stamps) and merged **with the operator's approval**.
  Her container was fast-forwarded (it was clean and in sync): `CLAUDE.md` now carries the mandate, `.claude/commands/academy-cycle.md` exists, `strategies/` and
  `strategies/code/` exist, `template.yaml` has the schedule **disabled**, and the bridge still answers.
* Trinity runs her tasks with `--dangerously-skip-permissions` (no permission prompts), so the bridge call is not blocked; it also means the strategy sandbox is the
  only barrier between her own Python code and the container.
* Local, token-safe tooling: `integration/trinity_login.sh` (hidden password prompt or a mode-600 password file; token only in a mode-600 header file, never printed or
  placed on a command line), `trinity_api.sh`, `run_one_cycle.sh` (one `/academy-cycle` task, waits, prints her actual tool calls with secrets masked) and
  `register_with_trinity.sh` (schedule created **disabled**; `--enable` only after the first autonomous cycle was verified).

## NOT verified / still open
1. **The first autonomous cycle through Trinity** and the **schedule in Trinity** (needs `trinity_login.sh`, see above). The recurring schedule is intentionally NOT active.
2. Whether the `schedules:` block in `template.yaml` is applied to an already running agent by Trinity (assumed not; the registration script creates it through the API).
3. Trinity MCP/tool registration: she is a Claude Code agent and uses the bridge through the Bash tool; no Trinity-side registration exists or was needed.

## Findings about her environment (not changed by me)
* `~/.mcp.json` is world-readable (mode 644) and contains a Trinity MCP key; it is git-ignored and not on GitHub, but any process in the container can read it.
  Her user has passwordless `sudo`. Her process environment holds GH/Claude/Trinity tokens (the sandbox worker gets none of them).
* The auto-sync did push (the container was later in sync with `origin/main`); it also commits `.claude/policy-limits*` stamp churn and anything new in tracked paths such as `strategies/`.
* Her existing research (`~/content/trading-research`: SPY SMA backtest, gold real-yield test) predates Academy; its holdouts are not known to Academy's test-window registry.
* One pending item in her operator queue (create a demo/paper trading account) was not touched.

## Persistence and rebuild notes
* Everything Academy owns is under `/home/developer/.local/share/saba` on the named workspace volume; it survives restart (tested) and recreation of the container
  as long as the volume is kept. The venv points at the image's Python: after a base-image upgrade run the deploy script again to rebuild it.
* The archive/registry/memory files are SQLite/Parquet; `.local/` is git-ignored in her repo, so they are not backed up to GitHub. Back up the volume.
* If Trinity's "reset to main, preserve state" is used, only paths in `.trinity/persistent-state.yaml` are preserved; `.local/share/saba/**` is not listed there.
  Whether that reset removes ignored files was not tested.
