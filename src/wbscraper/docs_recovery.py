"""Recover Water Boards crawl work that older versions silently dropped.

Older versions treated "page budget reached" as a finished task: the page was marked done without being fetched, so its
links (comment-letter PDFs, sub-pages) were never discovered and a later resume could not retry it. The budget now
defers such pages instead. This adds a second-pass task for each page that was most probably skipped; completed tasks
are never reset or deleted and the command is idempotent (a second run adds nothing).
"""
from __future__ import annotations

import json


def requeue_skipped_pages(db, *, dry_run: bool = False) -> dict:
    fetched = db.counters().get("wb_pages_fetched", 0)
    done = db.conn.execute(
        "SELECT payload, priority FROM tasks WHERE type='wb_page' AND status='done' ORDER BY updated_at DESC, id DESC").fetchall()
    # every page fetched is one counter tick, so at most (done - fetched) done tasks never touched the network; the
    # skipped ones were completed last, once the budget was spent. Pages that were merely robots-blocked are re-checked
    # for free.
    candidates = max(0, len(done) - fetched)
    added = 0
    for row in done[:candidates]:
        payload = json.loads(row["payload"])
        if payload.get("pass"):
            continue
        if dry_run:
            added += 1
            continue
        if db.add_task("wb_page", {**payload, "pass": 2}, row["priority"], "waterboards_docs"):
            added += 1
    if not dry_run:
        db.commit()
    return {"pages_done": len(done), "pages_fetched": fetched, "possibly_skipped": candidates, "requeued": added,
            "dry_run": dry_run}
