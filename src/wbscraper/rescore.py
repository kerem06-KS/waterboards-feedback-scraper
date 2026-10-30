"""Re-apply (edited) relevance rules to stored items without touching the network."""
from __future__ import annotations

import json

from .relevance import RelevanceScorer


def rescore_all(db, scorer: RelevanceScorer) -> dict[str, int]:
    db.clear_thread_relevance()
    rows = db.conn.execute("SELECT uid, thread_uid, kind, text FROM items").fetchall()
    own: dict[str, bool] = {}
    for r in rows:
        s = scorer.score(r["text"], thread_relevant=False)
        own[r["uid"]] = s.relevant
        if s.relevant and r["thread_uid"]:
            db.upsert_thread(r["thread_uid"], "rescore", relevant=True)
    changed = 0
    for r in rows:
        flag = r["kind"] != "post" and db.thread_relevant(r["thread_uid"])
        s = scorer.score(r["text"], thread_relevant=flag)
        before = db.item_row(r["uid"])["tier"]
        db.update_item_score(r["uid"], s.tier, int(own[r["uid"]]), s.total, int(s.entity_hit), json.dumps(s.reasons))
        changed += before != s.tier
    db.commit()
    return {"items": len(rows), "tier_changes": changed, **db.tier_counts()}
