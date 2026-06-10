# Pruning principles

When the proxy auto-elides content from the wire, the original is archived to
SQLite. The DB is queryable. The proxy is not. So the value of an elision
isn't how much it saves on the wire — it's how reliably a future session can
fish it back when needed.

This file is one of three anchors the model re-reads before classifying
archived content. The other two: `categories.md` (what each category means)
and `examples.md` (worked classification cases).

## Two distinct kinds of compression

1. **Mechanical (the proxy does this automatically).** Size-triggered. Old
   `tool_result` content above a threshold, old `tool_use.input` above a
   smaller threshold. The proxy can't tell what content means — just that
   it's bulky and old. Every elision lands in `archive.jsonl` and the DB.

2. **Model curation (done on demand by the model).** Archived rows arrive as
   `category=unclassified`. The model reviews them and either tags them with
   a real category (`decision`, `requirement`, etc.) so they're findable by
   future queries, or marks them `transient` (the noise tier) — the proxy
   was right to drop, no future value. The DB row stays either way; disk is
   cheap. `recall` filters out `transient` by default.

## What earns its keep

**Whatever a future session would lose by not having access to it.**

Concretely, archive with a meaningful category — not just `transient`:

- A choice made between options, with the reason.
- A user-stated requirement or constraint.
- A non-obvious fact about the codebase, system, or data.
- A failed attempt with the reason it failed.
- Code written or substantially modified, when the diff is the artifact.
- Dialogue from the user that carries tone, judgment, or framing the bare
  code wouldn't capture.

## What does NOT earn its keep

Mark these `transient`:

- Tool outputs that snapshot mutable state (file contents, directory listings,
  search results, command outputs). They re-fetch deterministically;
  re-fetching is more accurate than recalling stale state.
- Bulk reference data pulled to answer one question and unlikely to repeat.
- Planning chatter that doesn't conclude in a decision.
- Acknowledgments — "thanks", "go ahead", "sounds good".
- Status updates the next state supersedes.
- Tool inputs whose result is the load-bearing thing (the read call is noise;
  the file content was the answer, and the file is on disk anyway).

## The whitespace question

Unsure? Ask: *would a future session, months from now, regret not being able
to find this?* If yes → archive with a real category. If no → `transient`.
The default is `transient`; categories have to be earned.

False-positive cost (archiving noise as a real category): clutters queries.
Recoverable — reclassify.
False-negative cost (real content marked `transient`): future sessions can't
find it. The DB row exists but defaults exclude it from recall.

Lean slightly toward categorizing. Reclassification is cheap; rediscovery
isn't.

## When to classify

Pull-mode. The proxy doesn't ask the model to classify; the model triggers
classification itself. The trigger is one of:

1. A periodic reminder fires. Default response: take a moment to triage.
2. The model finishes a substantive piece of work and steps back. Before
   moving on, scan for items worth archiving (typically a small number per
   session beat).
3. Mid-session pruning is happening and content the proxy is about to elide
   looks load-bearing. Pre-archive before mechanical prune touches it.

## Pinned context

A small set of items are `pinned` in the session AND in the DB. Pinned items
are never auto-elided — the proxy skips them. Use sparingly. Reserve for
context the model needs to retain to use the system itself:

- The fact that this proxy + DB system exists and how to query it.
- Active project state that the model needs to operate against this turn.
- Stable facts about the user's environment that wouldn't survive otherwise.

Pinning is a stronger commitment than archiving. Pinned items consume context
budget every turn; archived items don't. Pin only what's needed *every turn*.

## Project scope

Each archive row carries a `project` tag derived from the working directory:
- `projects/<name>/...` → `project=<name>`
- anywhere else → `project=misc`

`recall` defaults to scoping queries to the current project. When current is
`misc`, queries default to all projects (since misc is a black hole; you
generally don't want to recall to misc).

Cross-project recall is opt-in: `--project any` or `--project <other>`.
