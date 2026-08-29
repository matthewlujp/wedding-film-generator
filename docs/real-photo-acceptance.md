# Real-Photo Acceptance Runbook

This is a human runbook, not an automated test. It describes how a user opts a real
wedding photo set into the pipeline and how to judge the resulting Rough Cut good
enough to keep. The automated fixture in `tests/test_end_to_end_mvp.py` proves the
pipeline machinery works; only a human can accept a real film.

## 1. Opt in explicitly

Point `--project` at a real Project Workspace outside the repository (or under the
`.gitignore`d `projects/` directory) and place real photos under its `materials/`
directory yourself. `project init` never creates or touches `materials/`, and
`catalog scan` never modifies Original Assets. Nothing in this pipeline calls a paid
provider unless a real `vision`/`narrative` adapter is configured in `project.yaml`
and a command is run without `--dry-run`.

## 2. Small cost-approved pilot before scaling up

Before spending budget on the full photo set:

1. Configure the real Vision/Narrative adapters and confirm credentials with
   `status --json` (`prerequisites.credentials`).
2. Run `catalog analyze-batch --dry-run` and read `estimated_cost_usd` and the
   selected asset count.
3. Get budget approval for that estimate, then run a small pilot: pass
   `--asset-id` for a handful of representative photos (5-10), or a low
   `--max-assets`/`--max-estimated-usd`, and inspect the resulting Inferences with
   `catalog list --json` / `catalog show`.
4. Only after the pilot's cost and output quality are acceptable, run
   `analyze-batch` again without an asset/budget cap to cover the remaining
   selected 60-80 photos.

## 3. Target for the final Storyboard

Aim the adopted `storyboard.yaml` at roughly 5 minutes of runtime, i.e. about 7,200
frames at the fixed 24 fps output contract (`storyboard validate --json` reports
`document.total_frames`). Treat this as a target to steer toward while authoring
Story/Script content and reviewing generated Storyboard candidates, not a hard gate
the CLI enforces.

## 4. Required human inspection before acceptance

Render the Rough Cut (`render rough-cut`) and watch it end to end. Do not accept a
render on `status`/`validate` passing alone - those confirm structural correctness,
not editorial quality. Explicitly check:

- **Asset choice** - are the selected photos the ones that should represent each
  moment; are any mis-attributed or low-quality photos included?
- **Order** - does the sequence follow the intended narrative arc from
  `story.md`?
- **Pacing** - do any photos or cards linger too long or flash by too quickly?
- **Narrative flow** - do cards and captions read coherently alongside the photos
  around them?
- **Text readability** - is card and caption text legible at normal viewing size
  and free of overflow or wrapping problems (`storyboard validate` only reports
  structural text-fit failures, not subjective readability)?

Only treat the Rough Cut as accepted once a human has confirmed all five points
above. If anything is off, correct the upstream layer (Story, Script, Storyboard,
or a Correction/Subject Attribution) rather than editing the rendered MP4 directly,
and rerender.
