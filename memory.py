#!/usr/bin/env python3
"""memory-inject CLI -- manage the archive DB from the shell.

Subcommands:

  recall          search the DB
  archive         insert a row directly (manual, not via the proxy)
  classify        change the category of a row
  pin             toggle the pinned flag on a row
  add-category    create a new category
  list            list categories / projects / unclassified rows
  show            print one row in full

All subcommands take --db to override the default. The default is
$MEMORY_INJECT_DB or ~/.claude/memory/archive.db.

Examples:

  # See what's still untriaged, in this project, most recent first.
  python memory.py list unclassified --project=memory-inject

  # Look up everything tagged as a decision touching "FTS".
  python memory.py recall --category=decision --query=FTS

  # Tag a specific row as a decision.
  python memory.py classify <archive_key> decision

  # Add an entry by hand (rare; the proxy normally does this).
  python memory.py archive --kind=user_text --category=requirement \\
      --project=memory-inject --body="must work offline"

  # Pin a row so the proxy never auto-elides it.
  python memory.py pin <archive_key>
"""
import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

import db

# Force UTF-8 on stdout so we can print archived bodies that contain non-cp1252
# characters (arrows, smart quotes, em-dashes, German umlauts) on Windows
# without UnicodeEncodeError. No-op on platforms whose default is already UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _short(s, n=80):
    s = (s or "").replace("\n", "  ")
    return s if len(s) <= n else s[: n - 1] + "..."


def _summarize_body(body, kind):
    """One-line preview of a row's body, for the table view."""
    if isinstance(body, dict):
        if kind == "tool_use" and "input" in body:
            return _short(json.dumps(body["input"], ensure_ascii=False))
        if "content" in body:
            c = body["content"]
            if isinstance(c, str):
                return _short(c)
            if isinstance(c, list):
                parts = [b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text"]
                return _short(" ".join(parts) or json.dumps(c, ensure_ascii=False))
        if body.get("type") == "text":
            return _short(body.get("text", ""))
        return _short(json.dumps(body, ensure_ascii=False))
    return _short(str(body))


def _print_table(rows):
    """Compact tabular view of recall results.

    First column is the 12-char key prefix (unique within the DB for any
    reasonable archive size; pass to classify/pin/show as a prefix).
    A leading * marks a pinned row.
    """
    if not rows:
        print("(no matches)")
        return
    print(f"{'pin':3} {'key':13} {'cat':14} {'project':18} {'kind':14} preview")
    print("-" * 110)
    for r in rows:
        pin_marker = " * " if r.get("pinned") else "   "
        print(f"{pin_marker}{r['archive_key'][:12]}  "
              f"{r['category']:14} {r['project']:18} {r['kind']:14} "
              f"{_summarize_body(r['body'], r['kind'])}")


# ---------------------------------------------------------------------------
# subcommand handlers
# ---------------------------------------------------------------------------
def cmd_recall(args, conn):
    rows = db.recall(
        conn,
        query=args.query,
        category=args.category,
        project=args.project,
        session=args.session,
        exclude_transient=not args.include_transient,
        limit=args.limit,
        include_pinned=not args.no_pin_boost,
    )
    _print_table(rows)


def cmd_archive(args, conn):
    # Body comes in as raw text or JSON; we try JSON first then fall back.
    body_text = args.body
    if body_text == "-":
        body_text = sys.stdin.read()
    try:
        body = json.loads(body_text)
    except Exception:
        body = body_text  # plain text body
    status, key = db.archive(
        conn,
        kind=args.kind,
        body=body,
        session=args.session,
        block_id=args.block_id,
        project=args.project,
        category=args.category,
    )
    print(f"{status}  {key}")


def cmd_classify(args, conn):
    try:
        full = db.classify(conn, args.archive_key, args.category)
        print(f"classified {full[:12]} as {args.category}")
    except (ValueError, KeyError, db.AmbiguousKeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_pin(args, conn):
    try:
        full = db.pin(conn, args.archive_key, pinned=not args.unpin)
    except (KeyError, db.AmbiguousKeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    state = "unpinned" if args.unpin else "pinned"
    print(f"{state} {full[:12]}")


def cmd_add_category(args, conn):
    cat_id = db.add_category(conn, args.name, args.description)
    print(f"category id={cat_id} name={args.name!r}")


def cmd_list(args, conn):
    if args.what == "categories":
        for r in db.list_categories(conn):
            print(f"  {r['name']:18} -- {r['description'] or ''}")
    elif args.what == "projects":
        for r in db.list_projects(conn):
            print(f"  {r['name']}")
    elif args.what == "unclassified":
        rows = db.list_unclassified(conn, project=args.project, limit=args.limit)
        _print_table(rows)
    else:
        print(f"unknown list target: {args.what!r}", file=sys.stderr)
        sys.exit(2)


def cmd_triage(args, conn):
    """Walk unclassified rows one at a time, prompting for a category.

    Quick interactive loop. Each row shows its kind, project, bytes, and a
    preview, then prompts. Replies:

      d, decision      -> classify decision
      r, requirement   -> classify requirement
      i, discovery     -> classify discovery
      f, failed        -> classify failed_attempt
      c, code          -> classify code_artifact
      u, user          -> classify user_dialogue
      t, transient     -> classify transient   (the noise tier)
      p, pin           -> pin + ask category again
      s, skip          -> leave as unclassified, move on
      q, quit          -> stop the loop

    Any other reply is treated as a literal category name (lets the model
    use 'methodology' or any other added category by name).
    """
    rows = db.list_unclassified(conn, project=args.project, limit=args.limit)
    if not rows:
        print("nothing to triage.")
        return
    short = {
        "d": "decision", "decision": "decision",
        "r": "requirement", "requirement": "requirement",
        "i": "discovery", "discovery": "discovery",
        "f": "failed", "failed": "failed_attempt", "failed_attempt": "failed_attempt",
        "c": "code", "code": "code_artifact", "code_artifact": "code_artifact",
        "u": "user", "user": "user_dialogue", "user_dialogue": "user_dialogue",
        "t": "transient", "transient": "transient",
    }
    total = len(rows)
    classified = 0
    skipped = 0
    for i, r in enumerate(rows, 1):
        print()
        print(f"[{i}/{total}]  {r['archive_key'][:12]}  "
              f"{r['kind']}  {r['bytes']}B  ({r['project']})")
        print(f"  {_summarize_body(r['body'], r['kind'])[:200]}")
        while True:
            try:
                resp = input("  > ").strip().lower()
            except EOFError:
                print()
                return
            if resp in ("", "s", "skip"):
                skipped += 1
                break
            if resp in ("q", "quit"):
                print(f"stopped. classified {classified}, skipped {skipped}.")
                return
            if resp in ("p", "pin"):
                db.pin(conn, r["archive_key"])
                print("  pinned. now pick a category:")
                continue
            cat = short.get(resp, resp)
            try:
                db.classify(conn, r["archive_key"], cat)
                classified += 1
                print(f"  -> {cat}")
                break
            except ValueError as e:
                print(f"  {e}. try again or 's' to skip.")
    print(f"\ndone. classified {classified}, skipped {skipped}.")


def cmd_triage_summary(args, conn):
    """Per-category counts, for at-a-glance progress."""
    rows = conn.execute("""
        SELECT categories.name, COUNT(*) AS n
        FROM pruned_content
        JOIN categories ON pruned_content.category_id = categories.id
        GROUP BY categories.name
        ORDER BY n DESC
    """).fetchall()
    if not rows:
        print("(no rows)")
        return
    for r in rows:
        print(f"  {r['name']:18} {r['n']}")


def cmd_show(args, conn):
    try:
        row = db.get(conn, args.archive_key)
    except db.AmbiguousKeyError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    if row is None:
        print(f"no row matching key {args.archive_key!r}", file=sys.stderr)
        sys.exit(1)
    print(f"key       {row['archive_key']}")
    print(f"category  {row['category']}{'  (pinned)' if row['pinned'] else ''}")
    print(f"project   {row['project']}")
    print(f"kind      {row['kind']}")
    print(f"session   {row['session']}")
    print(f"block_id  {row['block_id']}")
    print(f"bytes     {row['bytes']}")
    print(f"archived  {row['archived_at']}")
    print(f"body:")
    if isinstance(row["body"], str):
        print(textwrap.indent(row["body"], "  "))
    else:
        print(textwrap.indent(json.dumps(row["body"], ensure_ascii=False, indent=2), "  "))


# ---------------------------------------------------------------------------
# argument plumbing
# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        description="memory-inject CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--db", help="override DB path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # recall
    p = sub.add_parser("recall", help="search the DB")
    p.add_argument("--query", help="FTS5 expression (hyphens auto-quoted)")
    p.add_argument("--category", help="exact category, or 'any'")
    p.add_argument("--project", help="exact project, or 'any'")
    p.add_argument("--session", help="restrict to one session id")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-transient", action="store_true",
                   help="don't filter out category=transient")
    p.add_argument("--no-pin-boost", action="store_true",
                   help="don't auto-include pinned rows for the project")
    p.set_defaults(func=cmd_recall)

    # archive (manual insert)
    p = sub.add_parser("archive", help="manually insert one row")
    p.add_argument("--kind", required=True,
                   help="block kind (tool_use, tool_result, user_text, assistant_text, ...)")
    p.add_argument("--body", required=True,
                   help="raw body, or '-' to read stdin; JSON is parsed if it parses")
    p.add_argument("--session")
    p.add_argument("--block-id")
    p.add_argument("--project", default="misc")
    p.add_argument("--category", default="unclassified")
    p.set_defaults(func=cmd_archive)

    # classify
    p = sub.add_parser("classify", help="change a row's category")
    p.add_argument("archive_key")
    p.add_argument("category")
    p.set_defaults(func=cmd_classify)

    # pin
    p = sub.add_parser("pin", help="pin (default) or unpin a row")
    p.add_argument("archive_key")
    p.add_argument("--unpin", action="store_true")
    p.set_defaults(func=cmd_pin)

    # add-category
    p = sub.add_parser("add-category", help="create a new category")
    p.add_argument("name")
    p.add_argument("description")
    p.set_defaults(func=cmd_add_category)

    # list
    p = sub.add_parser("list", help="list categories, projects, or unclassified rows")
    p.add_argument("what", choices=["categories", "projects", "unclassified"])
    p.add_argument("--project", help="for 'unclassified', scope to one project")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    # triage
    p = sub.add_parser("triage", help="walk unclassified rows, classify each")
    p.add_argument("--project", help="scope to one project (default: all)")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_triage)

    # triage-summary
    p = sub.add_parser("triage-summary", help="per-category counts")
    p.set_defaults(func=cmd_triage_summary)

    # show
    p = sub.add_parser("show", help="print one row in full")
    p.add_argument("archive_key")
    p.set_defaults(func=cmd_show)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.db:
        os.environ["MEMORY_INJECT_DB"] = args.db
        db.DB_PATH = Path(args.db)

    conn = db.init()
    try:
        args.func(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
