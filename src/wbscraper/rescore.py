"""Re-apply (edited) relevance rules to stored items without touching the network.

Raw collected rows are never deleted and completed tasks are never reset: only the classification columns of
`items`, the relevance flag of `threads`, the list of auto-discovered subreddits and *pending* tasks that exist
solely because of earlier misclassifications are changed.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from .relevance import TIERS, RelevanceScorer, cap_tier
from .sources.waterboards_docs import classify_document
from .webclass import DEFAULT_REFERENCE, cap_for

# Evidence the live pipeline passes to the scorer next to the text (RawItem.score_hint). The hint itself is never
# stored, so it is rebuilt from the row where it can be (GitHub issues in the Water Boards' own organisation).
_GH_ORG_PREFIX = "CAWaterBoardDataCenter/"
_GH_HINT = "waterboards.ca.gov California Water Boards"


def score_hint(row) -> str:
    if row["source"] == "github" and (row["community"] or "").startswith(_GH_ORG_PREFIX):
        return f"{_GH_HINT} {row['community']}"
    return ""


def effective_kind(row) -> str:
    """Rows stored before web articles got their own kind: a web "post" that is not a forum/Discourse post is an article."""
    kind = row["kind"] or ""
    if row["source"] == "web" and kind == "post" and not (row["uid"] or "").startswith(("web:wp:", "web:disc:")):
        return "article"
    return kind


def letter_doc_caps(db) -> dict[str, str]:
    """Re-classify every stored comment-letter PDF from its own passages (the live crawler applies the same rules to
    new PDFs): an agency document (order, complaint, minutes ...) is not a public letter, an unclear one is review-only."""
    docs: dict[str, list[tuple[int, str, str]]] = {}
    for r in db.conn.execute("SELECT uid, url, text FROM items WHERE source='waterboards'"):
        parts = (r["uid"] or "").split(":")                 # wb:<doc hash>:<page>:<index>
        if len(parts) != 4:
            continue
        docs.setdefault(parts[1], []).append((int(parts[2]) * 1000 + int(parts[3]), r["url"].split("#")[0], r["text"]))
    caps = {}
    for h, passages in docs.items():
        passages.sort()
        kind = classify_document(passages[0][1], "\n\n".join(t for _, _, t in passages)[:60000])
        caps[h] = {"letter": "", "agency": "irrelevant"}.get(kind, "borderline")
    return caps


def _stricter(*caps: str) -> str:
    caps = [c for c in caps if c]
    return min(caps, key=TIERS.index) if caps else ""


def tier_cap(row, doc_caps: dict[str, str] | None = None, reference=DEFAULT_REFERENCE, official_extra=()) -> str:
    cap = "borderline" if score_hint(row) else ""      # project repositories: review only, never exported
    if row["source"] == "web":                          # agency / institutional pages and site checkers are not feedback
        cap = _stricter(cap, cap_for(urlparse(row["url"] or "").hostname, reference, official_extra))
    elif row["source"] == "waterboards" and doc_caps:
        parts = (row["uid"] or "").split(":")
        cap = _stricter(cap, doc_caps.get(parts[1], "") if len(parts) == 4 else "")
    return cap


def rescore_all(db, scorer: RelevanceScorer, *, prune: bool = True, retired_queries: list[str] | None = None,
                web_cfg: dict | None = None) -> dict:
    before = db.tier_counts()
    db.clear_thread_relevance()
    rows = db.conn.execute("SELECT uid, source, thread_uid, kind, community, url, text FROM items").fetchall()
    web_cfg = web_cfg or {}
    doc_caps = letter_doc_caps(db)
    reference = tuple(web_cfg.get("reference_domains") or DEFAULT_REFERENCE)
    official_extra = tuple(web_cfg.get("official_domains") or ())

    def cap_of(r):
        return tier_cap(r, doc_caps, reference, official_extra)

    # pass 1: what is relevant on its own merits? Only those items may make their thread relevant.
    prev = {r["uid"]: r["tier"] for r in db.conn.execute("SELECT uid, tier FROM items")}
    results = {}
    for r in rows:
        s = cap_tier(scorer.score(r["text"], thread_relevant=False, hint=score_hint(r), kind=effective_kind(r), allow_usage=r["source"] != "github"), cap_of(r))
        results[r["uid"]] = s
        if s.relevant and r["thread_uid"]:
            db.upsert_thread(r["thread_uid"], r["source"] or "rescore", community=r["community"], relevant=True)

    # pass 2: items in a relevant thread get the (narrow) thread exception; everything else keeps its own result
    own = {uid: s.relevant for uid, s in results.items()}
    for r in rows:
        if not own[r["uid"]] and db.thread_relevant(r["thread_uid"]):
            results[r["uid"]] = cap_tier(scorer.score(r["text"], thread_relevant=True, hint=score_hint(r), kind=effective_kind(r), allow_usage=r["source"] != "github"), cap_of(r))
    changed = 0
    for uid, s in results.items():
        db.update_item_score(uid, s.tier, int(own[uid]), s.total, int(s.entity_hit), json.dumps(s.reasons))
        changed += prev[uid] != s.tier
    db.commit()

    out = {"items": len(rows), "tier_changes": changed, "before": before, "after": db.tier_counts()}
    if prune:
        out["pruned"] = prune_stale_work(db, retired_queries or [])
    return out


def prune_stale_work(db, retired_queries: list[str], *, dry_run: bool = False) -> dict[str, int]:
    """Cancel *pending* tasks that only exist because of items that are no longer relevant, and forget
    auto-discovered subreddits that never produced a relevant item. Completed work is untouched."""
    conn = db.conn
    cancelled = {"discovered_subreddit_searches": 0, "thread_reads": 0, "post_lookups": 0, "retired_queries": 0}

    good_subs = {r["community"].lower() for r in conn.execute(
        "SELECT DISTINCT community FROM items WHERE own_relevant=1 AND community IS NOT NULL")}
    raw = db.get_meta("discovered_subs")
    discovered = json.loads(raw) if raw else []
    keep = [s for s in discovered if s.lower() in good_subs]
    dropped = {s.lower() for s in discovered} - {s.lower() for s in keep}
    if dropped and not dry_run:
        db.set_meta("discovered_subs", json.dumps(keep))

    pending = conn.execute("SELECT id, type, payload FROM tasks WHERE status='pending'").fetchall()
    relevant_threads = {r["thread_uid"] for r in conn.execute("SELECT thread_uid FROM threads WHERE relevant=1")}
    search_discovered = {r["thread_uid"] for r in conn.execute("SELECT thread_uid FROM threads WHERE discovered=1")}
    cancel: list[tuple[int, str]] = []
    for t in pending:
        p = json.loads(t["payload"])
        if t["type"] == "reddit_search":
            if (p.get("subreddit") or "").lower() in dropped:
                cancel.append((t["id"], "discovered_subreddit_searches"))
            elif p.get("q") in (retired_queries or []):
                cancel.append((t["id"], "retired_queries"))
        elif t["type"] == "reddit_thread":
            tuid = f"reddit:t3_{p.get('post_id')}"
            if tuid not in relevant_threads and tuid not in search_discovered:
                cancel.append((t["id"], "thread_reads"))
        elif t["type"] == "reddit_posts" and not p.get("discovered"):
            tuids = [f"reddit:t3_{i}" for i in p.get("ids", [])]
            if tuids and all(u not in relevant_threads and u not in search_discovered for u in tuids):
                cancel.append((t["id"], "post_lookups"))
    for tid, kind in cancel:
        if not dry_run:
            conn.execute("UPDATE tasks SET status='cancelled', last_error='cancelled by rescore: item no longer relevant',"
                         " updated_at=? WHERE id=? AND status='pending'", (db.clock(), tid))
        cancelled[kind] += 1
    if not dry_run:
        db.commit()
    cancelled["subreddits_forgotten"] = len(dropped)
    return cancelled


def queue_thread_followups(db, since_ts: int, until_ts: int | None = None) -> dict[str, int]:
    """Queue collection of the other comments in Reddit threads that are relevant now.

    A thread that only became relevant after a rescore never had its replies collected (or collected without the
    thread context, so they were dropped). Same task shape as the live collector, so threads already expanded are not
    repeated; already-expanded ones get the one-time "rescan" pass. Additive and idempotent.
    """
    from .sources.reddit_tasks import FAR_FUTURE, PRI_POSTS, PRI_THREAD

    before = until_ts if until_ts else FAR_FUTURE
    added = {"expansions": 0, "rescans": 0, "post_lookups": 0}
    for t in db.conn.execute("SELECT thread_uid, expanded FROM threads WHERE relevant=1 AND thread_uid LIKE 'reddit:t3_%'").fetchall():
        post_id = t["thread_uid"].split("t3_", 1)[1]
        if not db.item_row(t["thread_uid"]) and db.add_task("reddit_posts", {"ids": [post_id], "discovered": False}, PRI_POSTS, "reddit_posts"):
            added["post_lookups"] += 1
        payload = {"post_id": post_id, "after": since_ts, "before": before, "page": 0}
        if t["expanded"]:
            if db.add_task("reddit_thread", {**payload, "rescan": True}, PRI_THREAD, "reddit_thread"):
                added["rescans"] += 1
        elif db.add_task("reddit_thread", payload, PRI_THREAD, "reddit_thread"):
            added["expansions"] += 1
    db.commit()
    return added
