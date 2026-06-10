#!/usr/bin/env python3
"""memory-inject migration -- one-shot seed of the SQLite DB from archive.jsonl.

The proxy writes elided originals to `archive.jsonl` as it runs (a portable,
human-readable log). This script walks that file and inserts every row into
the SQLite DB at $MEMORY_INJECT_DB (default ~/.claude/memory/archive.db).

It is idempotent: archive_key dedup means re-running the migration after the
proxy has appended new rows only inserts the new ones. So a reasonable
workflow is:

    python migrate.py                 # initial seed
    # ... proxy runs for a week, archive.jsonl grows ...
    python migrate.py                 # catch up incremental rows

Or run it on a timer if you want the DB to stay near-current.

Usage:
    python migrate.py                 # migrate archive.jsonl in this directory
    python migrate.py /path/to/archive.jsonl
    python migrate.py --db /tmp/test.db archive.jsonl
"""
import argparse
import os
import sys
from pathlib import Path

import db


def main():
    ap = argparse.ArgumentParser(description="seed the memory-inject DB from archive.jsonl")
    ap.add_argument("jsonl", nargs="?",
                    default=str(Path(__file__).parent / "archive.jsonl"),
                    help="path to archive.jsonl (default: archive.jsonl next to this script)")
    ap.add_argument("--db", default=None,
                    help="override DB path (default: $MEMORY_INJECT_DB or ~/.claude/memory/archive.db)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-batch progress")
    args = ap.parse_args()

    if args.db:
        os.environ["MEMORY_INJECT_DB"] = args.db
        # Re-resolve after env override.
        db.DB_PATH = Path(args.db)

    src = Path(args.jsonl)
    if not src.exists():
        print(f"no such file: {src}", file=sys.stderr)
        sys.exit(1)

    print(f"source: {src}")
    print(f"target: {db.DB_PATH}")

    conn = db.init()
    new_n, existing_n, err_n = db.migrate_from_jsonl(conn, str(src))
    conn.close()

    print(f"  new:      {new_n}")
    print(f"  existing: {existing_n}  (already in DB)")
    print(f"  errors:   {err_n}")
    print("done.")


if __name__ == "__main__":
    main()
