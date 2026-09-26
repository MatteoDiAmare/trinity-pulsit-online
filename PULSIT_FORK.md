# trinity-pulsit-online

This repository is the Pulsit-maintained fork of [Ability AI Trinity](https://github.com/abilityai/trinity).

## Purpose

`trinity-pulsit-online` is the agent platform layer used by pulsit.online. The fork stays connected to Ability AI's upstream repository so Pulsit can review and incorporate future Trinity updates while keeping Pulsit-specific changes isolated and auditable.

## Repository rules

- Keep the upstream Git history intact. Do not rewrite, squash, or replace the inherited Trinity history.
- Keep `LICENSE` and `NOTICE` intact and preserve upstream attribution required by the Apache 2.0 license.
- Treat `abilityai/trinity` as upstream. Pulsit-specific work is added as new commits on top of that history.
- Prefer small, reviewable Pulsit changes so upstream updates remain easy to compare and merge.
- Do not commit credentials, API keys, customer data, private company state, or agent secrets to this public repository.
- Store secrets in approved secret stores or runtime configuration, never in Git.
- Review inherited GitHub Actions, deployment workflows, telemetry, vendor endpoints, and private submodules before enabling them for Pulsit infrastructure.
- Changes to security-sensitive repository configuration require Pulsit ownership/review as defined in `.github/CODEOWNERS`.

## Upstream update flow

The intended relationship is:

```
abilityai/trinity          upstream
        |
        v
trinity-pulsit-online      Pulsit fork
        |
        +-- Pulsit-specific configuration and integrations
```

When upstream changes are available:

1. Fetch or sync the upstream changes.
2. Review the diff and release notes.
3. Run the relevant test and security checks.
4. Merge the upstream changes without rewriting inherited history.
5. Resolve Pulsit-specific conflicts explicitly and document material deviations.

## Scope boundary

This public repository is platform code. Private Pulsit company data, strategy, operational state, budgets, customer information, and agent-specific private configuration belong outside this repository.
