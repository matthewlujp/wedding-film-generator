# Project Principles

This repository implements an AI-assisted wedding movie production pipeline.

## Architecture

The pipeline is:

original assets
→ semantic catalog
→ story
→ script
→ storyboard
→ rendered movie

Never collapse these layers.

## Source of truth

- Original photos are immutable.
- Semantic catalog is the source of truth for asset metadata.
- story.md is the source of truth for narrative intent.
- script.md is the source of truth for narrative text.
- storyboard.yaml is the source of truth for concrete movie composition.

Generated MP4 files are build artifacts.

## Development philosophy

Prefer:
- simple files
- deterministic transformations
- CLI tools
- inspectable intermediate artifacts
- small dependencies
- reproducible rendering

Avoid:
- premature UI
- hidden state
- modifying original photos
- storing important information only in generated folders
- coupling the system to a specific AI provider

## MVP

Do not add Runway, facial recognition, DaVinci integration,
or cloud infrastructure until the basic rough-cut pipeline works.

## Agent skills

### Issue tracker

Issues and specs are tracked in this repository's GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The canonical triage roles use the default label names. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. See `docs/agents/domain.md`.
