"""Maintenance for a queue built by older versions. Nothing here deletes data or touches finished tasks.

* `rebalance_split_priorities`: window-split children used to inherit their parent's priority, so an ever-growing tree
  of split windows always ran ahead of searches that had not run at all (the Arctic Shift comment searches never ran).
  Children are re-ranked root priority - decay x depth (breadth before depth). Deterministic, so safe to repeat.
"""
from __future__ import annotations

import json


def rebalance_split_priorities(db, decay: int = 10, *, dry_run: bool = False) -> dict:
    roots: dict[tuple, int] = {}
    pending = []
    for r in db.conn.execute("SELECT id, status, priority, payload FROM tasks WHERE type='reddit_search'"):
        p = json.loads(r["payload"])
        key = (p.get("kind"), p.get("q"), p.get("subreddit"))
        depth = int(p.get("depth", 0))
        if depth == 0:
            roots[key] = max(roots.get(key, -10**9), r["priority"])
        elif r["status"] == "pending":
            pending.append((r["id"], r["priority"], key, depth))
    changed = missing = 0
    by_depth: dict[int, int] = {}
    for tid, prio, key, depth in pending:
        if key not in roots:
            missing += 1
            continue
        new = roots[key] - decay * depth
        if new != prio:
            changed += 1
            by_depth[depth] = by_depth.get(depth, 0) + 1
            if not dry_run:
                db.conn.execute("UPDATE tasks SET priority=? WHERE id=? AND status='pending'", (new, tid))
    if not dry_run:
        db.commit()
    return {"pending_split_children": len(pending), "reprioritised": changed, "root_not_found": missing,
            "by_depth": dict(sorted(by_depth.items())), "dry_run": dry_run}
