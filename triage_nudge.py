#!/usr/bin/env python3
"""memory-inject triage nudge -- UserPromptSubmit hook.

Prints a one-line reminder when archived rows await classification, so the
unclassified queue gets cleared a few at a time instead of piling up.

Global-safe: read-only DB access, silent + exit 0 if the DB is absent or
unreadable, so it never disrupts sessions that don't use the proxy.
"""
import os
import sqlite3
import sys

THRESHOLD = 1  # emit when unclassified count >= this

db = os.environ.get("MEMORY_INJECT_DB") or os.path.expanduser(
    "~/.claude/memory/archive.db"
)
try:
    if os.path.exists(db):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = con.execute(
            "SELECT count(*) FROM pruned_content WHERE category_id="
            "(SELECT id FROM categories WHERE name='unclassified')"
        ).fetchone()[0]
        con.close()
        if n >= THRESHOLD:
            print(
                f"[memory-inject] {n} archived row(s) await triage. "
                "BULK-classify tool_use/tool_result as 'transient' without inspection — "
                "almost always re-derivable from disk. "
                "INSPECT EACH user_text and assistant_text before classifying: "
                "user_text often carries rules/requirements/preferences; "
                "assistant_text often carries discoveries reached after diagnostic work. "
                "Default-classifying these as transient loses the user's actual instructions. "
                "If >10 unclassified, do the triage BEFORE responding to the current task. "
                "`python ~/projects/memory-inject/memory.py list unclassified` then `classify <key> <cat>`."
            )
except Exception:
    pass
sys.exit(0)
