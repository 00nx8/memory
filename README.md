# memory-inject

A stateless local proxy that sits between Claude Code (or any Anthropic SDK
client) and `api.anthropic.com`. It runs a small ordered transform pipeline
on every `/v1/messages` request — pruning oversized old tool payloads,
dropping old thinking blocks, compacting older text when the window fills up,
guarding against runaway auto-injected attachments, and (optionally) recalling
archived context from a local SQLite database.

There is no on-disk mirror of the conversation, no divergence reconciliation,
and no validation gate that can refuse to forward. Pruning is by stubbing
content, never by removing messages — so role alternation and tool-use
pairing are preserved by construction, and the API never sees a body it
would have rejected because of the proxy.

---

## When should you use this?

You'll get something out of `memory-inject` if any of these is true:

- **Your conversations grow beyond a single window.** Long-running agents
  (research pipelines, multi-day refactors, customer support bots) hit the
  context limit and either auto-compact (lossy, no recall) or fail. This
  proxy reclaims old tool bulk and archives older content into a queryable
  DB so you can keep going past where you'd normally stop.

- **You want a record of what got compacted.** The default behaviour of
  client-side autocompaction throws information away. `memory-inject`
  archives every elided original to `archive.jsonl` and (optionally) to a
  SQLite DB you can query.

- **You've been bitten by oversized auto-injected blocks.** If your IDE
  occasionally injects a multi-megabyte `<ide_selection>` from a long-line
  file, the `guard` step caps exactly that without touching the user's own
  prose.

- **You want to learn what's actually on the wire.** The transform pipeline
  is observable — every request gets a `transform` log entry showing what
  was pruned and how many bytes were saved.

It is **not** the right tool if you need:

- TLS termination of a non-cooperating client (the proxy depends on
  `ANTHROPIC_BASE_URL` — clients that ignore it can't be intercepted).
- Cross-machine context sharing (everything is local).
- A persistent edit-the-conversation-between-turns workflow. That was the
  failure mode that motivated this rewrite; this proxy is stateless on
  purpose. Use a different tool if you need that.

---

## What it does, in one paragraph

For each `/v1/messages` request from the client, the proxy parses the body,
runs the five-step pipeline (`inject → prune → strip_thinking → compact →
guard`) on a
mutable copy, re-serialises, and forwards. Every transform is independent;
each one either mutates the payload and reports stats, or raises and is
skipped while the rest still run. The proxy is never stricter than the API.

---

## Quickstart

End-to-end recipe for a fresh install. Every step has a deeper reference
later in this README; if anything in the short version is unclear, jump to
the section called out in parentheses.

1. **Clone the directory** somewhere. No external Python deps; just
   Python 3.10+ on `PATH`.

2. **Start the proxy once** to generate config and pick a port:
   ```
   python proxy.py
   ```
   The chosen port is written to `proxy_port.txt`. Leave the proxy
   running; restart it whenever you edit `proxy_config.json`. (See
   §Setup → step 2.)

3. **Wire your client to it.** For Claude Code, add an `env` block to
   `~/.claude/settings.json` — substitute the port from `proxy_port.txt`:
   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://127.0.0.1:49833"
     }
   }
   ```
   Restart `claude`. (See §Setup → step 3 for other clients and the
   launcher scripts.)

4. **Turn on the recall layer.** The default ships `inject.enabled: false`,
   which leaves the DB write-only — stubs go in, the model never sees them
   spliced back inline. Edit `proxy_config.json`:
   ```json
   "inject": { "enabled": true }
   ```
   Restart the proxy. (See §`inject` and §`inject()` in the proxy for what
   this unlocks: pinned auto-injection and `[[recall: ...]]` markers.)

5. **Register the MCP server** so the model can recall/archive/classify in
   conversation without shelling out:
   ```
   claude mcp add --scope user memory-inject -- python /ABSOLUTE/PATH/memory-inject/mcp_server.py
   ```
   Optional but strongly recommended — without it every memory operation is
   a `Bash` round-trip. (See §MCP server.)

6. **Add the global orientation block** to `~/.claude/CLAUDE.md`. This is
   what teaches every future session that the proxy exists, how to recall,
   and the four MUST-rules (triage gate, per-kind discipline, triple-layer
   rule persistence, DB-first). Without this, fresh sessions ignore the
   archive entirely. See §"Setup: session orientation + the triage nudge"
   → §1 for the verbatim block to paste.

7. **Wire the triage hook** as a global `UserPromptSubmit` hook in
   `~/.claude/settings.json`. Keeps the `unclassified` queue from rotting
   into bulk work. See §"Setup: session orientation + the triage nudge"
   → §2.

8. **Smoke-test.** Open a fresh `claude` and ask "what's the memory-inject
   proxy?". Two things should be true:
   - the model's answer reflects the orientation block you just added —
     proves `CLAUDE.md` loaded;
   - `proxy.log` shows a new `transform` event for that request — proves
     traffic is actually routed through the proxy.

   If either is missing, `ANTHROPIC_BASE_URL` isn't reaching the client.

The rest of this README explains *why* each piece exists and how to tune
it. If you only ever read the Quickstart, the system still works.

---

## Setup

### 1. Drop the directory somewhere

Clone or copy `memory-inject/` to anywhere on your machine. Python 3.10+
on the system path is enough; there are no external dependencies.

### 2. Start the proxy

```
python proxy.py
```

The interpreter may be `python` or `python3` depending on your system (on
many macOS/Linux setups only `python3` exists). Use whichever resolves to
Python 3.10+; check with `python3 --version`. The same applies to the
`migrate.py` / `memory.py` commands below and the MCP-server config.

On first run it picks a free port, persists it to `proxy_port.txt`, and
prints the listen URL and which transform steps are active. Subsequent runs
reuse the port. Override with `--port N` or `PROXY_PORT=N`.

The proxy writes its events to `proxy.log` (one JSON record per line) and
archives elided originals to `archive.jsonl`. Both are co-located with
`proxy.py`.

### 3. Point your client at it

Set the environment variable before launching the client. The port lives
in `proxy_port.txt` next to `proxy.py`:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:<port>
```

For Claude Code specifically, the recommended path is to pin it in the
global settings file at `~/.claude/settings.json` so every `claude`
invocation picks it up. Merge an `env` block alongside any existing keys
(do not overwrite the file):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:49833"
  }
}
```

A per-shell `export` also works for one-off invocations. Two launcher
scripts ship with the proxy as a third option — they spawn one client
process with `ANTHROPIC_BASE_URL` set AND derive the project tag from
`pwd` (see Project attribution below): `run-claude-proxied.sh` on
macOS/Linux and `run-claude-proxied.ps1` on Windows. Use them when you
want per-launch project tagging without editing `settings.json`.

### 4. Verify it's wired

Send any request from the client and check `proxy.log` — you should see
matching `transform` and `response` events. If you don't, the env var
isn't reaching the client process.

A stronger smoke test (recommended after steps 5–7 of the Quickstart are
also done): open a fresh `claude` and ask "what's the memory-inject
proxy?". A correct answer is one that reflects the orientation block from
`~/.claude/CLAUDE.md` — that confirms both routing *and* orientation,
which is what you actually care about.

---

## Configuration: `proxy_config.json`

The file is written with defaults on first run. Edit it and restart the
proxy to apply changes. All values are optional; missing ones fall back to
defaults.

```json
{
  "guard":   {"enabled": true, "text_block_max_bytes": 131072},
  "prune":   {"enabled": true, "tool_result_max_bytes": 8000,
              "tool_use_max_bytes": 1000, "keep_recent_turns": 6},
  "strip_thinking": {"enabled": true, "keep_recent_turns": 6},
  "compact": {"enabled": true, "compact_at_fraction": 0.75,
              "keep_recent_turns": 8},
  "inject":  {"enabled": false},
  "capture": {"enabled": true}
}
```

### `guard`

Caps auto-injected user text blocks (anything starting with one of
`<ide_selection>`, `<ide_opened_file>`, `<ide_diagnostics>`,
`<system-reminder>`). The user's own prose is left untouched even if it's
above the cap. Set `text_block_max_bytes` to whatever fits your model's
window.

Disable if you don't want this safety net (`--no-guard` flag also works).

### `prune`

Replaces oversized `tool_use.input` (default >1KB) and `tool_result.content`
(default >8KB) blocks in the OLD region of the conversation with short
stubs. The OLD region is everything before the last `keep_recent_turns`
user-text turns. Block ids and tool-use/tool-result pairing are preserved,
so the API sees a valid body.

Raise the byte thresholds to be more conservative; lower them to reclaim
more space. The default 8KB / 1KB are conservative enough that genuinely
small tool results are never touched.

`keep_recent_turns: 6` means the most recent 6 user-text turns are
untouched. The active tool-use loop almost always lives in the last 2-3
turns, so 6 is comfortable.

### `strip_thinking`

Drops `thinking` / `redacted_thinking` blocks **entirely** from assistant
turns in the OLD region (everything before the last `keep_recent_turns`
user-text turns), archiving each to the DB under the `thinking` category
first so it stays recallable.

This is safe because the API does not require previous turns' thinking blocks
and only rejects *modified* ones — removing a whole block is fine, rewriting
it in place is not (the signature stops verifying). So this step removes the
block outright; it never stubs. The one turn whose thinking block *is*
mandatory — the active tool-use loop — lives in the recent tail and is never
touched.

Note the reclaim is mostly request-body (wire) size: newer models already
exclude prior-turn thinking from input-token billing, so the main payoffs are
a smaller forwarded body and a searchable DB record of the reasoning. Each
drop re-stabilises after the first turn, so the prompt cache rebuilds once,
not every turn. Set `strip_thinking.enabled: false` to keep thinking blocks in
the forwarded request.

### `compact`

Runs only when the prior turn's real input-token count is at or above
`compact_at_fraction` of the model window (default 0.75, so 750k tokens on
a 1M-context model). At that point it archives + stubs OLD user/assistant
**text** entries so space is reclaimed but the content stays recallable
from the DB.

It NEVER touches `thinking` / `redacted_thinking` blocks (signed; modifying
them errors under newer models), only `text` blocks.

`keep_recent_turns: 8` is the protected tail for compact — slightly larger
than prune's, because losing recent context to compact is worse than
losing it to prune.

If you don't want compact, set `compact.enabled: false`. The prune step is
usually enough for normal session lengths.

### `inject`

A no-op hook by default. This is where the DB-recall layer plugs in —
querying `archive.jsonl` or the SQLite DB by project + category + full-text
search, and splicing recalled originals back into the outbound request.

The body of `inject()` lives in `proxy.py`; replace the placeholder with
your DB-backed implementation when the recall layer is ready. The function
signature is documented at the top of the file.

### `capture`

When enabled (default), the proxy writes a structured `transform` event to
`proxy.log` for every `/v1/messages` request with the byte savings, the
inferred session id, and the context-fullness fraction. Useful for tuning
the thresholds. Disable to reduce log noise.

---

## What to edit

Most users will:

1. **Tune the thresholds.** `prune.tool_result_max_bytes` is the lever with
   the biggest impact on memory; `guard.text_block_max_bytes` is the lever
   with the biggest impact on robustness against runaway attachments. Edit
   `proxy_config.json`, restart the proxy.

2. **Toggle steps for debugging.** Every step has a `--no-<step>` CLI flag
   (e.g. `--no-strip_thinking`) and an `enabled: false` config option. Useful
   to bisect "did the proxy cause this?" — turn off one step at a time.

3. **Add a category.** If the default six (`decision`, `requirement`,
   `discovery`, `failed_attempt`, `code_artifact`, `user_dialogue`) don't
   fit something you're archiving, add a new one with the CLI. Check
   `pruning/categories.md` for guidance on when to add and when to fold
   into an existing category.

4. **Replace `inject()`.** If you want to enable the DB-recall layer, the
   placeholder function in `proxy.py` is documented; fill it in with a
   call to your DB and a splice into the request payload.

Most users will NOT need to:

- Edit `archive.jsonl` directly. It's append-only and content-hashed for
  dedup; treat it as a log.
- Edit the SQLite DB by hand. Use the `memory.py` CLI or the MCP server
  verbs.
- Change the pipeline order. The pipeline is `inject → prune → compact →
  guard` for a reason: recall first (so recalled content gets pruned if
  oversized), then mechanical prune (cheap), then text compact (only when
  needed), then size guard (last-resort safety).

---

## Pruning anchors: `pruning/`

Three docs live in `pruning/` next to the proxy: `principles.md`,
`categories.md`, `examples.md`. They define the rules the model re-reads
when classifying archived content. Keep them in sync with how you actually
use the system — if you find yourself adding categories or changing what
counts as `transient`, edit the docs first so future classification stays
consistent.

These are not consumed by the proxy. They are reference material for the
model and for whoever's running the system.

---

## Files

| file | what it is |
|---|---|
| `proxy.py` | the proxy itself, single file, no external deps |
| `proxy_config.json` | configuration (auto-created on first run) |
| `proxy_port.txt` | persisted listen port (auto-created) |
| `proxy.log` | structured event log (one JSON per line) |
| `archive.jsonl` | elided originals, append-only, content-hashed dedup |
| `db.py` | SQLite layer (categories + projects + FTS5 search) |
| `migrate.py` | seed the DB from `archive.jsonl`; idempotent |
| `memory.py` | CLI: recall / archive / classify / pin / list / show |
| `mcp_server.py` | stdio MCP server exposing the same verbs to the model |
| `triage_nudge.py` | `UserPromptSubmit` hook: nudges when rows await triage |
| `pruning/` | classification anchors (principles, categories, examples) |
| `run-claude-proxied.sh` | launcher for Claude Code on macOS/Linux |
| `run-claude-proxied.ps1` | launcher for Claude Code on Windows |
| `README.md` | this file |

---

## The DB layer (recommended)

Technically optional — the proxy still prunes without it — but the
"queryable system of record" promise the rest of this README makes
*requires* the DB layer. Without it, elided originals are recoverable only
by grepping `archive.jsonl` by hand, `inject()` recall is a no-op, and
the MUST-rule "DB-first before deciding or asking" has nothing to query.
Treat this section as part of normal setup.

The proxy writes elided originals to `archive.jsonl` regardless. The DB
layer is what makes that archive *queryable*: FTS5 search, category
tagging, pinning, `inject()` recall, and the `memory.py` / MCP verbs all
live here.

### Initial seed

```
python migrate.py
```

Reads `archive.jsonl` from the proxy directory and seeds SQLite at
`~/.claude/memory/archive.db` (override with `--db PATH` or
`MEMORY_INJECT_DB`). Idempotent — re-run any time to catch up new rows.

If you skipped this and started using the proxy first, that's fine —
tee-writes (see below) have already populated the DB from fresh elisions.
`migrate.py` is only required when you have a pre-existing `archive.jsonl`
to import, or when rebuilding the DB after corruption.

### CLI

```
# Search the archive.
python memory.py recall --query='proxy validation' --project=any

# See what's still untriaged.
python memory.py list unclassified

# Triage: walk unclassified rows one at a time. d=decision, r=requirement,
# i=discovery, f=failed, c=code, u=user, t=transient, p=pin, s=skip, q=quit.
python memory.py triage

# At-a-glance counts per category.
python memory.py triage-summary

# Tag a row as a real category (accepts unambiguous key prefix).
python memory.py classify 03ad33f2 discovery

# Pin a row so the proxy never auto-elides it.
python memory.py pin 03ad33f2

# Add a new category.
python memory.py add-category methodology "Process notes about how we worked."

# Print a row in full.
python memory.py show 03ad33f2
```

Run `python memory.py --help` for the full subcommand list.

### Project attribution

Every archived row is tagged with a project. The proxy resolves it on each
request from these sources, in order:

1. `X-Memory-Project` request header (clients that allow custom headers)
2. `./current_project.txt` next to the proxy — a sidecar file the launcher
   writes from the current working directory on each invocation. The proxy
   re-reads it per request, so changing directories between launches just
   works.
3. `MEMORY_INJECT_PROJECT` env var on the proxy process
4. `payload.metadata.cwd` / `.project` (currently not populated by Claude
   Code, but reserved)
5. Default: `misc`

The provided launcher scripts (`run-claude-proxied.sh` on macOS/Linux,
`run-claude-proxied.ps1` on Windows) auto-derive the project from `pwd` —
the first path segment under `$HOME` becomes the project name, falling back
to the directory's basename, then `misc`. Set `MEMORY_INJECT_PROJECT`
(`$env:MEMORY_INJECT_PROJECT` on Windows) before launching to override.

When you migrate from `archive.jsonl` to the DB after enabling project
detection, rows captured BEFORE the launcher started writing the sidecar
will all be tagged `misc`. That's fine — you can reclassify them with the
`memory.py classify ...` command, or filter the recall query with
`--project=any`.

### Tee-writes to the DB

When `db.py` is importable (i.e. SQLite is available, which it is in any
standard CPython), the proxy writes elided originals to BOTH
`archive.jsonl` AND the SQLite DB in one step. No separate `migrate.py`
pass is needed for fresh elisions — they're queryable immediately via
`memory.py recall` or the MCP server.

`migrate.py` is still useful for:
- Seeding the DB on a fresh install from an existing `archive.jsonl`
- Recovering after a DB file corruption (the JSONL is the source of truth)
- Re-importing if you ever rebuild the DB schema

### MCP server

Wire `mcp_server.py` into your client's MCP config to give the model
in-conversation access to recall/archive/classify/pin.

For Claude Code, the one-liner registers it in the global (user) scope so
every project sees it — use an absolute interpreter path and an absolute
script path:

```
claude mcp add --scope user memory-inject -- python3 /ABSOLUTE/PATH/memory-inject/mcp_server.py
```

Equivalently, add it by hand to the `mcpServers` block of `~/.claude.json`
(user scope) or a project `.mcp.json`:

```json
{
  "mcpServers": {
    "memory-inject": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/memory-inject/mcp_server.py"]
    }
  }
}
```

Use whichever interpreter name resolves to Python 3.10+ (`python` or
`python3`) — `python3 --version` to check. The server and the proxy must
resolve the same DB; leave `MEMORY_INJECT_DB` unset on both to share the
default `~/.claude/memory/archive.db`. Verify with `claude mcp list` (shows
`memory-inject … ✓ Connected`).

Tools exposed: `recall`, `archive`, `classify`, `pin`, `show`,
`list_categories`, `list_projects`, `list_unclassified`, `add_category`.

### `inject()` in the proxy

When `inject.enabled: true` in `proxy_config.json`, the proxy's `inject()`
step runs on every request. It does two things:

1. **Pinned auto-injection.** For any non-misc project, pinned rows get
   appended to the first user message as a single text block. The model sees
   them as part of the request automatically.

2. **Magic-marker recall.** If a user message contains `[[recall: ...]]`,
   the proxy runs the query against the DB and appends the results as a text
   block to that same message. The marker is rewritten to a
   `[[recalled@...]]` stub so re-runs don't duplicate.

   Marker forms:
   ```
   [[recall: search terms here]]
   [[recall: category=decision query=foo]]
   [[recall: category=decision project=memory-inject query=foo limit=5]]
   ```

   Operators (OR / AND / NOT / NEAR / quoted phrases) pass through to FTS5.
   Hyphenated terms are auto-quoted so `first-party` works as a literal.

Both paths preserve API invariants — only existing user messages get
content appended, no messages are dropped, no thinking blocks touched.

---

## Operational notes

- **The proxy must be running** while `ANTHROPIC_BASE_URL` points at it.
  Unset the env var to fall back to direct API access.

- **First-run permissions.** On some platforms the first `python proxy.py`
  may prompt for firewall access for incoming connections on localhost. Allow.

- **Logs grow.** `proxy.log` and `archive.jsonl` are append-only. Rotate
  them if you run the proxy continuously for weeks. There's no automatic
  rotation.

- **Crashes are safe.** The proxy is stateless; killing it (Ctrl+C, OS
  reboot) loses no client-side state. Restart and continue.

- **The proxy never modifies thinking blocks.** Models from the 4.8
  generation onward verify thinking-block signatures and reject anything
  modified mid-history. The compact step only touches `text` blocks for
  this reason.

## Setup: session orientation + the triage nudge

Three pieces make a fresh Claude session use this proxy well: a pointer in your
global `CLAUDE.md` so the session knows the proxy exists, a
`UserPromptSubmit` hook so the `unclassified` queue gets cleared a few rows at a
time instead of piling up into a bulk job, and the usage rules below
(§"Required usage rules") that keep the archive useful over months instead of
decaying into an unsearchable heap.

### 1. Global `~/.claude/CLAUDE.md` additions

Add a block like this so every session orients to memory-inject (and does **not**
go hunting for some other memory directory):

```
You are running behind the memory-inject proxy (~/projects/memory-inject). It manages your
context transparently: oversized/old tool payloads and old conversation text are
stubbed on the wire and archived to a SQLite DB (~/.claude/memory/archive.db).
Inline stubs like `[context-proxy: N bytes elided, recall from DB]` are NOT lost --
recall them with: python3 ~/projects/memory-inject/memory.py recall --query='...'

The proxy pins ANTHROPIC_BASE_URL via ~/.claude/settings.json, so plain `claude`
already routes through it (run-claude-proxied.sh is only needed for per-launch
project tagging).

Keep the archive's `unclassified` queue small: when the triage nudge appears,
classify the new rows. tool_use/tool_result and IDE-wrapper user_text are almost
always `transient`; review assistant_text for decision/discovery/requirement:
python3 ~/projects/memory-inject/memory.py classify <key> <category>

MUST-rules (non-negotiable, see memory-inject README "Required usage rules"):
1. Triage gate: if the nudge reports MORE THAN 10 unclassified rows, triage
   them BEFORE responding to the current task. At or below 10, defer is fine.
2. Per-kind discipline: bulk-classify tool_use/tool_result as transient
   without inspection; INSPECT EACH user_text and assistant_text before
   classifying (user rules and hard-won conclusions hide there).
3. Triple-layer persistence: when the user establishes a rule ("from now on
   X", "always Y", "never Z"), persist it three ways — memory-index entry +
   dedicated detail file + a PINNED verbatim archive row of the user's words.
4. DB-first: BEFORE producing a non-trivial decision/recommendation OR
   asking the user about past decisions/discussions, run memory.py recall
   with several phrasings. Re-deriving without checking risks contradicting
   a settled decision; re-asking burns the user's turn. Proceed only if
   recall comes up empty.

Session-start orientation (MANDATORY, before substantive work, regardless of
what the user's first prompt asks): read the memory-inject README and the
three classification anchors in pruning/ (principles.md, categories.md,
examples.md). Do not classify archived rows or reason about context behaviour
from memory of a past session — re-anchor first. This is not optional and
does not wait for the user to mention memory.
```

This block belongs in the **global** `~/.claude/CLAUDE.md` — not a per-project
one — so that *every* session orients itself, including sessions opened in
repos that have no project `CLAUDE.md` of their own. A per-project copy only
protects the projects that have it; the failure mode (a fresh session ignoring
the archive, mis-classifying rows, or hunting for a stale memory tree) is
global, so the directive must be global too.

Do not point sessions at any other memory tree -- this proxy plus its DB is the
system of record.

### 2. The `UserPromptSubmit` triage nudge (global hook)

`triage_nudge.py` (in this repo) prints a one-line reminder when archived rows
await classification. It uses a read-only DB connection and is silent + exits 0
when the DB is absent, so it is safe in **every** session, including ones that do
not use the proxy.

Wire it as a **global** hook in `~/.claude/settings.json` so all sessions get it:

```
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "python3 /ABSOLUTE/PATH/memory-inject/triage_nudge.py" } ] }
    ]
  }
}
```

- Use the **absolute** path to your clone -- hooks do not expand `~`.
- Merge the `hooks` key alongside any existing keys (e.g. `env`); do not replace
  the whole file.
- The script's stdout is injected into the model's context on each prompt, so the
  reminder shows up in-band. Tune `THRESHOLD` in the script if a 1-row nudge is
  too eager.

### 3. Required usage rules

These four rules are the operating contract for any session running behind the
proxy. They were each earned the hard way (queue rot, lost user directives,
re-asked questions); treat them as MUSTs, not suggestions. The `CLAUDE.md`
block in §1 carries a compact copy so every session sees them — this section
is the reference version.

**Rule 1 — the triage gate.** When the `UserPromptSubmit` nudge reports
**more than 10** unclassified rows, the session must triage them **before**
responding to the current task. At or below 10, deferring to a natural pause
is fine. Rationale: `recall` excludes `transient` by default, but
`unclassified` rows aren't usefully searchable either — an accumulating queue
is dead weight that silently degrades every future recall. Classification is
non-interruptible hygiene, same as a failing CI gate.

**Rule 2 — per-kind classification discipline.** Not all rows deserve the
same attention:

| Row kind | Discipline |
|---|---|
| `tool_use` / `tool_result` | **Bulk-classify `transient` without inspection.** Almost always re-derivable: the file is on disk, the command can re-run, the HTTP response was mutable state anyway. Exception: a result carrying a load-bearing external finding (web search, vendor doc) — extract the 1–2 key sentences into a separate `discovery` row first, then mark the bulk row transient. |
| `user_text` | **Inspect each one.** IDE-wrapper noise (system reminders, file-open notifications) is transient — but user messages carrying rules, preferences, framing, or constraints are `requirement`. Default-transient here loses the user's actual instructions. |
| `assistant_text` | **Inspect each one.** Most are conversational filler, but conclusions reached after diagnostic work are `discovery`, and choices made between debated options are `decision`. These are exactly the rows future sessions need. |

**Rule 3 — triple-layer rule persistence.** When the user explicitly
establishes a behavioural rule ("from now on do X", "always Y", "never Z"),
persist it across **three** layers, not one:

1. a one-line entry in the assistant's persistent memory index (the file
   auto-loaded into every session, e.g. Claude Code's `MEMORY.md`),
2. a dedicated detail file alongside it (the rule + why + how-to-apply), and
3. a **pinned verbatim archive row** of the user's original message:

```
python3 /PATH/TO/memory-inject/memory.py archive \
  --kind=user_text --category=requirement --project=<project> \
  --body="<the user's exact words>"
# returns: new <key>
python3 /PATH/TO/memory-inject/memory.py pin <key>
```

The index alone is brittle (one un-followed link and the rule is lost); the
pinned verbatim row is the last line of defense — even if the detail file is
deleted and the index entry typo'd, `recall` still surfaces the original
directive intact. Skip the ceremony only for trivial preferences ("call me
X"); anything behavioural gets all three layers.

**Rule 4 — DB-first before deciding *or* asking.** Two triggers, same
discipline. Before either:

(a) producing a non-trivial decision, recommendation, architectural
    choice, or path forward, **or**
(b) asking the user a question — especially "did we discuss X", "what was
    the constraint on Y", "have we tried Z"

— query the archive first:

```
python3 /PATH/TO/memory-inject/memory.py recall --query='<terms>' --project=<project>
```

Try several phrasings (synonyms, related concepts, the inverse) before
concluding the answer isn't there. The proxy archives far more than is
visible in-context; the relevant prior row is usually one recall away.

The (a) case is the more common failure mode and the more dangerous one.
The model's instinct is to derive recommendations from scratch every
session — but the archive often already contains the path the user chose,
the path tried and abandoned (with the reason), or the constraint that
rules out the obvious option. Re-deriving without checking risks
contradicting a settled decision or re-litigating a debate the user
already concluded. The (b) case is more obviously wasteful — re-asking
something the user already answered burns their turn and reads as
memoryless — but it's also more visible, so it tends to self-correct.
The silent (a) case is what this rule is mostly defending against.

Only proceed (decide or ask) when recall genuinely comes up empty across
several phrasings.
