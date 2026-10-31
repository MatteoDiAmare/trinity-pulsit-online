---
description: One autonomous Academy research cycle (budgeted, evidence-linked, simulation only)
allowed-tools: Bash(~/.local/share/saba/academy-bridge:*), Read, Write, Edit, Glob, Grep
---
Run one Academy research cycle. This is simulation research on historical data: there is no broker, no order and no money involved.
You choose the direction, instruments and strategies yourself; no approval is needed for research. The budget and the integrity rules
are enforced by the bridge, not by this text.

Call the bridge ONLY like this (one JSON object per call, answer is one JSON object):

~/.local/share/saba/academy-bridge <<'JSON'
{"command": "status"}
JSON

Procedure
1. `status`. If `halted` is set, stop and report the reason. `notebook` (with `"write": true`) shows your open hypotheses, findings,
   dead ends and which test windows are already consumed. Read it before choosing what to do.
2. `start_cycle`. If denied (halt or cycle budget), end the turn and say so. Do not retry.
3. Decide what to learn. Use `search_lessons` and `propose_next`; do not repeat dead ends without a new reason. Ideas can be a JSON
   strategy (`strategies/*.json`), a variant (`variants`), your own Python strategy (see below), another instrument (GOLD, EURUSD,
   SP500) or a comparison across instruments.
4. `register_hypothesis` BEFORE running: statement, family, `trial_budget`, instruments, interval and the exact experiments. The plan is
   hashed; `run_hypothesis` executes exactly that plan. A new idea is a new (child) hypothesis.
5. `run_hypothesis` (stays within the cycle's experiment budget). Read `analysis`: it suggests confirm / vary / drop, nothing more.
6. `add_lesson` with the experiment ids as evidence. `refuted` for clear negatives, `candidate` for validation-only positives,
   `inconclusive` when trades are too few. Only a consumed, positive `final_test` supports `supported`.
7. A strategy that clearly beats the luck benchmark may get ONE `final_test`. A test window can confirm only a few experiments and
   is consumed afterwards; if the bridge says a window is exhausted or no fresh data exists, do other research and come back after new
   data has arrived. Never try to work around it.
8. `conclude_hypothesis` when settled, `finish_cycle`, then a short report: what you tested, what you learned, what you will do next.

Your own Python strategies
- Write `strategies/code/<name>.py` defining exactly one `class Strategy` with `__init__(self, params)` and `on_bar(self, view, position)`.
  `view` has read-only numpy arrays `open high low close volume`, `n`, `total` and `timestamp` (`.hour .weekday() .isoformat()`).
  Return `Decision("BUY"|"SELL"|"CLOSE"|"ABSTAIN", size=1.0, stop_loss_pct=..., take_profit_pct=..., reason="...")` or `None`.
  Every BUY/SELL needs `stop_loss_pct`. Available: `np` (a curated numpy subset), `math`, `statistics`, `rng.uniform()` (seeded).
- The code runs in an isolated worker with no files, network, secrets or processes, and sees only bars up to now. A restricted subset
  of Python is enforced; the policy error tells you what is not allowed.
- First call `test_strategy` (`code_file`, `instrument`): it checks the policy and causality and runs a quick simulation on development
  data only, and records nothing. Then register a hypothesis with `{"label": "...", "code_file": "<name>.py", "params": {...}}`.
- Improve a strategy by editing the file (git keeps the versions) and registering the change as a new hypothesis; the code hash is part of
  every experiment, so results are always traceable to the exact code.

Rules: results are simulations, not realized revenue. If something looks wrong (shortfall above zero, drawdown halts, data gaps),
`halt` with a reason; only the operator resumes. Never put credentials, account details or secrets into strategies or lessons.
