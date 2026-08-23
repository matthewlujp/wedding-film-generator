# Wedding Film

A local, inspectable wedding-film production pipeline. The MVP preserves separate sources of truth from immutable Original Assets through the Semantic Catalog, Story, Script, Storyboard, and rebuildable Rough Cut.

## Development

Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/) are required. FFmpeg and ffprobe are reported as explicit rendering prerequisites.

```sh
uv sync
uv run wedding-film --project projects/example project init
mkdir -p projects/example/materials
uv run wedding-film --project projects/example catalog scan
uv run wedding-film --project projects/example status
uv run wedding-film --project projects/example status --json
```

Initialization accepts a nonexistent destination or an existing empty directory. It creates `project.yaml`, `participants.yaml`, `runs/analysis/`, `.work/candidates/`, and `renders/`. It deliberately does not create `materials/`; that directory and every Original Asset inside it remain user-managed.

`catalog scan` recursively reads regular files in `materials/`, rejects symlinks, and writes a deterministic content-addressed `catalog.jsonl`. Byte-identical files share one record with multiple project-relative locators. Rescans preserve valid enrichment for unchanged content and atomically publish only a complete, source-integrity-checked catalog; Original Assets are never changed.

Real Project Workspaces under `projects/` are ignored by Git. Keep private workspaces outside the repository or under that ignored directory.

`status --json` is the machine-readable state interface. Plain status renders the same facts. Both derive state from the current workspace and process environment without a status database.
