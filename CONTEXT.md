# Wedding Movie Production Context

## Glossary

### Original Asset

A unique source-photo content supplied by the user under a project's Materials.
Byte-identical copies are the same Original Asset; the pipeline must never
modify, rename, or reorganize the files that contain it.

### Materials

The user-managed, read-only input area for a wedding-movie project. It contains
the photo files and folders that the pipeline treats as Original Assets.

### Project Workspace

The user-private local working area for one wedding-movie project. It contains
Materials, canonical project sources, operational records, and rebuildable
outputs while preserving the boundaries between them.

### Asset Locator

A project-relative location under Materials where an Original Asset was
discovered. An Original Asset may have multiple Asset Locators, but a locator
is not its identity.

### Semantic Catalog

The canonical, inspectable current-state metadata collection for Original
Assets, with one record per asset. It contains normalized observed data,
inferred semantic data, and human corrections with their provenance; music,
generated media, and render artifacts are outside its scope.

### Observation

A normalized fact obtained directly and deterministically from an Original
Asset, such as its dimensions or recorded capture time.

### Inference

A semantic claim produced by analyzing an Original Asset. An Inference retains
its provenance and a review-prioritization confidence score.

### Analysis Input

A deterministic derivative representation of an Original Asset supplied to a
vision adapter for semantic analysis. It does not replace or modify the
Original Asset, and its derivation is part of the resulting provenance.

### Analysis Run

The inspectable record of an enrichment execution, including its attempts,
outcomes, resource usage, and retry decisions. It is operational history rather
than canonical asset metadata; the Semantic Catalog retains the provenance of
the successful runs that produced its current Observations and Inferences.

### Vision Adapter

A provider-specific analyzer that accepts an Analysis Input and produces either
schema-valid Inference candidates with provenance or a classified failure. A
Vision Adapter does not modify Original Assets or write the Semantic Catalog.

### Participant

A person the user identifies as relevant to a wedding-movie project. A
Participant has a stable project-scoped identity and may have a display name
and user-authored role; image analysis does not infer Participant identity.

### Principal

A Participant the user marks as a central subject of the wedding movie.

### Subject Role

A generic, visually supported category for a person shown in an Original Asset,
such as couple, family, guest, or officiant. A Subject Role is an Inference and
does not establish Participant identity.

### Subject Attribution

A human-authored claim that a Participant appears in an Original Asset. Subject
Attributions retain provenance in the Semantic Catalog and are never produced
by a Vision Adapter.

### Correction

A human-authored replacement or suppression of a catalog value. A Correction
takes precedence when the catalog is consumed without erasing the earlier
Observation or Inference.

### Interview

The conversational phase, between Semantic Catalog and Story, that captures what
the couple says about themselves, their wedding, and their wishes for the movie
that no photo can supply. It produces an Interview Transcript and an Interview
Brief and is conducted by the agent, not the CLI.

### Interview Transcript

The verbatim, append-only record of the Interview, in the couple's own words. It
is a human-authored primary source, never rewritten or summarized in place.

### Interview Brief

The structured extraction of the Interview Transcript that Story, Script, and
Storyboard generation consume. Its required sections (couple, wedding, film,
constraints) must each be answered or explicitly recorded as skipped before
generation may proceed; its recommended sections may remain open indefinitely.

### Story

The narrative intent of the wedding movie: its target runtime, emotional arc,
themes, and moments to communicate. `story.md` is its source of truth and
deliberately does not prescribe particular assets or shot timing.

### Story Moment

A stable, named unit of narrative intent within a Story. It describes something
the movie should communicate without selecting an Original Asset or assigning
edit timing.

### Script

The narrative language presented by the movie, including voiceover narration,
text cards, and captions. `script.md` is its source of truth and translates
Story into words, without becoming the concrete edit decision list.

### Script Block

A stable, typed unit of Script text associated with one Story Moment. Its type
is voiceover narration, text card, or caption; its placement and duration are
decided by the Storyboard.

### Storyboard

The concrete, machine-readable composition of the movie: selected assets,
timing, text, transitions, motion, and music. `storyboard.yaml` is its source
of truth.

### Storyboard Item

An ordered visual unit in a Storyboard. It presents either one Original Asset
or one text card for a concrete duration and remains associated with a Story
Moment.

### Narration Cue

A timed placement in a Storyboard of a voiceover-narration Script Block. It is
independent of visual item boundaries and may span multiple Storyboard Items.

### Music Cue

A timed statement in a Storyboard of the intended music for part of the movie.
It may remain unresolved until a concrete audio source is supplied.

### Rough Cut

An MP4 rendered programmatically from a Storyboard using FFmpeg. The same
validated edit sources can be rendered again without reconstructing manual
editing actions, but byte-identical or cross-toolchain-equivalent output is not
part of its contract. It is a generated build artifact, not an editorial
source of truth.
