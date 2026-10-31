# Saba Academy: autonomous research loop and long-term memory

Status: implemented and tested in this repository. **Not verified inside the running Trinity agent** (`saba-pulsit-online`):
dependencies in the container, volume mapping, tool registration and the slash command still have to be integrated and tested there.
No broker, no live orders. Saba needs no per-experiment approvals; the safeguards are code.

## Pieces and where they live

| Need | Component | File |
|---|---|---|
| Hypotheses before runs, lessons with proof, search, notebook | `ResearchMemory` (SQLite on the data volume) | `academy/memory.py` |
| Facts (experiments, trials, final tests) | existing `ExperimentRegistry` + integrity index + `LedgerStore` | `laboratory.py`, `academy/integrity.py`, `academy/ledger.py` |
| Cycle budgets, kill-switch, runs, analysis, ranking, variants | `Orchestrator`, `mutate_spec` (ranking via existing `core/exploration_policy.py`) | `academy/orchestrator.py` |
| Agent-facing interface | JSON subprocess bridge, fixed allowlist | `academy_bridge.py` |
| Operator controls | `academy_cli halt` / `resume` | `academy_cli.py` |
| Strategy creation | JSON specs in git (`strategies/`), variants, causality gate | `academy/spec_strategy.py`, `academy/causality.py` |
| Cross-market work | `compare`, instrument specs | `academy/compare.py`, `academy/instruments/` |
| Proposed Trinity wiring | slash command + schedule example | `integration/` |

## One cycle
`status` -> `start_cycle` -> `propose_next`/`search_lessons` -> `register_hypothesis` (pre-registration) -> `run_hypothesis` -> `analysis` ->
`add_lesson` (evidence required) -> optional single `final_test` -> `conclude_hypothesis` -> `finish_cycle`.

Defaults (file `academy_budget.json` on the volume overrides): **20 experiments per cycle, 4 cycles per rolling 24 h, 1800 s per cycle**.
The experiment count is the number of new, distinct experiments; reused results cost nothing. A plan is refused up front if it needs more
experiments than the cycle has left or than the hypothesis's own trial budget allows.

## Safeguards that do not depend on the agent behaving
* **Pre-registration:** the plan (exact strategy content, instruments, interval) is hashed when the hypothesis is registered. `run_hypothesis`
  runs exactly that plan and refuses a hypothesis whose plan changed. New ideas are new (child) hypotheses with their own trial budget.
* **Trials are counted and priced in:** every distinct experiment per market counts; the luck benchmark rises with the count.
* **Lessons need proof:** a lesson must cite existing Academy experiments; their result hashes are stored and `verify_lesson` detects later
  edits or deletions. `supported` requires a consumed final test with positive net return, at least 5 trades and no shortfall; validation-only
  success is at most a `candidate`; `refuted` may rest on validation evidence. Dead ends stay visible in the notebook.
* **Final test:** one per experiment, window budget, monotonic frontier (see ACADEMY.md). The bridge exposes `final_test` without approval, and
  the protection refuses misuse.
* **Kill-switch:** `halt` (Saba or operator) stops new cycles and runs. Only the operator lifts it (`academy_cli resume`); the bridge has no resume
  command. An unreadable halt file counts as halted.
* **Bridge surface:** fixed command allowlist, strict type checks, strategy names from the registry, `spec_file` limited to plain `*.json`
  names inside the strategies directory, params validated (mandatory stop) before registration, `execution: DISABLED` on errors.

## Memory model (what goes where)
* Facts stay in the registry/ledger; the memory holds claims with links, never copies of results.
* `research_notebook.md` is generated (open hypotheses with budgets, supported/candidate findings, dead ends, status incl. consumed test
  windows) and size-capped, for loading at cycle start. It is a view; the stores are the source of truth.
* Optional mirroring of events into the existing `memory/experience_store.ExperienceStore` (`academy_events.sqlite3`) gives a generic timeline.
* Persistence: everything is under the archive root (`SABA_MARKET_DATA_DIR`); mount that on the agent's persistent volume and back it up.
  Git holds code and versioned strategy definitions (`strategies/`); results and lessons do not go to git.

## Not done / known limits
* Strategies written as arbitrary Python by Saba are **not** supported yet. That needs an isolated runner (no network, no data access,
  resource limits, AST restrictions) because the causality gate cannot detect side-channel reads. JSON specs are the supported path.
* The analysis is rule-based advice. Judgment (which lesson, which verdict) stays with Saba, but the bridge makes unsupported claims fail.
* Tests simulate the clock and use synthetic archives; real multi-day operation has not been run.
* The registry/memory are plain local SQLite files. An agent with shell access to the volume could edit them; this is a research-integrity
  guard, not a security boundary. Real-money safeguards (verified broker protection, an order gateway outside the agent) do not exist and
  are out of scope here.
* Hypotheses are per local store; multi-agent or multi-tenant sharing would need separate roots or a database.
