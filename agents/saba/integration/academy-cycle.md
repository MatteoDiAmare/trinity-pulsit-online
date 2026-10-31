---
description: One autonomous Academy research cycle (budgeted, evidence-linked, no live trading)
allowed-tools: Bash(python -m market_data.academy_bridge:*), Read
---
PROPOSED command file for Saba's operational repository (`saba-pulsit-online`, `.claude/commands/academy-cycle.md`).
It is not installed or verified in the running Trinity agent; adapt and test it there.

Run one research cycle. Use ONLY the Academy bridge, one JSON object per call on stdin, e.g.
`echo '{"command":"status"}' | python -m market_data.academy_bridge --root "$SABA_MARKET_DATA_DIR" --strategies-dir strategies`

1. `status`. If halted, stop and report why. Read `research_notebook.md` (call `notebook` with `"write": true`).
2. `start_cycle`. If it is denied (cycle budget or halt), end the turn and say so. Do not retry.
3. `propose_next` and `search_lessons`: continue open hypotheses and avoid dead ends. If there is nothing worth testing, think of a new
   hypothesis (a new strategy spec, a variant via `variants`, another market) and `register_hypothesis` BEFORE running it:
   state what you expect, the plan (experiments x instruments) and a trial budget.
4. `run_hypothesis` for at most the experiments the cycle still has. Never exceed the budget; the bridge enforces it anyway.
5. Read the `analysis`. Record what you learned with `add_lesson`, always citing the experiment ids as evidence:
   `refuted` for clear negatives, `candidate` for validation-only positives, `inconclusive` when trades are too few.
   A strategy that exceeds the luck benchmark in a market may get ONE `final_test`; the result decides `supported` (needs a positive consumed
   final test). Never try to repeat a final test; the protection will refuse.
6. `conclude_hypothesis` when settled, then `finish_cycle`. End with a short report: what was tested, what was learned, what is next.

Rules: results are simulations, not realized revenue. No live orders exist. Never claim an edge from validation data alone. If something looks
wrong (drawdown, shortfall > 0, data gaps), `halt` with a reason; only the operator resumes.
