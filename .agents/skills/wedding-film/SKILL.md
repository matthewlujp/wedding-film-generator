---
name: wedding-film
description: Orchestrate an explicit local Wedding Film Project Workspace. Use for requests to initialize, inspect, continue, analyze, correct, generate, validate, or render a wedding movie pipeline; consult CLI status instead of inferring pipeline state.
---

# Wedding Film

Use the `wedding-film` CLI as the sole authority for workspace validation and pipeline state. Never
infer readiness from scattered files, and never reimplement its validation or state logic yourself.

When the `interview` layer needs attention, follow `interview.md` for how to run the conversation
itself; this file only covers where that layer sits in the pipeline and what it gates.

## Workflow

1. Resolve one explicit Project Workspace path from the request. If no path is available, ask the
   user for it; workspace selection is a privacy boundary.
2. Consult the current CLI surface with `uv run wedding-film --help` and the relevant subcommand
   `--help` before choosing a command. Command names, flags, and exit codes below are a map, not a
   contract — re-check `--help` if anything looks stale.
3. Run `uv run wedding-film --project <workspace> status --json`. Read its `state`, `layers`,
   `prerequisites`, `warnings`, and `safe_next_commands` directly.
4. For an initialization request, initialize only when status reports an uninitialized workspace
   (`project_configuration` state `missing`) and the destination is explicit. Run
   `project init`, then rerun JSON status.
5. For inspection, report the JSON facts without probing files independently.
6. For continuation, choose one command that keeps Materials → Semantic Catalog → Interview →
   Story → Script → Storyboard → Rough Cut in order, run it, and rerun JSON status afterward to
   confirm the resulting gate. Never skip a layer even if a later one looks generatable.

### Choosing the next command

`status --json` enumerates safe read-only/replay-safe commands directly in `safe_next_commands`
(`project init`, `catalog scan`, `* validate`) — prefer those verbatim when present. It does not
enumerate narrative `generate`/`adopt` steps, because generating a candidate never touches a
canonical file and is always safe to run directly once its upstream layer is `ready` or
`complete-with-warnings`:

- Materials missing → tell the user to place files under `materials/` (user-managed, never created
  or written by this skill).
- `semantic_catalog` missing/stale and `materials` usable → `catalog scan`, then `catalog extract`.
- Vision coverage: `status` does not track per-asset analysis progress. Check with
  `catalog list --json` (`inference_count` per asset) before deciding whether `catalog
  analyze`/`analyze-batch` is still needed.
- `interview` missing/stale and `semantic_catalog` usable → run the Interview phase described in
  `interview.md`. This is a conversation, not a single command: there is no `interview generate`.
  The skill reads and writes `interview/transcript.md` and `interview/brief.yaml` directly, and
  only calls `interview validate --json` to check readiness. Required sections
  (`couple`, `wedding`, `film`, `constraints`) must be answered or explicitly skipped before
  `story`/`script`/`storyboard` can generate; recommended sections (`people`, `chronology`,
  `texture`) may stay open indefinitely — that is expected, not a defect.
- `story`/`script`/`storyboard` missing or stale and its upstream is usable → run that layer's
  `generate` directly (candidate-only), then pause before `adopt`.
- `rough_cut` missing/stale and `storyboard` usable → `render rough-cut` is replay-safe and may run
  directly.

## Pause points (always confirm with the user first)

- **Provider cost** — `catalog analyze` / `catalog analyze-batch` without `--dry-run`. Run
  `--dry-run` first and report `estimated_cost_usd` and asset count; only run for real after the
  user confirms.
- **Forced canonical replacement** — any `--force` flag on `story|script|storyboard adopt` (used
  when a regenerated candidate differs from the adopted file).
- **Correction** — `catalog correct set|remove`.
- **Subject Attribution** — `catalog correct --target /subject_attributions` and any `participant
  add|update|remove`.
- **Narrative-candidate adoption** — `story|script|storyboard adopt` (even without `--force`); it
  replaces the layer's canonical source of truth.
- **Interview section skip** — recording a required section (`couple`, `wedding`, `film`,
  `constraints`) as skipped in `interview/brief.yaml`. This is a deliberate waiver of a safety
  gate, not a routine edit — confirm explicitly, especially for `constraints`, since an unasked
  question there (a deceased relative, an estranged parent) can surface irreversibly in the
  finished film. See `interview.md` for how and when to ask before proposing a skip.

Read-only inspection (`status`, `validate`, `interview validate`, `catalog list|show`, `participant
list`), `--dry-run` planning, and `render rough-cut` (deterministic and rebuildable from
`storyboard.yaml`) may run without pausing. Writing to `interview/transcript.md` and answered
(non-skipped) fields in `interview/brief.yaml` also does not require pausing — recording what the
couple said is not a canonical-file replacement and carries no risk beyond a follow-up question.

Treat exit `0` as success or valid reuse, `1` as invalid input or preflight failure, `2` as partial
work or a budget stop, and `130` as interruption. On `1`, stop and report — do not attempt the next
layer over an invalid one. On `2`, report exactly what completed and what remains before proposing
a retry.

## Report

Report the explicit workspace, exact command, changed canonical artifact (if any), warnings or
partial failures, provider usage and estimated cost when a provider command ran, current layer
states from the rerun status, and one next gate. When the interview layer is not yet `ready`,
report which required sections remain open or skipped, and how many recommended-section gaps
`interview.md`'s gap-finding step surfaced, without pretending the count is a completion criterion.
Keep credential values and private Material contents — and anything the couple asked to keep out of
the film, or out of each other's hearing — out of the report.
