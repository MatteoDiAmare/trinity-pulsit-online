---
description: One autonomous Academy research cycle (budgeted, evidence-linked, simulation only)
allowed-tools: Bash(~/.local/share/saba/academy-bridge:*), Read, Write, Edit, Glob, Grep
---
Run one Academy research cycle. This is simulation research on historical data: there is no broker, no order and no money involved.
You choose the direction, instruments and strategies yourself; no approval is needed for research. The budget and the integrity rules
are enforced by the bridge, not by this text.

Send `{"command": "help"}` once to get the exact JSON of every command (errors also include the expected usage). `start_cycle` returns a
briefing (status, notebook, ranking). `register_hypothesis` with `run_in_cycle` registers and runs in one call; `add_lesson` with
`conclude_hypothesis` closes the hypothesis in the same call. A task may use up to 300 turns and 60 minutes (verified runtime limits), and experiments are accepted only within
30 minutes after `start_cycle` (see `status` for the budget): plan the cycle, keep it focused and always end with `finish_cycle`.

Call the bridge ONLY like this (one JSON object per call, answer is one JSON object):

~/.local/share/saba/academy-bridge <<'JSON'
{"command": "status"}
JSON

Procedure
1. `status`. If `halted` is set, stop and report the reason. `notebook` (with `"write": true`) shows your open hypotheses, findings,
   dead ends and which test windows are already consumed. Read it before choosing what to do.
2. `start_cycle`. If denied (halt or cycle budget), end the turn and say so. Do not retry.
3. Decide what to learn (start from `forward_report` for frozen candidates and from your notebook). Use `search_lessons` and `propose_next`; do not repeat dead ends without a new reason. Ideas can be a JSON
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

Studies, walk-forward, frozen candidates and your own forecasts (new)
- Prefer a STUDY to hand-made variants. One pre-registered grid (at most 60 cells; each combination of parameters is a cell) is evaluated as ONE piece of research:
  walk-forward over rolling windows, on all the markets you list, with the parameters of each window chosen from its training part only and tested on the unseen part after it.
  Send `{"command": "run_study", "cycle_id": N, "study": {...}}` (see `help` for the exact JSON; typical daily geometry: `train_bars` 750, `test_bars` 125; use GOLD, EURUSD, SP500 unless you
  have a reason not to). A study costs ONE experiment of the cycle budget and takes about a minute. For your own code use `"code_file": "name.py"` and grid paths that start with `params.`.
- Read the result honestly. `wfo` is the out-of-sample evidence of the whole search procedure; `dsr` is the probability that it beats what the best of N zero-edge trials would reach (N includes
  earlier trials on the same markets); `pbo` is the probability of backtest overfitting; compare with `benchmarks.buy_and_hold_*`. Verdict `no_evidence` is a valid and valuable result: record it as a
  lesson with the study's `registry_id` as evidence. Verdict `candidate` only means worth freezing and tracking forward, not proven. With three markets and a few years of out-of-sample days only
  fairly strong effects can be detected; do not loosen the criteria or re-run the same idea with tweaked grids hoping for a pass (each cell is counted).
- To follow a candidate on new data, `freeze_candidate` (plan, markets, interval, optional `study_sha256`, rules `min_trades`/`min_days`) with YOUR FORECAST: `p_positive` (your probability that the
  forward expectancy per trade is positive), `expected_hit_rate`, `expected_expectancy`. Be calibrated, not hopeful: forecasts are scored (Brier score against a coin flip) and are the measure of whether your
  judgement improves. A frozen plan cannot be edited. `forward_report` re-evaluates every frozen candidate on bars that arrived after its cutoff and shows verdicts and your forecast score; verdicts need
  time (new daily bars), so check them at the start of a cycle and write what you learn as lessons.
- Never use a final test to choose between parameters; studies and frozen candidates exist so that the final-test windows can stay scarce.

Your own Python strategies
- Write `strategies/code/<name>.py` defining exactly one `class Strategy` with `__init__(self, params)` and `on_bar(self, view, position)`.
  `view` has read-only numpy arrays `open high low close volume`, `n`, `total` and `timestamp` (`.hour .weekday() .isoformat()`).
  Return `Decision("BUY"|"SELL"|"CLOSE"|"ABSTAIN", size=1.0, stop_loss_pct=..., take_profit_pct=..., reason="...")` or `None`.
  Every BUY/SELL needs `stop_loss_pct`. Available: `np` (a curated numpy subset), `math`, `statistics`, `rng.uniform()` (seeded).
- The code runs in an isolated worker with no files, network, secrets or processes, and sees only bars up to now. A restricted subset
  of Python is enforced; the policy error tells you what is not allowed.
- First call `test_strategy` (`code_file`, `instrument`): it checks the policy and causality and runs a quick simulation on the TRAIN portion
  only, and records nothing. Validation results exist only through registered experiments; every registered experiment is counted as a trial
  and raises the luck benchmark, so tune on train and register deliberately. Then register a hypothesis with `{"label": "...", "code_file": "<name>.py", "params": {...}}`.
- Improve a strategy by editing the file (git keeps the versions) and registering the change as a new hypothesis; the code hash is part of
  every experiment, so results are always traceable to the exact code.

Rules: results are simulations, not realized revenue. If something looks wrong (shortfall above zero, drawdown halts, data gaps),
`halt` with a reason; only the operator resumes. Never put credentials, account details or secrets into strategies or lessons.
