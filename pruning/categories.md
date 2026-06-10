# Categories

The taxonomy used to tag archived content. Small on purpose. Each new category
fragments search and makes the model less sure which one to query, so the
rule is: **only add a category when an existing one genuinely doesn't fit.**

Categories live in the DB as their own table, not as an enum. Adding a
category is an explicit step (`memory.py add-category <name> "<description>"`).
That friction keeps the taxonomy from drifting into many lightly-overlapping
tags.

## The starting six

### `decision`

A choice made between options, with the reason. The reason matters more than
the choice — the choice can often be re-derived from context, but the reason
typically can't.

Use when: "we picked X because Y", "decided not to do Z", "switched from A
to B after seeing that C". Even small decisions count if the reason was
non-obvious and the decision will hold across sessions.

### `requirement`

A constraint, must-have, or non-negotiable stated by the user. Distinct from
`decision` because requirements are inputs to the work, not outputs.

Use when: "must work offline", "no third-party API calls", "only Python
3.10+", "the model must never call the user by name", "keep the diff under
100 lines".

A requirement that gets relaxed later doesn't get re-categorized — it stays
`requirement` (the historical fact that it was once a requirement is part of
the record). Pair the relaxation with a `decision` entry that supersedes it.

### `discovery`

A non-obvious fact about the codebase, system, environment, data, or world
that took work to learn and that future sessions would otherwise have to
re-derive.

Use when: "the auth middleware silently swallows certain errors", "the API
ships a header as multiple lines, not comma-joined", "a particular vendor's
sandbox blocks outbound HTTP".

The litmus test: would you be tempted to write a comment like "note: X
is non-obvious because Y" — that's a `discovery`.

### `failed_attempt`

Something tried that didn't work, with the reason. Distinct from `decision`
because the work was started before the verdict was reached.

Use when: "tried using extension X but it's not in the shipped binary",
"tried path Y but the platform restricts it". A failure paired with a real
reason is gold; a failure without a reason is `transient`.

Often the best follow-up category. If a `failed_attempt` led to a
`decision`, archive both with the same archived-at timestamp so they
cluster.

### `code_artifact`

Code written or substantially modified, when the diff itself is the artifact
and a future session would need it. Not for code that was read (that's still
on disk and can be re-read).

Use when: a substantial implementation, a tricky refactor, a generated
script. Skip when: a typo fix, a one-line change, a delete.

The body of the archive row is the code (or a unified diff). Include enough
context that the snippet stands alone — file path, language, what it does
at a glance.

### `user_dialogue`

A user message worth keeping because of *tone*, *judgment*, or *framing*,
not because of the literal content. The raw text is the artifact.

Use when: explicit statements of preference about scope, taste, or working
style; signals about what to optimize for; framing that shapes downstream
work.

Captures the user's mental model and preferences in a way that mechanical
code-and-decision logs don't. Use sparingly — a project-level user manual
isn't this category.

## The noise tier

### `transient`

The proxy was right to drop it. No future value. The row stays in the DB
(disk is cheap) but `recall` filters it out by default.

This is the default category for anything archived without explicit thought.
Most rows will end up here, and that's fine. Tool outputs, bulk reference
data, planning chatter without conclusion, status updates, acknowledgments.

If unsure whether to archive something as `transient` or `discovery`,
the question is: *does someone need to know this fact later?* If yes —
`discovery`. If "this fact is already on disk in some file that can be
re-read" — `transient`.

## Sticky tag

### `pinned`

Items that should never be auto-elided by the proxy. Used in addition to
their semantic category — a row can be `category=requirement, pinned=true`.

Implemented as a boolean column on `pruned_content`, not a category, so
search doesn't get cluttered.

## Adding a new category

Before adding, check whether the existing six legitimately don't fit.
Failure modes to resist:

- **Splitting `discovery` by domain.** "Codebase discovery", "API discovery",
  "data discovery" — they're all `discovery`. The `project` column already
  partitions by domain.
- **Over-categorizing user dialogue.** "Praise", "frustration", "preference"
  — all `user_dialogue`. The body text already carries the affect.
- **Categorizing by content shape rather than meaning.** "It's a JSON
  output" isn't a category; "it's a decision and the JSON happens to be the
  artifact" is.

If a new category is genuinely needed:
```
python memory.py add-category <name> "<one-line description>"
```
Then add it to this file with the same examples-first format.
