#!/usr/bin/env python3
"""memory-inject MCP server -- expose archive DB verbs to the model in-conversation.

Stdio MCP server. Tools:

    recall            FTS + filter search on the DB
    archive           insert a row by hand (model curates)
    classify          change a row's category
    pin               toggle the pinned flag
    list_categories   what categories exist
    list_projects     what projects exist
    list_unclassified rows still untriaged
    show              fetch one row by archive_key prefix

Wire up by adding to the client's MCP config:

    {
      "mcpServers": {
        "memory-inject": {
          "command": "python",
          "args": ["/path/to/memory-inject/mcp_server.py"]
        }
      }
    }

The server speaks the standard MCP protocol over stdin/stdout. It uses the
`mcp` Python SDK if available, with a hand-rolled JSON-RPC fallback if not so
the file works as a single-file dependency.

DB path: $MEMORY_INJECT_DB or the default (~/.claude/memory/archive.db).
"""
import json
import sys
import traceback

import db


SERVER_NAME = "memory-inject"
SERVER_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# tool implementations -- thin wrappers over db.py
# ---------------------------------------------------------------------------
def tool_recall(args):
    """Search the archive DB."""
    conn = db.init()
    try:
        rows = db.recall(
            conn,
            query=args.get("query"),
            category=args.get("category"),
            project=args.get("project"),
            session=args.get("session"),
            exclude_transient=not args.get("include_transient", False),
            limit=int(args.get("limit", 20)),
            include_pinned=not args.get("no_pin_boost", False),
        )
        return {"hits": len(rows), "rows": rows}
    finally:
        conn.close()


def tool_archive(args):
    """Manually archive a piece of content. The model curates."""
    if "body" not in args:
        raise ValueError("body is required")
    body = args["body"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass  # treat as plain text
    conn = db.init()
    try:
        status, key = db.archive(
            conn,
            kind=args.get("kind", "user_text"),
            body=body,
            session=args.get("session"),
            block_id=args.get("block_id"),
            project=args.get("project", "misc"),
            category=args.get("category", "unclassified"),
        )
        return {"status": status, "archive_key": key}
    finally:
        conn.close()


def tool_classify(args):
    """Change a row's category (accepts archive_key prefix)."""
    conn = db.init()
    try:
        full = db.classify(conn, args["archive_key"], args["category"])
        return {"archive_key": full, "category": args["category"]}
    finally:
        conn.close()


def tool_pin(args):
    """Pin (or unpin) a row (accepts prefix). Pinned rows are auto-included
    in inject() for their project and never auto-elided by the proxy."""
    conn = db.init()
    try:
        full = db.pin(conn, args["archive_key"], pinned=not args.get("unpin", False))
        return {"archive_key": full, "pinned": not args.get("unpin", False)}
    finally:
        conn.close()


def tool_list_categories(args):
    conn = db.init()
    try:
        return {"categories": [dict(r) for r in db.list_categories(conn)]}
    finally:
        conn.close()


def tool_list_projects(args):
    conn = db.init()
    try:
        return {"projects": [dict(r) for r in db.list_projects(conn)]}
    finally:
        conn.close()


def tool_list_unclassified(args):
    """Rows still tagged 'unclassified' -- the model triages these into real
    categories or 'transient'."""
    conn = db.init()
    try:
        rows = db.list_unclassified(
            conn,
            project=args.get("project"),
            limit=int(args.get("limit", 50)),
        )
        return {"hits": len(rows), "rows": rows}
    finally:
        conn.close()


def tool_show(args):
    """Fetch one row in full (accepts archive_key prefix)."""
    conn = db.init()
    try:
        row = db.get(conn, args["archive_key"])
        if row is None:
            raise ValueError(f"no row matching {args['archive_key']!r}")
        return row
    finally:
        conn.close()


def tool_add_category(args):
    conn = db.init()
    try:
        cat_id = db.add_category(conn, args["name"], args.get("description", ""))
        return {"id": cat_id, "name": args["name"]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tool registry
# ---------------------------------------------------------------------------
TOOLS = {
    "recall": {
        "fn": tool_recall,
        "description": (
            "Search the memory-inject archive DB. Returns rows matching the "
            "criteria, sorted with pinned rows first. By default excludes "
            "category='transient'. When 'project' is omitted, scopes to the "
            "current project from the proxy's context; pass 'any' to widen. "
            "FTS query supports OR/AND/NOT/NEAR operators; hyphenated terms "
            "are auto-quoted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "FTS5 search expression (e.g. 'proxy' or 'decision OR requirement')"},
                "category": {"type": "string",
                             "description": "Exact category, or 'any' to disable category filter"},
                "project": {"type": "string",
                            "description": "Exact project, or 'any' to disable project filter"},
                "session": {"type": "string",
                            "description": "Restrict to one session id"},
                "limit": {"type": "integer", "default": 20},
                "include_transient": {"type": "boolean", "default": False},
                "no_pin_boost": {"type": "boolean", "default": False,
                                 "description": "Don't auto-include pinned rows for the project"},
            },
        },
    },
    "archive": {
        "fn": tool_archive,
        "description": (
            "Manually archive a piece of content as a DB row. Used when the "
            "model decides something is worth keeping (a decision, a "
            "requirement, a discovery). Body can be a string or a JSON object. "
            "Defaults: category='unclassified', project='misc', kind='user_text'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"description": "the content to archive (string or object)"},
                "kind": {"type": "string", "default": "user_text"},
                "category": {"type": "string", "default": "unclassified"},
                "project": {"type": "string", "default": "misc"},
                "session": {"type": "string"},
                "block_id": {"type": "string"},
            },
            "required": ["body"],
        },
    },
    "classify": {
        "fn": tool_classify,
        "description": (
            "Change a row's category. The archive_key may be a prefix as long "
            "as it's unambiguous. Use 'transient' to mark a row as noise so "
            "future recalls exclude it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "archive_key": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["archive_key", "category"],
        },
    },
    "pin": {
        "fn": tool_pin,
        "description": (
            "Pin (default) or unpin a row. Pinned rows are auto-included by "
            "inject() for their project and are never elided by the proxy's "
            "prune/compact steps. Use sparingly -- pinned rows consume context "
            "budget every turn."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "archive_key": {"type": "string"},
                "unpin": {"type": "boolean", "default": False},
            },
            "required": ["archive_key"],
        },
    },
    "list_categories": {
        "fn": tool_list_categories,
        "description": "List the categories that exist in the DB.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "list_projects": {
        "fn": tool_list_projects,
        "description": "List the projects that have rows in the DB.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "list_unclassified": {
        "fn": tool_list_unclassified,
        "description": (
            "List rows still tagged 'unclassified' (archived by the proxy but "
            "not yet reviewed). Use this for triage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    "show": {
        "fn": tool_show,
        "description": (
            "Fetch one row in full. The archive_key may be a prefix as long "
            "as it's unambiguous."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"archive_key": {"type": "string"}},
            "required": ["archive_key"],
        },
    },
    "add_category": {
        "fn": tool_add_category,
        "description": (
            "Create a new category. Use sparingly -- the existing six "
            "(decision, requirement, discovery, failed_attempt, code_artifact, "
            "user_dialogue) cover most things, and a fragmented taxonomy hurts "
            "recall."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio (MCP wire protocol)
#
# We hand-roll a minimal subset of the MCP spec: initialize, tools/list,
# tools/call, plus the standard JSON-RPC envelope. This avoids a hard dep on
# the mcp Python SDK and keeps the file standalone. The dialect matches what
# Claude Desktop and Claude Code accept.
# ---------------------------------------------------------------------------
def _send(message):
    """Write one JSON-RPC message to stdout, framed by Content-Length headers
    OR as a single line of JSON (newline-delimited). MCP-over-stdio uses NDJSON
    in practice."""
    raw = json.dumps(message, ensure_ascii=False)
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def _err(rpc_id, code, message):
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": code, "message": message}}


def _result(rpc_id, value):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": value}


def _tool_list_response():
    tools = []
    for name, info in TOOLS.items():
        tools.append({
            "name": name,
            "description": info["description"],
            "inputSchema": info["inputSchema"],
        })
    return {"tools": tools}


def _tool_call_response(name, args):
    if name not in TOOLS:
        raise ValueError(f"unknown tool: {name}")
    out = TOOLS[name]["fn"](args or {})
    # MCP tool results return a list of content blocks. Wrap our JSON.
    return {"content": [{"type": "text",
                         "text": json.dumps(out, ensure_ascii=False, indent=2)}]}


def handle(request):
    """Dispatch one JSON-RPC request to its handler. Returns response dict."""
    method = request.get("method")
    rpc_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None  # notification, no reply expected
    if method == "tools/list":
        return _result(rpc_id, _tool_list_response())
    if method == "tools/call":
        try:
            value = _tool_call_response(params.get("name"), params.get("arguments"))
            return _result(rpc_id, value)
        except Exception as e:
            return _err(rpc_id, -32000, f"{type(e).__name__}: {e}")
    if method == "ping":
        return _result(rpc_id, {})
    if method == "shutdown":
        return _result(rpc_id, {})

    return _err(rpc_id, -32601, f"method not found: {method}")


def main():
    # Allow $MEMORY_INJECT_DB override; db.py picks it up at init.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _send(_err(None, -32700, f"parse error: {e}"))
            continue
        try:
            resp = handle(req)
        except Exception as e:
            tb = traceback.format_exc()
            resp = _err(req.get("id"), -32000, f"{type(e).__name__}: {e}\n{tb}")
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
