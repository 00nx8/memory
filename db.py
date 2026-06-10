#!/usr/bin/env python3
"""memory-inject DB layer -- SQLite at ~/.claude/memory/archive.db.

Three real tables + one FTS5 virtual table:

  projects (id, name UNIQUE)
      Created lazily by archive() the first time a project is seen.
      'misc' is seeded.

  categories (id, name UNIQUE, description)
      The taxonomy from this repo's pruning/categories.md, seeded on
      first run. New categories require an explicit add_category() call --
      not auto-created by classify() -- so the taxonomy can't drift.

  pruned_content (
      archive_key  TEXT PRIMARY KEY,   sha1-of-(kind,id,body) so dedup is content-aware
      category_id  INTEGER  -> categories.id   default = 'unclassified'
      project_id   INTEGER  -> projects.id     default = 'misc'
      kind         TEXT NOT NULL                'tool_use' | 'tool_result' | 'user_text' | 'assistant_text' | ...
      session      TEXT                        per-session id (lets recall scope to current session)
      block_id     TEXT                        original block id (tool_use_id, etc.) -- not unique
      bytes        INTEGER
      archived_at  TEXT NOT NULL               ISO timestamp
      pinned       INTEGER NOT NULL DEFAULT 0
      body         TEXT NOT NULL                the original block as JSON
      searchable   TEXT NOT NULL                projection used by FTS (text content extracted)
  )

  pruned_content_fts (USING fts5(searchable))  -- backs MATCH queries

Recall queries use `pruned_content_fts MATCH ?` joined to `pruned_content`
for filtering + ordering. By default `category != 'transient'` and
`session = current OR project = current`, with `--project any` to widen.

This module is pure DB. It doesn't know anything about the proxy or HTTP. The
proxy calls archive() at elision time; the MCP server / CLI call recall(),
classify(), pin().
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

# Allow override for tests; default is global at ~/.claude/memory/archive.db
DB_PATH = Path(os.environ.get(
    "MEMORY_INJECT_DB",
    str(Path.home() / ".claude" / "memory" / "archive.db")
))

# Default category set, seeded on first init. Mirrors categories.md.
DEFAULT_CATEGORIES = [
    ("unclassified", "Default. Archived by the proxy but not yet reviewed by the model."),
    ("transient",    "The proxy was right to drop. No future value. Excluded from recall by default."),
    ("decision",     "A choice made between options, with the reason."),
    ("requirement",  "A constraint or must-have stated by the user."),
    ("discovery",    "A non-obvious fact about codebase, system, environment, or data."),
    ("failed_attempt", "Something tried that didn't work, with the reason."),
    ("code_artifact", "Code written or substantially modified."),
    ("user_dialogue", "User message carrying tone, judgment, or framing."),
    ("thinking",     "Model reasoning (thinking block) dropped from an old turn. Recoverable; rarely needed."),
]


# ---------------------------------------------------------------------------
# init / connection
# ---------------------------------------------------------------------------
def _connect():
    """Return a new connection. Caller closes. Schema is idempotent so calling
    _init() here is cheap; we still call it because the DB may have been
    created in a prior process."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _init(conn):
    """Create schema if missing. Idempotent."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS pruned_content (
            archive_key  TEXT PRIMARY KEY,
            category_id  INTEGER NOT NULL,
            project_id   INTEGER NOT NULL,
            kind         TEXT NOT NULL,
            session      TEXT,
            block_id     TEXT,
            bytes        INTEGER,
            archived_at  TEXT NOT NULL,
            pinned       INTEGER NOT NULL DEFAULT 0,
            body         TEXT NOT NULL,
            searchable   TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (project_id)  REFERENCES projects(id)
        );

        CREATE INDEX IF NOT EXISTS idx_category ON pruned_content (category_id);
        CREATE INDEX IF NOT EXISTS idx_project  ON pruned_content (project_id);
        CREATE INDEX IF NOT EXISTS idx_session  ON pruned_content (session);
        CREATE INDEX IF NOT EXISTS idx_pinned   ON pruned_content (pinned);
        CREATE INDEX IF NOT EXISTS idx_at       ON pruned_content (archived_at);

        CREATE VIRTUAL TABLE IF NOT EXISTS pruned_content_fts
        USING fts5(searchable, content='pruned_content',
                   content_rowid='rowid', tokenize='porter unicode61');

        CREATE TRIGGER IF NOT EXISTS pruned_content_ai AFTER INSERT ON pruned_content
            BEGIN
                INSERT INTO pruned_content_fts(rowid, searchable)
                VALUES (new.rowid, new.searchable);
            END;
        CREATE TRIGGER IF NOT EXISTS pruned_content_ad AFTER DELETE ON pruned_content
            BEGIN
                INSERT INTO pruned_content_fts(pruned_content_fts, rowid, searchable)
                VALUES ('delete', old.rowid, old.searchable);
            END;
        CREATE TRIGGER IF NOT EXISTS pruned_content_au AFTER UPDATE ON pruned_content
            BEGIN
                INSERT INTO pruned_content_fts(pruned_content_fts, rowid, searchable)
                VALUES ('delete', old.rowid, old.searchable);
                INSERT INTO pruned_content_fts(rowid, searchable)
                VALUES (new.rowid, new.searchable);
            END;
    """)
    # Seed projects + categories on first init.
    conn.execute("INSERT OR IGNORE INTO projects (name) VALUES ('misc')")
    for name, desc in DEFAULT_CATEGORIES:
        conn.execute("INSERT OR IGNORE INTO categories (name, description) VALUES (?, ?)",
                     (name, desc))
    conn.commit()


def init():
    """Open + initialize. Safe to call repeatedly; cheap when already set up."""
    conn = _connect()
    _init(conn)
    return conn


# ---------------------------------------------------------------------------
# project + category helpers
# ---------------------------------------------------------------------------
def _project_id(conn, name):
    """Resolve project name to id, creating if absent. Always returns an int."""
    cur = conn.execute("SELECT id FROM projects WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
    return cur.lastrowid


def _category_id(conn, name):
    """Resolve category name to id. Does NOT auto-create (taxonomy is explicit).
    Returns None if the category doesn't exist."""
    cur = conn.execute("SELECT id FROM categories WHERE name = ?", (name,))
    row = cur.fetchone()
    return row["id"] if row else None


def add_category(conn, name, description):
    """Explicit category addition. Returns id."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO categories (name, description) VALUES (?, ?)",
        (name.lower(), description),
    )
    return _category_id(conn, name.lower())


def list_categories(conn):
    return list(conn.execute("SELECT name, description FROM categories ORDER BY name"))


def list_projects(conn):
    return list(conn.execute("SELECT name FROM projects ORDER BY name"))


# ---------------------------------------------------------------------------
# archive_key + searchable projection
# ---------------------------------------------------------------------------
def archive_key(kind, block_id, body):
    """Content-hashed dedup key. Same key = same row, even across sessions."""
    h = sha1()
    h.update((kind or "").encode("utf-8"))
    h.update(b"\0")
    h.update((block_id or "").encode("utf-8"))
    h.update(b"\0")
    if isinstance(body, str):
        h.update(body.encode("utf-8"))
    else:
        h.update(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _extract_searchable(kind, body):
    """Project a block down to plain text for FTS. The body itself stays in
    `body` as JSON; this column is only what MATCH searches."""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        # tool_use input: just JSON-stringify the input
        if kind == "tool_use" and "input" in body:
            return json.dumps(body["input"], ensure_ascii=False)
        # tool_result: content can be str or list of blocks
        if "content" in body:
            c = body["content"]
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                parts = []
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        parts.append(blk.get("text", ""))
                return "\n".join(parts) or json.dumps(c, ensure_ascii=False)
        # thinking block: index the reasoning text, not the signature blob
        if body.get("type") == "thinking":
            return body.get("thinking", "")
        if body.get("type") == "redacted_thinking":
            return ""  # encrypted payload -- nothing to index
        # text block
        if body.get("type") == "text":
            return body.get("text", "")
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# archive / classify / pin
# ---------------------------------------------------------------------------
def archive(conn, *, kind, body, session=None, block_id=None,
            project="misc", category="unclassified"):
    """Insert one row. If the (kind, block_id, body)-hash is already present,
    this is a no-op and returns ('existing', archive_key). Otherwise inserts
    and returns ('new', archive_key)."""
    key = archive_key(kind, block_id, body)
    cur = conn.execute("SELECT archive_key FROM pruned_content WHERE archive_key = ?",
                       (key,))
    if cur.fetchone():
        return ("existing", key)

    cat_id = _category_id(conn, category) or _category_id(conn, "unclassified")
    proj_id = _project_id(conn, project)
    body_json = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    nbytes = len(body_json.encode("utf-8"))
    searchable = _extract_searchable(kind, body)
    ts = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        INSERT INTO pruned_content
            (archive_key, category_id, project_id, kind, session, block_id,
             bytes, archived_at, pinned, body, searchable)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (key, cat_id, proj_id, kind, session, block_id,
          nbytes, ts, body_json, searchable))
    conn.commit()
    return ("new", key)


def resolve_key(conn, prefix):
    """Resolve an archive_key prefix to its full value.

    Returns the full key string if exactly one row matches, raises KeyError
    on no match, and AmbiguousKeyError on multiple matches.
    """
    if not prefix:
        raise KeyError("empty prefix")
    rows = list(conn.execute(
        "SELECT archive_key FROM pruned_content WHERE archive_key LIKE ? LIMIT 5",
        (prefix + "%",),
    ))
    if not rows:
        raise KeyError(f"no archive_key matches prefix {prefix!r}")
    if len(rows) > 1:
        raise AmbiguousKeyError(
            f"prefix {prefix!r} matches multiple rows: "
            + ", ".join(r["archive_key"][:16] + "..." for r in rows)
        )
    return rows[0]["archive_key"]


class AmbiguousKeyError(Exception):
    pass


def classify(conn, archive_key, category):
    """Reclassify one row. Used by the model to move things out of
    'unclassified' into a real category, or to 'transient'.

    Accepts a prefix; raises KeyError if no match, AmbiguousKeyError if
    multiple."""
    cat_id = _category_id(conn, category)
    if cat_id is None:
        raise ValueError(f"unknown category: {category!r}")
    full = resolve_key(conn, archive_key)
    conn.execute("UPDATE pruned_content SET category_id = ? WHERE archive_key = ?",
                 (cat_id, full))
    conn.commit()
    return full


def pin(conn, archive_key, pinned=True):
    """Mark a row pinned (or unpin). Pinned rows are never auto-elided by the
    proxy. Accepts a prefix."""
    full = resolve_key(conn, archive_key)
    conn.execute("UPDATE pruned_content SET pinned = ? WHERE archive_key = ?",
                 (1 if pinned else 0, full))
    conn.commit()
    return full


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------
_FTS_RESERVED = {"AND", "OR", "NOT", "NEAR"}


def _normalize_fts_query(q):
    """FTS5 with unicode61 tokenizer treats `-`, `.`, etc. as word separators
    AND parses bare hyphenated terms as column-references (so 'page-builder'
    becomes "WHERE 'builder' EXISTS in the column 'page'", which errors).

    Quote each non-reserved bare term in double-quotes to force literal
    matching. Operators (OR / AND / NOT / NEAR / "..." already-quoted) pass
    through. A leading `+` is also allowed (the FTS5 "include" prefix), but we
    don't generate one here.

    Examples:
        "page-builder"                -> '"page-builder"'
        "first-party OR whitespace"   -> '"first-party" OR "whitespace"'
        '"already quoted"'            -> '"already quoted"'
        "(a OR b) NEAR/3 c"           -> passes through largely untouched
    """
    if not q:
        return q
    # Already contains FTS5 syntax (quotes / parens / column prefix) -> trust caller.
    if any(ch in q for ch in '"():'):
        return q
    out = []
    for tok in q.split():
        if tok.upper() in _FTS_RESERVED or tok.upper().startswith("NEAR/"):
            out.append(tok.upper())
        else:
            # Strip surrounding punctuation that's safe to drop.
            safe = tok.strip(",;.!?")
            if not safe:
                continue
            out.append(f'"{safe}"')
    return " ".join(out)


def recall(conn, *, query=None, category=None, project=None, session=None,
           exclude_transient=True, limit=20, include_pinned=True):
    """Find archived rows matching the criteria.

    query: FTS5 MATCH expression (e.g. "decision OR requirement", "first-party")
           If None, returns all rows matching the other filters.
    category: 'decision' | 'requirement' | ... or None for any non-transient.
              Pass 'any' to disable the category filter entirely.
              Pass 'transient' explicitly to see noise.
    project: project name, 'any' for all projects, or None to default-scope.
    session: session id, or None for any.
    exclude_transient: default True. Set False if you want noise in results.
    include_pinned: if True, pinned rows always come back (subject to other filters)
                    even if they wouldn't otherwise match -- they're always relevant.
    limit: result count cap.

    Returns list of dicts with archive_key, category, project, kind, body, etc.
    """
    # Build a `match_clause` that captures all filters EXCEPT the pin-relevance
    # rule. Then the outer WHERE is `(match_clause) OR (pinned and in-scope)`,
    # so pinned rows in the right project are always considered relevant.
    match = []
    params = []

    if query:
        match.append("pruned_content.rowid IN "
                     "(SELECT rowid FROM pruned_content_fts WHERE pruned_content_fts MATCH ?)")
        params.append(_normalize_fts_query(query))

    if category and category != "any":
        match.append("categories.name = ?")
        params.append(category)
    elif exclude_transient:
        match.append("categories.name != 'transient'")

    if session:
        match.append("session = ?")
        params.append(session)

    if project and project != "any":
        match.append("projects.name = ?")
        params.append(project)

    match_sql = (" AND ".join(match)) if match else "1=1"

    # Pinned-in-scope branch: only if include_pinned AND a real project was
    # specified. (Pinning is meaningless for project='any'.)
    if include_pinned and project and project != "any":
        outer_where = f"(({match_sql}) OR (pinned = 1 AND projects.name = ?))"
        params.append(project)
    else:
        outer_where = match_sql

    sql = f"""
        SELECT pruned_content.archive_key, pruned_content.kind, pruned_content.session,
               pruned_content.block_id, pruned_content.bytes, pruned_content.archived_at,
               pruned_content.pinned, pruned_content.body, categories.name AS category,
               projects.name AS project
        FROM pruned_content
        JOIN categories ON pruned_content.category_id = categories.id
        JOIN projects   ON pruned_content.project_id  = projects.id
        WHERE {outer_where}
        ORDER BY pinned DESC, archived_at DESC
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        try:
            body = json.loads(r["body"])
        except Exception:
            body = r["body"]
        out.append({
            "archive_key": r["archive_key"],
            "category": r["category"],
            "project": r["project"],
            "kind": r["kind"],
            "session": r["session"],
            "block_id": r["block_id"],
            "bytes": r["bytes"],
            "archived_at": r["archived_at"],
            "pinned": bool(r["pinned"]),
            "body": body,
        })
    return out


def list_unclassified(conn, *, project=None, limit=50):
    """Convenience: rows still tagged 'unclassified', for triage."""
    return recall(conn, category="unclassified", project=project,
                  exclude_transient=False, limit=limit, include_pinned=False)


def get(conn, archive_key):
    """Fetch one row by key (or unambiguous prefix). Returns None if missing,
    raises AmbiguousKeyError on multi-match."""
    try:
        full = resolve_key(conn, archive_key)
    except KeyError:
        return None
    for r in conn.execute("""
        SELECT pruned_content.archive_key, pruned_content.kind, pruned_content.session,
               pruned_content.block_id, pruned_content.bytes, pruned_content.archived_at,
               pruned_content.pinned, pruned_content.body, categories.name AS category,
               projects.name AS project
        FROM pruned_content
        JOIN categories ON pruned_content.category_id = categories.id
        JOIN projects   ON pruned_content.project_id  = projects.id
        WHERE pruned_content.archive_key = ?
    """, (full,)):
        try:
            body = json.loads(r["body"])
        except Exception:
            body = r["body"]
        return {
            "archive_key": r["archive_key"], "category": r["category"],
            "project": r["project"], "kind": r["kind"], "session": r["session"],
            "block_id": r["block_id"], "bytes": r["bytes"],
            "archived_at": r["archived_at"], "pinned": bool(r["pinned"]),
            "body": body,
        }
    return None


# ---------------------------------------------------------------------------
# migration from archive.jsonl
# ---------------------------------------------------------------------------
def migrate_from_jsonl(conn, jsonl_path):
    """One-shot: read every row from an archive.jsonl file and insert into the
    DB. Idempotent thanks to archive_key dedup -- re-running migrate doesn't
    duplicate rows.

    Returns (new_count, existing_count, error_count).
    """
    new_n = existing_n = err_n = 0
    if not Path(jsonl_path).exists():
        return (0, 0, 0)
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                err_n += 1
                continue
            try:
                status, _ = archive(
                    conn,
                    kind=rec.get("kind", "unknown"),
                    body=rec.get("original"),
                    session=rec.get("session"),
                    block_id=rec.get("id"),
                    project=rec.get("project") or "misc",
                    category=rec.get("category") or "unclassified",
                )
                if status == "new":
                    new_n += 1
                else:
                    existing_n += 1
            except Exception:
                err_n += 1
    return (new_n, existing_n, err_n)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Smoke test the schema + a roundtrip.
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["MEMORY_INJECT_DB"] = tmp.name
    DB_PATH = Path(tmp.name)

    conn = init()
    print(f"DB: {tmp.name}")
    print(f"  categories: {[r['name'] for r in list_categories(conn)]}")
    print(f"  projects:   {[r['name'] for r in list_projects(conn)]}")

    # archive a tool_result
    status, key = archive(
        conn,
        kind="tool_result",
        body={"type": "tool_result", "tool_use_id": "tu_test",
              "content": "the build briefs say BB-001 is page-builder rescue"},
        session="sess_abc", block_id="tu_test",
        project="memory-inject", category="unclassified",
    )
    print(f"  archive(): {status}  key={key[:16]}...")

    # Re-archive the same content -- should dedup.
    status2, _ = archive(
        conn, kind="tool_result",
        body={"type": "tool_result", "tool_use_id": "tu_test",
              "content": "the build briefs say BB-001 is page-builder rescue"},
        session="sess_abc", block_id="tu_test", project="memory-inject",
    )
    print(f"  archive() again: {status2}  (expect 'existing')")

    # classify it as a discovery
    classify(conn, key, "discovery")
    print(f"  classify({key[:16]}..., 'discovery'): ok")

    # recall via FTS
    hits = recall(conn, query="page-builder", project="memory-inject")
    print(f"  recall(query='page-builder', project='memory-inject'): {len(hits)} hit(s)")
    for h in hits:
        print(f"    -> {h['category']}/{h['kind']} archived={h['archived_at'][:19]}")

    # recall everything for project
    hits = recall(conn, project="memory-inject", limit=10)
    print(f"  recall(project='memory-inject') no query: {len(hits)} hit(s)")

    # pin
    pin(conn, key)
    print(f"  pin({key[:16]}...): ok")

    conn.close()
    os.unlink(tmp.name)
    print("OK.")
