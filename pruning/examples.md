# Worked classification examples

How to think about classifying actual content. Each example is a piece of
conversational content with the call to make and the *reason*.

When unsure, the framework is:
1. Is it noise (mutable state, ack, planning) → `transient`.
2. Is it a load-bearing fact someone would have to re-derive otherwise → real
   category from `categories.md`.
3. Multiple categories seem to fit → pick the one that best answers "what
   would I query on to find this months from now?"

---

## Example 1: A shell tool_result

```
Bash: `ls /some/dir/`
Returns: file1  file2  subdir/  README.md  ...
```

**Verdict: `transient`.**

It's a directory snapshot. Mutable, re-runnable, future sessions will re-list
if they need to know what's there now. Saving a stale listing has negative
value — recalling it later could be misleading.

The size threshold doesn't really matter; even at 50KB, it'd still be
`transient`.

## Example 2: A WebFetch tool_result on an API doc page

```
WebFetch: docs.example.com/v1/messages -> the entire reference page
```

**Verdict: `transient` — usually. `discovery` — sometimes.**

The page itself is re-fetchable, so by default `transient`. But if the
discovery from reading it was *"this header ships as multiple lines, not
comma-joined"*, that fact is a `discovery` and should be archived
separately as a short text note. The original 80KB page body stays
`transient`; the one extracted fact gets its own `discovery` row.

This is a general pattern: large tool_results get marked `transient`, and
the load-bearing 1-2 sentences from them get hand-written into their own
`discovery` (or `decision`) rows.

## Example 3: A user message saying "go for it"

```
User: "sounds good, go for it"
```

**Verdict: `transient`.**

Ack. No load-bearing content. Future sessions reconstruct "the user approved"
by the fact that the work then proceeded — no need for the literal text.

If the user had said "go for it, but stay under 200 lines" — *that* would be
`requirement`, because the constraint is the load-bearing part.

## Example 4: A user message stating a project goal

```
User: "I want a system that does X, lets me Y between turns, and ships to
       non-technical users out of the box."
```

**Verdict: `requirement`. Possibly also `pinned`.**

Three constraints stated explicitly. These need to survive every future
planning session, so `requirement` is the right category, and if the project
will span sessions, `pinned=true` so it can't get auto-elided.

## Example 5: An Agent dispatch return summary

```
Agent("research topic") returns 8KB summary:
"...the most-leveraged cluster is X. Methodology: WebSearch site-restricted
 queries... particular vendor's API was unreachable, used Y instead..."
```

**Verdict: `discovery` for the methodology nuggets; `transient` for the
rest of the summary.**

The agent dispatch is expensive and non-idempotent (re-running won't
reproduce the exact summary), so the *findings* are worth keeping. But not
the whole 8KB blob — extract the 2-3 sentence findings as their own
`discovery` rows, mark the original `transient`.

This is the same pattern as Example 2: split bulk into extractable insights
+ mark the bulk noise.

## Example 6: A failed implementation attempt

```
Notes: "tried using extension X to index content but the macOS-shipped
        binary doesn't include it. Falling back to alternative Y."
```

**Verdict: `failed_attempt` for the X finding, `decision` for the Y choice.**

Two distinct rows, same archived-at timestamp so they cluster in recall.
The `failed_attempt` carries the *reason* the obvious path didn't work,
which prevents future sessions from trying it again. The `decision` records
the chosen path and the relationship.

## Example 7: A long Edit tool_use input (the file content body)

```
Edit(file_path=..., old_string=<3KB code>, new_string=<3KB code>)
```

**Verdict: `code_artifact` if the edit is substantial; `transient` if the
edit is mechanical.**

For a substantial diff (algorithm change, new function, refactor of error
handling), archive as `code_artifact` with the new_string body as the
artifact. Future sessions might want to find "what was written to handle
problem X" by searching the code.

For a mechanical edit (renamed a variable, added a docstring, fixed a
typo), `transient` — the result is already on disk in the file, and the
diff has no independent value.

## Example 8: A Read tool_result on a 50-line config file

```
Read(file_path="config.json") returns the full file
```

**Verdict: `transient`.**

The file is on disk. Future sessions will re-read it if they need to know
what the config looks like, and re-reading is more accurate than recalling
a stale snapshot.

This is the most common verdict in any session — most Reads, Bashes, Greps,
Globs, LSs all map to `transient`. That's the design.

## Example 9: A discovery the user makes about their own setup

```
User: "btw the desktop client on platform X uses a newer model but doesn't
       honor environment variable Y — so the proxy only sees CLI traffic."
```

**Verdict: `discovery` AND `requirement` (the implied scope constraint).**

Pure factual discovery about the environment. The proxy can't intercept the
desktop client — that's something future sessions need to know before
assuming we have desktop traffic to work with.

When two categories both fit, pick `discovery` as primary and add a
note in the body about the scope implication. Recall on either category
will find it.

## Example 10: Thinking-out-loud that didn't conclude

```
Notes: "We could go with X per-project, or one global Y. Pros and cons both
        ways. Let me think... actually let's wait for the user to weigh in
        before deciding."
```

**Verdict: `transient`.**

Planning chatter without a conclusion. The user's reply ("global with
session_id as a column") and the subsequent decision is what'd get archived
as a `decision`. The exploratory rambling doesn't need to survive — if
future sessions revisit the question, they can re-explore.

This is the most common false-positive temptation: archiving thinking
because it *feels* substantive. The test is: did it conclude? If yes,
archive the conclusion. If no, drop the rambling.
