# Wedding Film

A local, inspectable wedding-film production pipeline. The MVP preserves separate sources of truth from immutable Original Assets through the Semantic Catalog, Story, Script, Storyboard, and rebuildable Rough Cut.

## Development

Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/) are required. FFmpeg and ffprobe are reported as explicit rendering prerequisites.

```sh
uv sync
uv run wedding-film --project projects/example project init
mkdir -p projects/example/materials
uv run wedding-film --project projects/example catalog scan
uv run wedding-film --project projects/example catalog extract
uv run wedding-film --project projects/example catalog analyze --asset-id sha256:<digest>
uv run wedding-film --project projects/example status
uv run wedding-film --project projects/example status --json
```

Initialization accepts a nonexistent destination or an existing empty directory. It creates `project.yaml`, `participants.yaml`, `runs/analysis/`, `.work/candidates/`, and `renders/`. It deliberately does not create `materials/`; that directory and every Original Asset inside it remain user-managed.

`catalog scan` recursively reads regular files in `materials/`, rejects symlinks, and writes a deterministic content-addressed `catalog.jsonl`. Byte-identical files share one record with multiple project-relative locators. Rescans preserve valid enrichment for unchanged content and atomically publish only a complete, source-integrity-checked catalog; Original Assets are never changed.

`catalog extract` decodes each cataloged image locally and checkpoints allowlisted media,
dimension, orientation, capture-time, camera, and GPS Observations. Malformed embedded tags
become warnings in append-only `runs/analysis/*.jsonl`; decode failures return exit code 2 after
preserving successful asset checkpoints. Identical successful extraction contracts are reused.

`catalog analyze --asset-id` analyzes exactly one cataloged image through the configured Vision
Adapter. The offline `fake` adapter is deterministic. Only a metadata-free, oriented, sRGB JPEG
Analysis Input crosses the adapter boundary; the temporary derivative is deleted afterward. A
complete candidate atomically replaces the adapter-owned Inferences, while invalid or refused
candidates leave the Semantic Catalog unchanged. Identical successful contracts are reused.

Real Project Workspaces under `projects/` are ignored by Git. Keep private workspaces outside the repository or under that ignored directory.

`status --json` is the machine-readable state interface. Plain status renders the same facts. Both derive state from the current workspace and process environment without a status database.

Validate an authored Storyboard in isolation with `storyboard validate`; use top-level `validate`
to require the complete Story → Script → Storyboard chain. Both commands accept `--json` and
`--strict`. Storyboard editorial findings such as stale input hashes, narration/music work not yet
rendered, unused upstream objects, and material runtime deviation are warnings by default and
errors in strict mode.
Structural, reference-integrity, transition, cue-timeline, and frame-arithmetic failures always
return exit code 1.

See `docs/real-photo-acceptance.md` for the opt-in runbook covering a real photo set: the cost-approved pilot before scaling up, the ~5 minute/7,200 frame Storyboard target, and the required human inspection before accepting a Rough Cut.
