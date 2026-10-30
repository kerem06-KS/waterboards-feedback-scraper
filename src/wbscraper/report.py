"""Coverage report built from what actually happened in the database (never from assumptions)."""
from __future__ import annotations

import time

STALE_DAYS = 120

COUNTER_LABELS = {
    "wb_pages_fetched": "Water Boards pages fetched",
    "wb_pdfs_queued": "Water Boards PDFs discovered",
    "wb_letters": "PDFs classified as individual public comment letters (processed)",
    "wb_agency_docs": "PDFs classified as agency documents / comment summaries (skipped: not independent posts)",
    "wb_unclassified_docs": "PDFs that could not be classified (skipped)",
    "wb_scanned_pdfs": "Scanned PDFs without a text layer (coverage gap: OCR not implemented)",
    "wb_pdf_unreadable": "Unreadable/corrupt PDFs",
    "wb_pdf_too_large": "PDFs over the size cap",
    "wb_robots_or_host_blocked": "Water Boards URLs skipped (robots.txt or outside allowed hosts)",
    "web_robots_blocked": "Web pages skipped because robots.txt disallows them",
    "web_not_html": "Web pages skipped (not HTML)",
    "web_not_extracted": "Web pages fetched but no discussion text could be extracted",
}


def _ts(v) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(v)) if v else "-"


def build_report(db) -> str:
    out: list[str] = ["# Coverage report (from this machine's collection database)\n"]
    tiers = db.tier_counts()
    out.append(f"Stored items: relevant **{tiers.get('relevant', 0)}**, borderline **{tiers.get('borderline', 0)}**, "
               f"other {tiers.get('irrelevant', 0)}.\n")

    out.append("## Funnel: where the candidates go\n")
    out.append("Most stored items are thread context or pages that only mention the topic. Stored, mention and system are "
               "nested; borderline and relevant are the outcome. The drop-off shows where a source's volume turns into "
               "(or fails to become) usable feedback.\n")
    out.append("| Source | Stored | Mention the Water Boards | Name a specific system | Borderline | Relevant | Capped (agency page, "
               "site checker, project repo, agency document) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in db.conn.execute(
            "SELECT source, COUNT(*) n, SUM(entity_hit) hit, "
            "SUM(CASE WHEN reasons LIKE '%\"stage1:system\"%' THEN 1 ELSE 0 END) sysn, "
            "SUM(tier='borderline') bl, SUM(tier='relevant') rel, "
            "SUM(CASE WHEN reasons LIKE '%capped:%' THEN 1 ELSE 0 END) capped "
            "FROM items GROUP BY source ORDER BY rel DESC, n DESC"):
        out.append(f"| {r['source']} | {r['n']} | {r['hit']} | {r['sysn']} | {r['bl']} | {r['rel']} | {r['capped']} |")
    out.append("")

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
    out.append(f"\nURLs discovered by search engines: {db.urls_seen_count()}\n")
    counters = db.counters()
    shown = [(COUNTER_LABELS[k], v) for k, v in counters.items() if k in COUNTER_LABELS and v]
    if shown:
        out.append("## Documents and pages not turned into posts (nothing is discarded silently)\n")
        out.extend(f"- {label}: {v}" for label, v in shown)
        out.append("")
    out.append("See `docs/SOURCE_COVERAGE.md` for the full platform matrix (implemented, optional, restricted, future).")
    return "\n".join(out) + "\n"
