"""Summary of the scored posts. Every figure is a share of the *collected* posts, not a measure of user satisfaction."""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

from .config import LABELS
from .tune import wilson

SMALL_N = 10


def source_name(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    if host.endswith("reddit.com") or host == "redd.it":
        return "Reddit"
    if host.endswith("github.com"):
        return "GitHub"
    if host.endswith("ycombinator.com"):
        return "Hacker News"
    if host.endswith("bsky.app"):
        return "Bluesky"
    if host.endswith("youtube.com") or host == "youtu.be":
        return "YouTube"
    if host.endswith(("stackoverflow.com", "stackexchange.com", "superuser.com", "serverfault.com", "askubuntu.com")):
        return "Stack Exchange"
    if host.endswith("waterboards.ca.gov"):
        return "Water Boards documents"
    return "Other websites"


def _share(k: int, n: int) -> str:
    if n == 0:
        return "—"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({100 * lo:.0f}–{100 * hi:.0f}%)"


def _table(header: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def duplicates(texts: list[str]) -> int:
    c = Counter(re.sub(r"\s+", " ", t).strip().lower() for t in texts)
    return sum(v for v in c.values() if v > 1)


def build_summary(items: list[dict], *, accuracy_note: str = "", reviewed: int = 0, changed: int = 0,
                  tagged: bool = False, date: str = "") -> str:
    """items: dicts with source, label, score, task, pain_point (pain point only meaningful for negatives), systems, text."""
    n = len(items)
    lines = ["# Sentiment summary" + (f" ({date})" if date else ""), "",
             "> **How to read this.** These are public posts that were *found by searching* for specific Water Boards systems. "
             "Every percentage below is a share of **the posts collected**. It is **not** a survey and says nothing about how "
             "satisfied Water Boards users are overall. Most posts are from Reddit, and most mention GeoTracker.",
             "> A post is positive or negative only when it gives an opinion of a Water Boards website or system itself. "
             "Environmental worries and neutral recommendations count as neutral.", ""]
    if n == 0:
        return "\n".join(lines + ["No posts to summarise."]) + "\n"
    cnt = Counter(i["label"] for i in items)
    lines += ["## Overall", "", *_table(["Label", "Posts", "Share of collected posts (95% interval)"],
                                        [[lab, cnt.get(lab, 0), _share(cnt.get(lab, 0), n)] for lab in ("negative", "neutral", "positive")]),
              "", f"Total posts: {n}. Mean score: {sum(i['score'] for i in items) / n:+.2f}."]
    d = duplicates([i["text"] for i in items])
    if d:
        lines.append(f"{d} of the posts have identical text to another post (the same message posted in more than one place), so shares are slightly inflated by repeats.")
    if reviewed:
        lines.append(f"Manual review: {reviewed} posts have an override or note; {changed} changed the automatic label.")

    by_src: dict[str, list[dict]] = {}
    for i in items:
        by_src.setdefault(i["source"], []).append(i)
    rows = []
    for src, grp in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        c = Counter(g["label"] for g in grp)
        flag = f" ⚠ n<{SMALL_N}, too few to compare" if len(grp) < SMALL_N else ""
        rows.append([src + flag, len(grp), c.get("negative", 0), _share(c.get("negative", 0), len(grp)),
                     c.get("neutral", 0), c.get("positive", 0)])
    lines += ["", "## By source", "", *_table(["Source", "Posts", "Negative", "Share negative (95% interval)", "Neutral", "Positive"], rows)]

    sys_counter: dict[str, Counter] = {}
    for i in items:
        for s in i.get("systems") or ["(no system named)"]:
            sys_counter.setdefault(s, Counter())[i["label"]] += 1
    rows = []
    for s, c in sorted(sys_counter.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values())
        rows.append([s + (f" ⚠ n<{SMALL_N}" if tot < SMALL_N else ""), tot, c.get("negative", 0), _share(c.get("negative", 0), tot),
                     c.get("positive", 0)])
    lines += ["", "## By system mentioned", "(a post naming two systems appears in both rows)", "",
              *_table(["System", "Posts", "Negative", "Share negative (95% interval)", "Positive"], rows)]

    if tagged:
        tc = Counter(i["task"] or "(not tagged)" for i in items)
        rows = []
        for t, k in tc.most_common():
            c = Counter(i["label"] for i in items if (i["task"] or "(not tagged)") == t)
            rows.append([t, k, _share(k, n), c.get("negative", 0), c.get("neutral", 0), c.get("positive", 0)])
        lines += ["", "## What people were trying to do (all posts)", "",
                  *_table(["Task", "Posts", "Share of collected posts", "Negative", "Neutral", "Positive"], rows)]
        neg = [i for i in items if i["label"] == "negative"]
        if neg:
            pc = Counter(i["pain_point"] or "(not tagged)" for i in neg)
            lines += ["", f"## Why it went badly (negative posts only, n={len(neg)})", "",
                      *_table(["Pain point", "Negative posts", "Share of negative posts"],
                              [[p, k, _share(k, len(neg))] for p, k in pc.most_common()])]
            tasks = sorted({i["task"] or "(not tagged)" for i in neg})
            pains = [p for p, _ in pc.most_common()]
            grid = [[t, *[sum(1 for i in neg if (i["task"] or "(not tagged)") == t and (i["pain_point"] or "(not tagged)") == p) or "" for p in pains]]
                    for t in tasks]
            lines += ["", "### Task × pain point (negative posts)", "", *_table(["Task", *pains], grid)]
            lines += ["", "### The negative posts", "",
                      *_table(["Date", "Source", "Score", "Task", "Pain point", "Link"],
                              [[i["date"][:10], i["source"], f"{i['score']:+.2f}", i["task"], i["pain_point"], i["source_url"]] for i in neg])]
    else:
        lines += ["", "_Task and pain-point tags were not produced in this run (`wbscraper analyze --tag`)._"]
    if accuracy_note:
        lines += ["", "## Method and measured accuracy", "", accuracy_note]
    return "\n".join(lines) + "\n"
