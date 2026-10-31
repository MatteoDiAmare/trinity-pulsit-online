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

## NOT verified / blocked
1. **A cycle started by Saba herself through Trinity, and the schedule registered in Trinity.** Both need an authenticated Trinity API call
   (`POST /api/agents/saba-pulsit-online/schedules` and `/task`). I could not obtain credentials: reading the admin password from the backend
   container was refused by the environment's safety control, and I did not work around it. Everything is prepared: `integration/register_with_trinity.sh`
   (needs `TRINITY_TOKEN`; creates the schedule `0 */6 * * *` = 4 cycles/day and can start one cycle with `--run-now`).
2. **`/academy-cycle` is not yet a registered command in her repo.** The prepared change (command, strategy folders, disabled schedule in
   `template.yaml`, docs) is in `integration/saba-pulsit-online-academy.patch`. Pushing a branch/PR to `saba-pulsit-online` was refused by the environment's
   safety control, so nothing was pushed there. Apply with `git am` on a branch of her repo and open a PR.
3. **The autonomy mandate is not live in her instructions.** It exists only on the unmerged branch `docs/saba-autonomy-two-accounts` (no PR). Her
   `CLAUDE.md` on `main` and in the container still says: start without schedules, do not start autonomous execution, Matteo approves material actions.
   Merge the mandate before enabling the schedule, otherwise her own instructions contradict the cycle.
4. Her container is one sync commit ahead of `origin/main` (unpushed, plus modified tracked `.claude/policy-limits*` files): the auto-sync does not seem to be pushing.
5. Trinity MCP/tool registration: Saba is a Claude Code agent; the bridge is used through the Bash tool (`allowed-tools` in the command). No Trinity-side tool
   registration exists or was needed; this was not tested end to end for lack of (1).

## Findings about her environment (not changed by me)
* `~/.mcp.json` is world-readable (mode 644) and contains a Trinity MCP key; it is git-ignored and not on GitHub, but any process in the container can read it.
  Her user has passwordless `sudo`. Her process environment holds GH/Claude/Trinity tokens (the sandbox worker gets none of them).
* Her existing research (`~/content/trading-research`: SPY SMA backtest, gold real-yield test) predates Academy; its holdouts are not known to Academy's test-window registry.
* One pending item in her operator queue (create a demo/paper trading account) was not touched.

## Persistence and rebuild notes
* Everything Academy owns is under `/home/developer/.local/share/saba` on the named workspace volume; it survives restart (tested) and recreation of the container
  as long as the volume is kept. The venv points at the image's Python: after a base-image upgrade run the deploy script again to rebuild it.
* The archive/registry/memory files are SQLite/Parquet; `.local/` is git-ignored in her repo, so they are not backed up to GitHub. Back up the volume.
* If Trinity's "reset to main, preserve state" is used, only paths in `.trinity/persistent-state.yaml` are preserved; `.local/share/saba/**` is not listed there.
  Whether that reset removes ignored files was not tested.
