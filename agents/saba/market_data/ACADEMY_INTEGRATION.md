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

## First autonomous cycles through Trinity (2026-10-31)
Started with `integration/run_one_cycle.sh` (task `/academy-cycle` on the existing agent `saba`, authenticated with the locally stored token; schedule NOT active).
* **Cycle 1 - failed by the platform:** 7 min, 39 tool calls, hit Trinity's 50-turn limit (`Task exceeded turn limit`); Trinity stored no log. 13 of 39 calls were errors from guessing
  field names; the cycle stayed `running`. Even so she registered 6 hypotheses, ran 15 experiments, wrote 7 lessons and used one final test (EUR/USD SMA: validation Sharpe 0.87 vs
  luck benchmark 0.84 -> final test 0.11, refuted by her own lesson). Her real tool calls were read from her Claude session file in the container.
* **Fixes from that run:** errors return the exact usage and a `help` command exists; `start_cycle` returns a briefing; `register_hypothesis` can run at once (`run_in_cycle`);
  `add_lesson` can conclude the hypothesis; cut-off cycles are closed by the next start and still count toward the daily budget; a hypothesis can be `supported` only with a
  `supported` lesson (hypothesis 2 was left `supported` by the old code although her lesson 4 refutes it; her record was not edited).
* **Cycle 2 - success:** 12.6 min, 53 tool calls, 6 errors, ended with `finish_cycle` and a report; cost about USD 1.75. She read the notebook (citing lessons from cycle 1), chose a new
  direction herself, wrote and policy-tested four Python strategies in the sandbox (the sandbox rejected `_direction` and `view.open` with exact reasons), ran three formal experiments plus
  one final test: streak momentum (GOLD) inconclusive, volume-surge momentum (GOLD) inconclusive, MA pullback (SP500) passed validation (Sharpe 1.20 vs benchmark 1.18) and collapsed in the
  final test (Sharpe -0.87), refuted. Her conclusion: validation passes are noise-prone, regime dependence, next ideas: regime detection and intraday data.
* **Saved and reusable:** all 10 lessons verify against the registry, the notebook for later cycles carries them, 9 hypotheses and 10 distinct trials are stored in the volume, her code strategies
  are committed in her repo, and a redeploy of the Academy code kept all research data.
* **Loophole found in this run and closed:** she screened parameter variants through `test_strategy`, which showed validation results without counting trials (the registered pullback was
  the best of several variants), so the luck benchmark was too low. `test_strategy` now shows the train portion only. Also fixed: the policy blocked `view.open` (price data) for strategies.

## Applying the real turn limit: controlled recreate of Saba only (2026-10-31)
Root cause of the 50-turn failures: Trinity stores per-agent guardrails in its database (`max_turns_task: 300`) but the agent container reads them only at creation, from the
`AGENT_GUARDRAILS` env; Saba's container had none, so `/opt/trinity/guardrails-runtime.json` stayed at the baseline 50 turns. A per-task `max_turns` in the task request is not forwarded.
Trinity's own remedy (GUARD-001): `start` recreates a container whose guardrails drifted. Procedure used, for Saba only:
1. Pre-flight (read-only): nothing running, no image drift (same image id and Python), volume 757 MB, fingerprints of research data, git workspace, config and credentials (hashes only).
2. Backup of the workspace without credential files (`~/saba-backups/`, mode 600, 193 MB; restore = untar into the volume).
3. `POST /api/agents/saba/stop` then `/start` through Trinity (reply: `recreated: true, recreate_reason: config_drift`).
4. Verification after: research counts and hashes (registry, ledgers, lessons, strategies, parquet, code version, venv, bridge) identical; git head/branch/remote/user identical, including her unpushed local commit;
   `.claude.json`, `.mcp.json`, `.claude/settings.json` identical; every secret env var (OAuth, GitHub, Trinity auth, MCP key) unchanged (hash prefix compared); only `AGENT_GUARDRAILS` is new;
   runtime guardrails now `max_turns_task` 300 / `max_turns_chat` 300; a minimal task through Trinity answered (Claude authentication works); bridge answers; 0 schedules; the other agents were not touched.
5. Note: the runtime `execution_timeout_sec` guardrail is a separate field (chat path, still 1800 s); task runs take their limit from the agent's `execution_timeout_seconds` (3600 s) per the code path. A run longer than 30 minutes has not yet been observed.

## NOT verified / still open
1. **The recurring schedule** is intentionally not created or enabled. `integration/register_with_trinity.sh` creates it disabled; `--enable` switches it on.
2. Whether the `schedules:` block in `template.yaml` is applied to an already running agent by Trinity (assumed not; the script creates it through the API).
3. A cycle's behaviour under the platform turn limit now relies on the combined commands and her own planning; a longer cycle may still be cut off (cycle 2 used 54 turns and passed, so the
   limit is evidently not a hard 50 for every run; this was not investigated).
4. The updated `/academy-cycle` wording (`integration/academy-cycle.md`: help, briefing, train-only dry run) is not yet in her repo; it needs another PR and your approval.

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
