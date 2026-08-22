---
name: wedding-film
description: Orchestrate an explicit local Wedding Film Project Workspace. Use for requests to initialize, inspect, continue, validate, or render a wedding movie pipeline; consult CLI status instead of inferring pipeline state.
---

# Wedding Film

Use the `wedding-film` CLI as the sole authority for workspace validation and pipeline state.

## Workflow

1. Resolve one explicit Project Workspace path from the request. If no path is available, ask the user for it; workspace selection is a privacy boundary.
2. Consult the current CLI surface with `uv run wedding-film --help` and the relevant subcommand help.
3. Run `uv run wedding-film --project <workspace> status --json`. Read its states, reason codes, warnings, artifacts, and safe next commands directly.
4. For an initialization request, initialize only when status reports an uninitialized workspace and the destination is explicit. Run `uv run wedding-film --project <workspace> project init`, then rerun JSON status.
5. For inspection, report the JSON facts without probing files independently.
6. For continuation, choose one command that status currently marks safe, confirm it exists in CLI help, and preserve Semantic Catalog → Story → Script → Storyboard → Rough Cut order. Rerun JSON status after the command.

Pause for user confirmation before provider cost, forced canonical replacement, Corrections, Subject Attributions, or narrative-candidate adoption. Treat exit `0` as success or valid reuse, `1` as invalid input or preflight failure, `2` as partial work or a budget stop, and `130` as interruption.

## Report

Report the explicit workspace, exact command, changed canonical artifact (if any), warnings or partial failures, current layer states, and one next gate. Keep credential values and private Material contents out of the report.
