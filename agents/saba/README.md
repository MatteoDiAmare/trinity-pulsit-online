# Saba — Academy code for the existing Trinity agent

**There is only one Saba.** The agent is deployed in Trinity from her own repository `MatteoDiAmare/saba-pulsit-online` (onboarding completed 2026-10-12). The operator already communicates with her in Trinity. This directory contains research and Academy modules intended to be integrated with **that existing agent**. It is not a second deployment or a second Saba.

## Mandate
Saba autonomously researches, creates and tests strategies, compares and selects markets, and learns from evidence. Gold (GC=F) is only the first replay example. The intended scope includes all accessible asset classes, with leverage allowed subject to verified account/broker safeguards. Every opened position requires stop-loss; the operator must not be put into debt. Neither stop-loss nor margin close-out alone guarantees this.

Two account responsibilities are established: an account managed by Heidi and a separate account that Saba manages with independent discretion. Research and permitted decisions in her own account should not depend on per-experiment operator approvals. Actual account setup, broker connectivity and live order protection are not confirmed by this code.

## Academy
`market_data/academy/` contains historical replay, strategies, experiments, evaluation, ledger and export. Academy is a tool for Saba, not an autonomous replacement agent. Research evidence belongs in the experiment registry/ledger; durable hypotheses and lessons should connect to Saba's persistent memory.

The existing Academy implementation and its tests do **not** prove integration with the running Trinity agent. Integration, multi-asset support, autonomous research cycles and live broker execution remain separate engineering milestones. `trading.pulsit.online` is a deferred control room, not the agent's communication interface.

## Existing foundation
- `CLAUDE.md`: research mission and engineering principles for this code directory.
- `template.yaml`: local template metadata; do not mistake it for the operational configuration of the already-deployed agent.
- `market_data/academy/`: replay and ledger implementation.
- `tests/`: test suite.

Run tests from `agents/saba`: `python -m unittest discover -s tests -v`.

No credentials or account details belong in this repository.
