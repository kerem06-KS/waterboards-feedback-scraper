"""Coverage report built from what actually happened in the database (never from assumptions)."""
from __future__ import annotations

import time

STALE_DAYS = 120


def _ts(v) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(v)) if v else "-"


def build_report(db) -> str:
    out: list[str] = ["# Coverage report (from this machine's collection database)\n"]
    tiers = db.tier_counts()
    out.append(f"Stored items: relevant **{tiers.get('relevant', 0)}**, borderline **{tiers.get('borderline', 0)}**, "
               f"other {tiers.get('irrelevant', 0)}.\n")

    out.append("## Providers and methods\n")
    out.append("| Provider | Method | Requests | OK | Failed | Restricted | Records seen | New stored | New relevant | Last error |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in db.conn.execute("SELECT * FROM stats ORDER BY provider, method"):
        out.append(f"| {r['provider']} | {r['method']} | {r['requests']} | {r['ok']} | {r['failures']} | {r['restricted']} | "
                   f"{r['items_seen']} | {r['items_new']} | {r['relevant_new']} | {(r['last_error'] or '')[:80]} |")
    out.append("")

    out.append("## Provider health\n")
    out.append("| Provider | Last success (UTC) | Last failure (UTC) | Consecutive failures | Newest record returned | Note |")
    out.append("|---|---|---|---:|---|---|")
    now = time.time()
    for r in db.conn.execute("SELECT * FROM provider_health ORDER BY provider"):
        note = ""
        if r["newest_item_ts"] and now - r["newest_item_ts"] > STALE_DAYS * 86400 and r["provider"] in ("pullpush", "arcticshift"):
            note = f"possibly stale archive (newest record > {STALE_DAYS} days old)"
        out.append(f"| {r['provider']} | {_ts(r['last_success'])} | {_ts(r['last_failure'])} | {r['consecutive_failures']} | "
                   f"{_ts(r['newest_item_ts'])} | {note} |")
    out.append("")

    out.append("## Unique relevant threads per method\n")
    out.append("| Provider | Method | Relevant items | Unique threads | Found only by this provider |")
    out.append("|---|---|---:|---:|---:|")
    q = """
    SELECT m.provider, m.method, COUNT(DISTINCT i.uid) AS items, COUNT(DISTINCT i.thread_uid) AS threads,
      COUNT(DISTINCT CASE WHEN (SELECT COUNT(DISTINCT m2.provider) FROM item_methods m2 WHERE m2.uid=i.uid)=1 THEN i.uid END) AS only_me
    FROM item_methods m JOIN items i ON i.uid=m.uid WHERE i.tier='relevant'
    GROUP BY m.provider, m.method ORDER BY m.provider, m.method"""
    for r in db.conn.execute(q):
        out.append(f"| {r['provider']} | {r['method']} | {r['items']} | {r['threads']} | {r['only_me']} |")
    out.append("")

    out.append("## Where relevant items were found\n")
    out.append("| Source | Community | Relevant items |")
    out.append("|---|---|---:|")
    for r in db.conn.execute("SELECT source, COALESCE(community,'-') AS c, COUNT(*) AS n FROM items WHERE tier='relevant' "
                             "GROUP BY source, c ORDER BY n DESC LIMIT 25"):
        out.append(f"| {r['source']} | {r['c']} | {r['n']} |")
    out.append("")

    out.append("## Work queue (unexplored / unfinished)\n")
    out.append("| Task type | Status | Count |")
    out.append("|---|---|---:|")
    for r in db.task_counts_by_type():
        out.append(f"| {r['type']} | {r['status']} | {r['n']} |")
    failed = db.conn.execute("SELECT type, last_error, COUNT(*) AS n FROM tasks WHERE status='failed' "
                             "GROUP BY type, last_error ORDER BY n DESC LIMIT 10").fetchall()
    if failed:
        out.append("\nFailed tasks (top reasons):\n")
        for r in failed:
            out.append(f"- {r['type']} x{r['n']}: {(r['last_error'] or '')[:120]}")
    out.append(f"\nURLs discovered by search engines for later page extraction: {db.urls_seen_count()}\n")
    out.append("See `docs/SOURCE_COVERAGE.md` for the full platform matrix (implemented, optional, restricted, future).")
    return "\n".join(out) + "\n"
