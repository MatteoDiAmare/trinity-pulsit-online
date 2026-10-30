# Saba: Trinity tool bridge (research only)

This bridge is a subprocess interface for a Trinity agent, **not an automatically
registered or deployed Trinity tool**. It uses archived GC=F futures candles only.
No broker orders, CFD fills, live quotes or autonomous trading are implemented.

From `agents/saba` with the Python environment activated:

```bash
echo '{"command":"analyze","symbol":"GC=F"}' | python -m market_data.trinity_bridge --journal saba_experiences.sqlite
```

For a decision, copy the exact `timeframes["1m"]["completed_at_utc"]` from
the analysis response into `evidence_completed_at_utc`. Example payload:

```json
{
  "command": "decide",
  "symbol": "GC=F",
  "evidence_completed_at_utc": "REPLACE_WITH_ACTUAL_TIMESTAMP",
  "side": "ABSTAIN",
  "rationale": "Archived futures data lacks executable CFD spread and event risk",
  "invalidation_price": null,
  "expected_holding_minutes": 20,
  "confidence": 0.3
}
```

Run with: `python -m market_data.trinity_bridge --journal saba_experiences.sqlite`
and supply the JSON object on stdin. The tool recomputes archive evidence, applies
the Stockholm time-only gate and appends an auditable SQLite event. It returns
`execution: DISABLED` on every successful decision. A `RESEARCH_CANDIDATE`
is **not** permission to trade.

## Trinity integration contract

Register a restricted local subprocess tool in the running Trinity deployment
that executes only this module with a fixed working directory and Python interpreter.
Pass a validated JSON object via stdin; consume JSON stdout and handle nonzero exit
codes. Do not expose an arbitrary shell command to the LLM. Give the agent two
allowed operations: `analyze` and `decide`. Instruct it to inspect the
`uncertainties` and record `ABSTAIN` when evidence is inadequate. Persist the
journal on a mounted data volume, not an ephemeral container filesystem.

Actual Trinity tool registration must be adapted and tested against the running
Trinity version and deployment configuration; this repository change alone does
not register a tool in the agent runtime.

Next: real instrument bid/ask data, simulated execution and outcome events,
independent order risk governor, and empirical validation.
