#!/usr/bin/env python3
"""memory-inject -- a terse, stateless Anthropic context proxy.

Sits between the Claude Code client and api.anthropic.com. For every
/v1/messages POST it runs an ordered transform pipeline on a copy of the
client's OWN request body, then forwards it. There is no on-disk mirror, no
divergence reconciliation and no validation gate -- the bug classes those
caused (context fossilisation; "422 refused to forward"; role-alternation
breaks) are structurally absent here.

Why stateless works: the client re-transmits the entire conversation on every
turn, so the proxy always holds every message in the current request. It never
needs to persist context to remember it -- it just transforms what's in front
of it, deterministically, each turn.

Pipeline (each step mutates a parsed copy; a step that raises is skipped and the
rest still run -- the proxy is never stricter than the API it fronts):

    inject()  -- recall DB-stored context and splice it in. NO-OP HOOK; the
                 memory/DB layer plugs in here.
    prune()   -- stub OLD oversized tool_result / tool_use.input. Cheap tier.
    strip_thinking() -- drop OLD thinking/redacted_thinking blocks entirely,
                 archiving each to the DB first (category 'thinking').
    compact() -- at >= compact_at_fraction context-fullness, archive + stub OLD
                 (start->middle) user/assistant entries to the DB so space is
                 reclaimed but the content stays recallable.
    guard()   -- cap oversized auto-injected text blocks (<ide_selection> etc.).

Hard-won invariants baked in here so a future reader doesn't re-derive them:

* anthropic-beta is sent as MULTIPLE header lines; one carries
  context-1m-2025-08-07, which selects the 1M window. Headers are replayed
  verbatim, one line at a time. Collapse them into a dict and the beta is
  dropped -> the server silently falls back to 200k.
* NEVER drop a whole message to save space -- that creates consecutive
  same-role messages (role-alternation error) or orphans a tool_use/tool_result
  pair. Elide by STUBBING content and keeping the message shell.
* NEVER modify thinking / redacted_thinking blocks. Opus 4.8 verifies their
  signatures; touching them throws "thinking blocks cannot be modified".
  REMOVING a whole thinking block from an OLD turn is fine, though -- the API
  doesn't require previous turns' thinking and only rejects *modified* blocks.
  strip_thinking() exploits exactly that: whole-block drop, never a stub. The
  current tool-use turn (whose thinking block IS mandatory) is protected by
  keep_recent_turns.
* The single proven size failure was a ~1.25MB <ide_selection> block injected
  by the IDE that tokenised ~1:1 and alone blew the 1M ceiling -- guard() caps
  exactly that.
"""

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Optional DB-backed recall. db.py is part of memory-inject; if it can't be
# imported (e.g. sqlite3 missing) inject() degrades to a no-op.
try:
    import db as _db  # noqa: F401
    _DB_AVAILABLE = True
except Exception:
    _db = None
    _DB_AVAILABLE = False

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443
UPSTREAM_TIMEOUT = 600
PORT_FILE = os.path.join(HERE, "proxy_port.txt")
CONFIG_FILE = os.path.join(HERE, "proxy_config.json")
LOG_FILE = os.path.join(HERE, "proxy.log")
ARCHIVE_FILE = os.path.join(HERE, "archive.jsonl")  # DB feed / recall source
CURRENT_PROJECT_FILE = __import__("pathlib").Path(HERE) / "current_project.txt"
# Sidecar the launcher writes per invocation -- proxy reads on each request
# to attribute archived rows to the right project. Optional; absent -> "misc".
MODEL_CONTEXT_TOKENS = 1_000_000

# Hop-by-hop headers must not cross a proxy (RFC 7230 6.1).
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
              "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade"}

# Auto-injected context wrappers (attached by the client/IDE, not typed by the
# user) -- safe to truncate when oversized.
INJECTED_MARKERS = ("<ide_selection>", "<ide_opened_file>",
                    "<ide_diagnostics>", "<system-reminder>")

DEFAULTS = {
    "guard":   {"enabled": True,  "text_block_max_bytes": 131072},
    "prune":   {"enabled": True,  "tool_result_max_bytes": 8000,
                "tool_use_max_bytes": 1000,
                "protect_assistant_turns": 15},
    "strip_thinking": {"enabled": True, "keep_recent_turns": 6},
    "compact": {"enabled": True,  "compact_at_fraction": 0.75,
                "keep_recent_turns": 8},
    "inject":  {"enabled": False},
    "capture": {"enabled": True},
    # Transparent upstream retry. When Anthropic returns an overload/rate-limit
    # status (529 Overloaded, 429 rate-limited -- "Server is temporarily
    # limiting requests, not your usage limit"), the proxy re-sends the SAME
    # request with backoff BEFORE relaying anything to the client. The client
    # never sees the error, so the agent never halts -- it just continues once
    # capacity frees up. Bounded by max_attempts; after that the error is
    # surfaced as normal. base/max_delay in seconds; Retry-After is honoured.
    "retry":   {"enabled": True, "statuses": [429, 529],
                "max_attempts": 8, "base_delay": 1.0, "max_delay": 30.0},
}
CFG = {}                      # populated by main()
_last_tokens = {}            # session -> last input_tokens seen (75% trigger)
_archived_keys = set()       # cross-session dedup for archive.jsonl
_locks = {"log": threading.Lock(), "archive": threading.Lock()}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(event, **fields):
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    line = json.dumps(rec, ensure_ascii=False)
    with _locks["log"]:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _nbytes(obj):
    if isinstance(obj, str):
        return len(obj.encode("utf-8"))
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _session_of(payload):
    """Best-effort session id from metadata.user_id; falls back to 'unknown'."""
    uid = (payload.get("metadata") or {}).get("user_id") or ""
    m = re.search(r"session[_-]?([0-9a-f-]{8,})", uid)
    return m.group(1) if m else (uid[:32] or "unknown")


# Project of the current request, set by Handler._transform before any pipeline
# step runs. Used by archive() to tag DB rows. Reset to "misc" on each request.
_current_project = "misc"


def archive(session, kind, block_id, original, category="unclassified"):
    """Append one elided original to archive.jsonl -- the JSONL feed and the
    historical recall source. If db.py is importable, ALSO write to the SQLite
    DB so recall can run without a separate migrate step. Deduped by
    (kind, id, content-hash) so re-eliding the same block every turn writes
    it once."""
    h = hashlib.sha1(_nbytes(original).to_bytes(8, "big")
                     + json.dumps(original, ensure_ascii=False,
                                  sort_keys=True).encode("utf-8")).hexdigest()[:16]
    key = (kind, block_id, h)
    with _locks["archive"]:
        if key in _archived_keys:
            return False
        _archived_keys.add(key)
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "session": session, "category": category,
                "kind": kind, "id": block_id, "original": original,
            }, ensure_ascii=False) + "\n")
        # Tee-write to the DB if available. Errors are logged and swallowed;
        # the JSONL write above is the source of truth and migrate.py can
        # back-fill anything the DB drops.
        if _DB_AVAILABLE:
            try:
                conn = _db.init()
                try:
                    _db.archive(conn, kind=kind, body=original,
                                session=session, block_id=block_id,
                                project=_current_project, category=category)
                finally:
                    conn.close()
            except Exception as e:
                log("db_archive_error", error=str(e))
    return True


def _detect_project(headers, payload):
    """Resolve the project name for this request.

    Priority:
      1. X-Memory-Project header (only some clients let you add headers)
      2. ./current_project.txt sidecar file (the launcher writes it from $cwd
         on each `run-claude-proxied.ps1` invocation; the proxy reads it
         per-request because the proxy process started before the launcher
         and won't inherit env from it)
      3. MEMORY_INJECT_PROJECT env var on the proxy process itself
      4. payload.metadata.cwd / .project if the client ever populates it
      5. 'misc'
    """
    h = headers.get("X-Memory-Project") if headers else None
    if h:
        return _normalize_project(h)
    try:
        if CURRENT_PROJECT_FILE.exists():
            v = CURRENT_PROJECT_FILE.read_text(encoding="utf-8").strip()
            if v:
                return _normalize_project(v)
    except Exception:
        pass
    e = os.environ.get("MEMORY_INJECT_PROJECT")
    if e:
        return _normalize_project(e)
    md = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(md, dict):
        cwd = md.get("cwd") or md.get("project")
        if cwd:
            return _normalize_project(cwd)
    return "misc"


def _normalize_project(name):
    """Lowercase, strip, replace whitespace with '-'. Project names live in
    URLs/filenames/db keys and benefit from being conservative."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-")
    return s or "misc"


def _old_boundary(messages, keep_recent_turns):
    """Index splitting OLD (prunable, before it) from RECENT (kept). A turn
    starts at a user message carrying a real text block. Returns 0 when there
    are fewer turns than the keep window (nothing is old)."""
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not (isinstance(m, dict) and m.get("role") == "user"):
            continue
        c = m.get("content")
        if isinstance(c, str) or (isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "text" for b in c)):
            seen += 1
            if seen >= keep_recent_turns:
                return i
    return 0


# --------------------------------------------------------------------------- #
# transforms -- each takes the parsed payload, mutates in place, returns stats
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# inject() helpers
# --------------------------------------------------------------------------- #
# Magic marker the model writes in a user message to trigger a DB recall on the
# next turn. Forms:
#     [[recall: <FTS query>]]
#     [[recall: category=<cat> query=<FTS>]]
#     [[recall: category=<cat> project=<proj> query=<FTS> limit=<n>]]
# `inject()` finds these in the most recent user message, runs the query,
# appends the matched bodies as a single text block to that same message, and
# rewrites the marker to a record of what was recalled (so a re-run of the
# same turn doesn't recall twice).
_RECALL_MARKER_RE = re.compile(r"\[\[recall:\s*([^\]]+?)\s*\]\]")
_RECALL_HANDLED_PREFIX = "[[recalled@"


def _parse_recall_args(arg_text):
    """Parse the body of a [[recall: ...]] marker.

    Accepted forms:
        bare:  "some search terms"        -> {"query": "some search terms"}
        kv:    "category=decision query=foo limit=10"
               (kv parsing only; whitespace separates pairs)
    """
    if "=" not in arg_text:
        return {"query": arg_text.strip()}
    out = {}
    # Split on whitespace BUT respect quoted values: query="multi word"
    tokens = re.findall(r'(\w+)=("[^"]*"|\S+)', arg_text)
    for k, v in tokens:
        out[k] = v.strip('"')
    return out


def _format_recall_block(rows, args):
    """Render recall hits as a single text block ready to append to a message."""
    if not rows:
        return ("[memory-inject: recall returned no rows for "
                + json.dumps(args, ensure_ascii=False) + "]")
    lines = [f"[memory-inject: recalled {len(rows)} row(s) for "
             + json.dumps(args, ensure_ascii=False) + "]"]
    for r in rows:
        body = r["body"]
        if isinstance(body, dict):
            if r["kind"] == "tool_use" and "input" in body:
                preview = json.dumps(body["input"], ensure_ascii=False)
            elif "content" in body:
                c = body["content"]
                preview = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            elif body.get("type") == "text":
                preview = body.get("text", "")
            else:
                preview = json.dumps(body, ensure_ascii=False)
        else:
            preview = str(body)
        ts = (r["archived_at"] or "")[:19]
        lines.append(f"--- {r['archive_key'][:12]}  {r['category']}/{r['kind']}  "
                     f"({r['project']}, {ts}) ---")
        lines.append(preview)
    return "\n".join(lines)


def _pinned_block(rows):
    """Render pinned rows as a single text block."""
    if not rows:
        return None
    lines = [f"[memory-inject: {len(rows)} pinned context row(s) for this project]"]
    for r in rows:
        body = r["body"]
        if isinstance(body, dict) and body.get("type") == "text":
            preview = body.get("text", "")
        elif isinstance(body, str):
            preview = body
        else:
            preview = json.dumps(body, ensure_ascii=False)
        lines.append(f"--- {r['archive_key'][:12]}  {r['category']}  ---")
        lines.append(preview)
    return "\n".join(lines)


def _append_text_block(message, text):
    """Append a text block to a message's content array. Coerces a string
    content into a list first. Never modifies non-user messages because that
    could break role alternation expectations downstream."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content},
                              {"type": "text", "text": text}]
    elif isinstance(content, list):
        content.append({"type": "text", "text": text})
    else:
        message["content"] = [{"type": "text", "text": text}]


def inject(payload, ctx, cfg):
    """Recall DB-stored context and splice it into the request.

    Three things this does, in order:

    1. **Pinned auto-injection**: when project is known (non-misc) and there
       are pinned rows for that project, append them to the FIRST user message
       once. Pinned rows are the things future sessions need to know to use the
       system itself.

    2. **Magic-marker recall**: scan user messages for `[[recall: ...]]`
       markers, run the query against the DB, append results as a text block
       to the same message, and rewrite the marker so re-running this turn
       doesn't recall twice.

    3. **No-op fallback**: if `db` isn't importable or `inject.enabled` is
       false, this returns 0 cleanly. The proxy stays functional without the
       DB layer.

    Invariants honored:
      - Never drops a message, so role alternation can't break.
      - Never touches thinking blocks.
      - Only mutates the `text` content of existing user messages.
    """
    if not _DB_AVAILABLE:
        return {}
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return {}

    project = ctx.get("project") or "misc"
    session = ctx.get("session")

    try:
        conn = _db.init()
    except Exception as e:
        return {"inject_error": str(e)}

    pinned_n = recalled_n = 0
    try:
        # --- (1) pinned auto-inject -------------------------------------- #
        # Only once per request: look at the FIRST user message. If it
        # already carries a pinned-context block (an earlier inject() pass
        # left a marker), skip. Otherwise append.
        if project and project != "misc":
            pinned = _db.recall(
                conn, project=project, category="any",
                exclude_transient=False, limit=20, include_pinned=True,
            )
            pinned = [r for r in pinned if r["pinned"]]
            if pinned:
                first_user = next((m for m in msgs
                                   if isinstance(m, dict) and m.get("role") == "user"),
                                  None)
                if first_user:
                    existing_text = ""
                    if isinstance(first_user.get("content"), list):
                        for b in first_user["content"]:
                            if isinstance(b, dict) and b.get("type") == "text":
                                existing_text += b.get("text", "")
                    elif isinstance(first_user.get("content"), str):
                        existing_text = first_user["content"]
                    if "[memory-inject: pinned context row(s)" not in existing_text and \
                       "[memory-inject:" not in existing_text[:200]:
                        block_text = _pinned_block(pinned)
                        if block_text:
                            _append_text_block(first_user, block_text)
                            pinned_n = len(pinned)

        # --- (2) [[recall: ...]] markers --------------------------------- #
        # Walk USER messages; for any text block containing a live marker,
        # run the query and append the rendered results. Rewrite the marker
        # to a [[recalled@<key>]] stub so future passes ignore it.
        for m in msgs:
            if not (isinstance(m, dict) and m.get("role") == "user"):
                continue
            content = m.get("content")
            blocks = content if isinstance(content, list) else (
                [{"type": "text", "text": content}] if isinstance(content, str) else []
            )
            new_blocks = list(blocks)
            appended_for_this_msg = []
            for b in blocks:
                if not (isinstance(b, dict) and b.get("type") == "text"):
                    continue
                text = b.get("text", "")
                if not text or _RECALL_HANDLED_PREFIX in text:
                    continue
                # Find ALL markers in this text block.
                matches = list(_RECALL_MARKER_RE.finditer(text))
                if not matches:
                    continue
                # For each marker, run the query and stash a result block.
                new_text = text
                for mat in matches:
                    args = _parse_recall_args(mat.group(1))
                    query = args.get("query")
                    category = args.get("category")
                    proj = args.get("project") or project or "any"
                    if proj == "misc":
                        proj = "any"
                    try:
                        limit = int(args.get("limit", 10))
                    except ValueError:
                        limit = 10
                    rows = _db.recall(
                        conn, query=query, category=category, project=proj,
                        session=session if args.get("session") == "current" else None,
                        limit=limit,
                    )
                    appended_for_this_msg.append(_format_recall_block(rows, args))
                    recalled_n += len(rows)
                    # Rewrite this specific marker so re-running doesn't repeat.
                    stub = (f"{_RECALL_HANDLED_PREFIX}"
                            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}, "
                            f"{len(rows)} row(s)]]")
                    new_text = new_text.replace(mat.group(0), stub, 1)
                b["text"] = new_text
            for block_text in appended_for_this_msg:
                new_blocks.append({"type": "text", "text": block_text})
            if appended_for_this_msg:
                if isinstance(content, str):
                    m["content"] = new_blocks
                else:
                    m["content"] = new_blocks
    finally:
        try:
            conn.close()
        except Exception:
            pass

    stats = {}
    if pinned_n:
        stats["inject_pinned"] = pinned_n
    if recalled_n:
        stats["inject_recalled"] = recalled_n
    return stats


def _assistant_turns_after(messages):
    """For each message index i, count the number of assistant messages at
    indices > i. This is the per-tool age clock prune() uses: each tool_use /
    tool_result is considered N turns old when N subsequent assistant messages
    have been emitted -- not when N user-text turns have happened.

    Why per-tool age beats a single global boundary: in a tool-heavy burst
    under one user prompt, a global "last 6 user-text turns" boundary would
    treat every tool call in that burst as the same age. So a file read at the
    start of the burst dies the moment the user's next text arrives, even if
    the model is still chaining work off it. Each tool's clock starting at its
    own emission lets early-burst data stay addressable while late-burst data
    is just as fresh -- the chain doesn't collapse mid-task."""
    n = len(messages)
    counts = [0] * n
    seen = 0
    for i in range(n - 1, -1, -1):
        counts[i] = seen
        if isinstance(messages[i], dict) and messages[i].get("role") == "assistant":
            seen += 1
    return counts


def prune(payload, ctx, cfg):
    """Stub OLD oversized tool payloads. tool_use.input and tool_result.content
    carry no signature, so rewriting them is safe under any model. Block id /
    name / pairing are preserved.

    A tool_use / tool_result becomes prunable when at least
    cfg['protect_assistant_turns'] assistant messages have followed it. The
    clock starts at the tool itself, not at the user prompt that began the
    task -- so chained tool calls don't all expire together when the next user
    text lands."""
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return {}
    protect = cfg.get("protect_assistant_turns", 15)
    trmax, tumax = cfg["tool_result_max_bytes"], cfg["tool_use_max_bytes"]
    age = _assistant_turns_after(msgs)
    n = saved = 0
    for i, m in enumerate(msgs):
        if age[i] < protect:
            continue
        if not isinstance(m, dict):
            continue
        content, role = m.get("content"), m.get("role")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if role == "assistant" and b.get("type") == "tool_use":
                inp = b.get("input")
                if not isinstance(inp, dict) or "_elided" in inp:
                    continue
                sz = _nbytes(inp)
                if sz > tumax:
                    archive(ctx["session"], "tool_use", b.get("id"), inp)
                    b["input"] = {"_elided": f"context-proxy: {sz} bytes elided"}
                    n += 1
                    saved += sz
            elif role == "user" and b.get("type") == "tool_result":
                inner = b.get("content")
                if inner is None:
                    continue
                sz = _nbytes(inner)
                already = isinstance(inner, str) and inner.startswith("[context-proxy:")
                if sz > trmax and not already:
                    archive(ctx["session"], "tool_result", b.get("tool_use_id"), inner)
                    b["content"] = f"[context-proxy: {sz} bytes elided, recall from DB]"
                    n += 1
                    saved += sz
    return {"pruned": n, "prune_saved": saved} if n else {}


def strip_thinking(payload, ctx, cfg):
    """Drop thinking / redacted_thinking blocks ENTIRELY from OLD assistant
    turns, archiving each to the DB (category 'thinking') first.

    Why this is safe where stubbing them would NOT be: the API rejects a
    *modified* thinking block (its signature stops verifying -> "thinking
    blocks cannot be modified"), but it does NOT require previous turns'
    thinking blocks to be present at all. So a whole-block REMOVAL from the OLD
    region is legal; an in-place stub would not be. We therefore drop the block
    outright, never rewrite it.

    The active tool-use loop -- the one turn whose thinking block IS mandatory
    -- lives in the last keep_recent_turns and sits after the boundary, so it
    is never touched. Messages that would be left with no content are skipped
    (an empty assistant message is invalid)."""
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return {}
    boundary = _old_boundary(msgs, cfg["keep_recent_turns"])
    if boundary <= 0:
        return {}
    n = saved = 0
    for m in msgs[:boundary]:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        kept, dropped = [], []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"):
                dropped.append(b)
            else:
                kept.append(b)
        if not dropped or not kept:
            continue            # nothing to drop, or removal would empty the message
        for b in dropped:
            archive(ctx["session"], b["type"], None, b, category="thinking")
            n += 1
            saved += _nbytes(b)
        m["content"] = kept
    return {"thinking_dropped": n, "thinking_saved": saved} if n else {}


def compact(payload, ctx, cfg):
    """At >= compact_at_fraction context-fullness, archive + stub OLD
    (start->middle) user/assistant text entries so space is reclaimed while the
    content stays recallable from the DB.

    Stubs CONTENT only, keeping the message shell -> role alternation and
    tool pairing are preserved. NEVER touches thinking/redacted_thinking
    (signed) or already-stubbed blocks."""
    fullness = ctx.get("fullness", 0.0)
    if fullness < cfg["compact_at_fraction"]:
        return {}
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return {}
    boundary = _old_boundary(msgs, cfg["keep_recent_turns"])
    if boundary <= 0:
        return {}
    n = saved = 0
    for m in msgs[:boundary]:
        if not isinstance(m, dict):
            continue
        content, role = m.get("content"), m.get("role")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "text":
                continue
            txt = b.get("text")
            if not isinstance(txt, str) or txt.startswith("[archived"):
                continue
            sz = _nbytes(txt)
            if sz < 200:                    # not worth archiving tiny acks
                continue
            rid = archive(ctx["session"], f"{role}_text", None, txt)
            b["text"] = f"[archived to DB, recall this {role} entry by content]"
            n += 1
            saved += sz
    return {"compacted": n, "compact_saved": saved} if n else {}


def guard(payload, ctx, cfg):
    """Cap oversized auto-injected text blocks (ide_selection etc.) in user
    messages. The user's own prose is left untouched."""
    cap = cfg["text_block_max_bytes"]
    if cap <= 0:
        return {}
    n = elided = 0
    for m in payload.get("messages") or []:
        if not (isinstance(m, dict) and m.get("role") == "user"):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "text"):
                continue
            txt = b.get("text")
            if not isinstance(txt, str):
                continue
            raw = txt.encode("utf-8")
            if len(raw) <= cap or not any(mk in txt[:200] for mk in INJECTED_MARKERS):
                continue
            keep = raw[:cap].decode("utf-8", "ignore")
            gone = len(raw) - len(keep.encode("utf-8"))
            b["text"] = (keep + f"\n\n…[memory-inject: injected block truncated, "
                         f"{gone} bytes elided]…")
            n += 1
            elided += gone
    return {"guard_capped": n, "guard_elided": elided} if n else {}


PIPELINE = [("inject", inject), ("prune", prune),
            ("strip_thinking", strip_thinking),
            ("compact", compact), ("guard", guard)]


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):       # silence stderr access log
        pass

    def handle_one_request(self):
        """Override to swallow client-side disconnects quietly.

        Claude Code (and other HTTP clients) routinely recycle pooled
        connections by closing them without sending a request -- a perfectly
        normal lifecycle event. The default BaseHTTPRequestHandler treats the
        resulting ConnectionResetError / BrokenPipeError as an exception and
        prints a multi-line traceback per occurrence, flooding the console.
        We log a one-liner and move on; real failures (in our own code) still
        surface as exceptions from _handle.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
            log("client_disconnect",
                client=f"{self.client_address[0]}:{self.client_address[1]}",
                error=type(e).__name__)
            self.close_connection = True

    def do_GET(self):    self._handle("GET")
    def do_POST(self):   self._handle("POST")
    def do_PUT(self):    self._handle("PUT")
    def do_PATCH(self):  self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self, method):
        body = self._read_body()
        path = self.path
        p = path.split("?", 1)[0]
        # Transform both the real call and count_tokens so the client's own
        # pre-flight size gate sees the SAME reduced body that gets sent.
        if body and (p.endswith("/v1/messages") or p.endswith("/v1/messages/count_tokens")):
            body = self._transform(body, path)
        self._forward(method, path, body)

    def _transform(self, body, path):
        try:
            payload = json.loads(body)
        except Exception as e:
            log("parse_error", error=str(e))
            return body
        session = _session_of(payload)
        self._session = session
        toks = _last_tokens.get(session, 0)
        project = _detect_project(self.headers, payload)
        # Tell archive() what project to tag rows with for THIS request.
        # Reset on every request so a leftover value from a prior request
        # can't leak. (Single-threaded write of a module global is acceptable;
        # per-request races would at worst tag a row with a sibling request's
        # project. archive.jsonl always has the canonical session id so
        # mis-tagged rows are recoverable by hand.)
        global _current_project
        _current_project = project
        ctx = {"session": session, "project": project,
               "fullness": toks / MODEL_CONTEXT_TOKENS if toks else 0.0}
        stats = {}
        for name, fn in PIPELINE:
            cfg = CFG.get(name, {})
            if not cfg.get("enabled", True):
                continue
            try:
                stats.update(fn(payload, ctx, cfg) or {})
            except Exception as e:
                log(f"{name}_error", error=str(e))
        new = json.dumps(payload, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        if CFG.get("capture", {}).get("enabled", True):
            log("transform", path=path, session=session,
                fullness=round(ctx["fullness"], 3),
                body_in=len(body), body_out=len(new), **stats)
        return new

    def _forward(self, method, path, body):
        # Transparent retry on upstream overload / rate-limit so the client
        # never sees the error and the agent never halts. A 429/529 is rejected
        # at the gate (no tokens generated), so re-sending the same request is
        # safe. Only the real /v1/messages call is retried -- not count_tokens.
        rcfg = CFG.get("retry", {})
        retryable = (rcfg.get("enabled", True)
                     and path.split("?", 1)[0].endswith("/v1/messages"))
        statuses = set(rcfg.get("statuses", [429, 529]))
        max_attempts = rcfg.get("max_attempts", 8) if retryable else 1
        base = rcfg.get("base_delay", 1.0)
        cap = rcfg.get("max_delay", 30.0)

        attempt = 0
        while True:
            attempt += 1
            try:
                conn = http.client.HTTPSConnection(
                    UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT)
                conn.putrequest(method, path, skip_host=True)
                # Replay headers verbatim, one line at a time -- preserves the
                # multi-line anthropic-beta (context-1m). Drop hop-by-hop, host,
                # accept-encoding (force identity for usage parsing) and the
                # client's content-length (we set our own from the body we send).
                for k, v in self.headers.items():
                    kl = k.lower()
                    if kl in HOP_BY_HOP or kl in ("host", "accept-encoding", "content-length"):
                        continue
                    conn.putheader(k, v)
                conn.putheader("Host", UPSTREAM_HOST)
                if body:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body or None)
                resp = conn.getresponse()
            except Exception as e:
                log("upstream_error", path=path, error=str(e), attempt=attempt)
                try:
                    self.send_error(502, f"proxy upstream error: {e}")
                except Exception:
                    pass
                return

            # Retryable overload/rate-limit, and attempts remain: back off and
            # re-send WITHOUT touching the client connection (it stays open,
            # waiting -- exactly as it would during a slow generation).
            if resp.status in statuses and attempt < max_attempts:
                retry_after = resp.getheader("retry-after")
                try:
                    snippet = resp.read(200).decode("utf-8", "ignore")
                except Exception:
                    snippet = ""
                try:
                    conn.close()
                except Exception:
                    pass
                delay = None
                if retry_after:
                    try:
                        delay = min(float(retry_after), cap)
                    except (TypeError, ValueError):
                        delay = None
                if delay is None:
                    delay = min(base * (2 ** (attempt - 1)), cap)
                    delay += random.uniform(0, 0.25 * delay)  # de-correlate herd
                log("retry", path=path, status=resp.status, attempt=attempt,
                    max_attempts=max_attempts, sleep=round(delay, 2),
                    retry_after=retry_after, detail=snippet or None)
                time.sleep(delay)
                continue

            # Terminal: relay this response. Log any error status so 4xx/5xx
            # (incl. a 529 that outlasted every retry) is visible in proxy.log.
            if resp.status >= 400:
                log("response_error", path=path, status=resp.status,
                    attempt=attempt)
            self._relay(resp, path)
            return

    def _relay(self, resp, path):
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        except Exception as e:
            log("relay_header_error", path=path, error=str(e))
            return
        buf, relayed = b"", 0
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                relayed += len(chunk)
                if len(buf) < 1_000_000:        # buffer just enough for usage
                    buf += chunk
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except Exception as e:
            log("relay_disconnect", path=path, error=str(e), bytes_relayed=relayed)
            return
        self._log_usage(buf, resp.status, path)

    def _log_usage(self, buf, status, path):
        usage = {}
        for line in buf.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(obj, dict):
                msg = obj.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                    usage.update(msg["usage"])
                if isinstance(obj.get("usage"), dict):
                    usage.update(obj["usage"])
        if usage:
            # Remember the real input size for the next turn's 75% trigger.
            total_in = (usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0))
            session = getattr(self, "_session", None)
            if total_in and session:
                _last_tokens[session] = total_in
            log("response", path=path, status=status, session=session,
                total_in=total_in or None,
                output_tokens=usage.get("output_tokens"))


# --------------------------------------------------------------------------- #
# config + bootstrap
# --------------------------------------------------------------------------- #
def load_config():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if os.path.exists(CONFIG_FILE):
        try:
            data = json.load(open(CONFIG_FILE, encoding="utf-8"))
            for section, defaults in DEFAULTS.items():
                for k in defaults:
                    if k in data.get(section, {}):
                        cfg[section][k] = data[section][k]
        except Exception as e:
            print(f"WARN: ignoring bad {CONFIG_FILE} ({e})", flush=True)
    else:
        try:
            json.dump({k: dict(v) for k, v in DEFAULTS.items()},
                      open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass
    return cfg


def resolve_port(cli_port):
    if cli_port:
        return cli_port
    if os.environ.get("PROXY_PORT"):
        return int(os.environ["PROXY_PORT"])
    if os.path.exists(PORT_FILE):
        try:
            return int(open(PORT_FILE, encoding="utf-8").read().strip())
        except Exception:
            pass
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    open(PORT_FILE, "w", encoding="utf-8").write(str(port))
    return port


def main():
    global CFG
    ap = argparse.ArgumentParser(description="memory-inject: terse stateless context proxy")
    ap.add_argument("--port", type=int, default=None)
    for name in DEFAULTS:
        ap.add_argument(f"--no-{name}", action="store_true",
                        help=f"disable the {name} transform/feature")
    args = ap.parse_args()

    CFG = load_config()
    for name in DEFAULTS:
        if getattr(args, f"no_{name}", False):
            CFG[name]["enabled"] = False

    port = resolve_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    enabled = [n for n in DEFAULTS if CFG[n].get("enabled", True)]
    print(f"memory-inject proxy on http://127.0.0.1:{port}  "
          f"-> https://{UPSTREAM_HOST}", flush=True)
    print(f"  active: {', '.join(enabled) or '(none)'}", flush=True)
    log("proxy_start", port=port, enabled=enabled)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("proxy_stop", port=port)


if __name__ == "__main__":
    main()
