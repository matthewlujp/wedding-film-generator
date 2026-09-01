# Interview

How to run the Interview phase: the conversation that fills `interview/brief.yaml` and
`interview/transcript.md` before Story generation. `SKILL.md` covers where this layer sits in the
pipeline and when to trigger it; this file covers how to conduct it. There is no `interview
generate` — you write both files directly with your normal file tools, and only call `wedding-film
--project <workspace> interview validate --json` to check whether the required sections are
satisfied.

## What you produce

- `interview/transcript.md` — the couple's own words, verbatim. Append-only, one Markdown section
  per round, a `**Q**`/`**A**` pair per question. Do not smooth over hesitation, dialect, or a
  reply the couple corrected mid-sentence — that texture is often the most usable material for
  narration later. Never edit an earlier round.
- `interview/brief.yaml` — structured extraction from the transcript, in the schema below. This is
  what Story, Script, and Storyboard generation actually read; the transcript is the source you
  extract it from, not something downstream reads directly.

Required sections — `story`/`script`/`storyboard generate` refuse to run until each is answered or
explicitly recorded under `skipped_sections`:

- `couple` — `partner_a`/`partner_b` (`name`, `called_as`), `first_met`, `relationship_years`,
  `proposal`, `turning_point`
- `wedding` — `date`, `venue`, `ceremony_style`, `guests`, `screening_moment`
- `film` — `target_duration_seconds`, `audience`, `tone_wanted`, `tone_avoided`, `music`
- `constraints` — `forbidden_topics` (list), `excluded_people` (list), `excluded_materials` (list
  of `{description, asset_ids}`), `notes`

Recommended sections — read by generation when present, otherwise silently omitted. They have no
skip mechanism because leaving them open is the normal, expected state, not a waiver:

- `people` — list of `{participant_id, relationship, called_as, anecdotes}`; `participant_id` must
  match an id already in `participants.yaml`
- `chronology` — list of `{period, what_happened}`, for events no photo can show
- `texture` — `catchphrases`, `nicknames`, `inside_jokes`, `speech_habits` (each a list)

A required section is skipped by adding `{section, reason, actor}` under `skipped_sections` instead
of answering it — see the Pause points in `SKILL.md` before doing this, `constraints` especially.

## Before asking anything: find the gaps yourself

Never ask the couple something you can already infer from the workspace. Read, in order:

1. `catalog list --json` — cluster assets by EXIF date. A cluster with no corresponding
   `interview/brief.yaml` `chronology` entry is a strong, concrete question ("42 photos cluster in
   August 2019 — what was happening then?"). Note recurring `setting` and `subject_roles` values
   the transcript hasn't explained yet.
2. `participant list --json` — anyone listed without a `people` entry in the brief is a gap.
3. The current `interview/brief.yaml` — never re-ask a section that already has an answer; if you
   want to deepen or correct one, say so explicitly rather than asking as if the field were empty.
4. `interview validate --json` — the authoritative list of which required sections still block
   generation.

Only questions that survive this pass belong in a round.

## Running a round

Ask three to five questions at once, not one at a time — the couple should never feel interrogated
one drip at a time. Each question:

1. States what you already know and where it came from (a photo cluster, an existing brief field,
   a participant with no `people` entry) — never ask blind when the catalog already has a lead.
2. Offers your best guess as a concrete, falsifiable claim, not an open question. "This looks like
   your honeymoon" is easy to correct with one sentence; "tell us about this trip" makes the couple
   do all the work. Being wrong costs nothing here — a correction is itself an answer.
3. Leaves room for the couple to say "skip this" without pressure.

Session shape:

- **Round 1** opens with something easy to talk about — how they met, an early memory — never with
  `constraints`. Then, in the same round, once the conversation is moving, ask the `constraints`
  questions (forbidden topics, anyone to exclude or keep off-camera, any specific photos to leave
  out). Putting safety questions after a warm-up, but still in round 1, means they get asked early
  without opening the whole conversation on a cold, defensive note.
- Later rounds fill `wedding`, `film`, then work outward into `people`, `chronology`, `texture` for
  as long as the couple is willing to keep going.
- At the end of every round, state in one line how many required-section gaps and how many
  recommended-section gaps remain, and say plainly that stopping now is fine. There is no
  threshold at which the interview is "done" — only a point at which the couple chooses to stop.
  Required-section completeness (answered-or-skipped) is the only thing that blocks generation.

## After each round

Before moving to the next round: append the round's `**Q**`/`**A**` pairs to
`interview/transcript.md`, then update the corresponding fields in `interview/brief.yaml`. Do this
immediately, not at the end of the session — a crash or a context reset should never lose an
answered round. Writing an answered field, or the transcript, does not require pausing for
confirmation (see `SKILL.md`); only a `skipped_sections` entry for a required section does.

## Corrections surfaced during the interview

The couple will sometimes identify someone in a photo, or ask you to exclude a set of photos by
description ("all of the ones from the trip to Kyoto"). Do not write these into `brief.yaml`
as-is:

- A person identified in a photo is a Subject Attribution: resolve the specific `asset_id`s from
  `catalog list`/`catalog show`, then propose `catalog correct set --target /subject_attributions`
  (a pause point in `SKILL.md`) — confirm with the couple before running it.
- Photos to exclude by description: search the catalog for matching `asset_id`s, confirm the match
  with the couple, then record both the description and the resolved `asset_id`s under
  `constraints.excluded_materials` in `brief.yaml`. Keep the description even after resolving the
  ids — it is the only record of *why* those photos are excluded, and `storyboard generate` and
  `storyboard validate` both refuse to include an excluded `asset_id` regardless of what the
  Storyboard candidate proposes.

## Language

Conduct the conversation, and write the transcript and every prose value in `brief.yaml`, in the
couple's own language. Keep `brief.yaml`'s keys and structure as specified above regardless of
conversation language — only the values are localized.
