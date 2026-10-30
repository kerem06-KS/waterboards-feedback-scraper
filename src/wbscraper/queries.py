"""Builds search queries from the editable term library and seeds the task queue.

PullPush gets single distinctive tokens (its `q` is fuzzy), Arctic Shift gets anchor phrases per subreddit,
search engines get quoted anchors, modifier combinations and natural-language questions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .db import Database
from .sources.reddit_tasks import FAR_FUTURE
from .terms import TermLibrary


@dataclass(frozen=True)
class Anchor:
    text: str
    tier: int
    entity: str


def anchors(lib: TermLibrary, max_tier: int = 3) -> list[Anchor]:
    """Unique anchor strings (case-insensitive), keeping the best tier each was listed under.

    Ordered by tier, then by entity order in entities.yaml (most important first), then list order.
    """
    best: dict[str, tuple[tuple[int, int, int], Anchor]] = {}
    for ei, e in enumerate(lib.entities):
        for tier, items in sorted(e.anchors.items()):
            if tier > max_tier:
                continue
            for pos, text in enumerate(items):
                key = str(text).lower()
                rank = (tier, ei, pos)
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, Anchor(str(text), tier, e.id))
    return [a for _, a in sorted(best.values(), key=lambda x: x[0])]


def _tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", text.lower()))


def _is_domain(text: str) -> bool:
    return "." in text and " " not in text


def compact(lib: TermLibrary, items: list[Anchor]) -> list[Anchor]:
    """Drop anchors made redundant by a broader one (searching 'GeoTracker' already returns 'GeoTracker upload').

    Ambiguous acronyms (case-sensitive weak terms such as SMARTS) never subsume anything, because a bare
    stemmed search for them is dominated by unrelated matches.
    """
    ambiguous = {w.term.lower() for e in lib.entities for w in e.weak if w.case_sensitive}
    broad = [(a, _tokens(a.text)) for a in items if not _is_domain(a.text) and a.text.lower() not in ambiguous]
    kept: list[Anchor] = []
    for b in items:
        if _is_domain(b.text):
            kept.append(b)
            continue
        tb = _tokens(b.text)
        redundant = any(a is not b and ta < tb and a.tier <= b.tier for a, ta in broad)
        if not redundant:
            kept.append(b)
    return kept


def pullpush_tokens(lib: TermLibrary) -> list[str]:
    seen: dict[str, str] = {}
    for e in lib.entities:
        for t in e.pullpush:
            seen.setdefault(str(t).lower(), str(t))
    return list(seen.values())


def _primary(e) -> str | None:
    tier1 = e.anchors.get(1) or e.anchors.get(2) or []
    return str(tier1[0]) if tier1 else None


def web_queries(lib: TermLibrary, prefix: str = "") -> list[tuple[int, str]]:
    """(tier, query) pairs for search engines. `prefix` e.g. 'site:reddit.com '."""
    out: dict[str, int] = {}

    def add(tier: int, q: str) -> None:
        out.setdefault(prefix + q, tier)

    for a in anchors(lib, max_tier=1):
        add(1, f'"{a.text}"')
    for e in lib.entities:
        for q in e.web_extra:
            add(1, q)
    mods = lib.modifiers.get("query_modifiers", [])[:8]
    for e in lib.entities:
        if e.kind != "system":
            continue
        primary = _primary(e)
        if not primary:
            continue
        for m in mods:
            add(2, f'"{primary}" {m}')
    qs = lib.questions
    for e in lib.entities:
        if e.kind not in ("system", "org"):
            continue
        for tpl in qs.get("system_templates", []):
            add(3, tpl.format(system=e.short))
        for task in e.tasks[:2]:
            for tpl in qs.get("task_templates", []):
                add(3, tpl.format(system=e.short, task=task))
    for g in qs.get("generic", []):
        add(3, g)
    return sorted(((t, q) for q, t in out.items()), key=lambda x: (x[0], x[1]))


def subreddit_tiers(lib: TermLibrary) -> list[tuple[int, str]]:
    subs = lib.subreddits or {}
    out, seen = [], set()
    for tier, key in ((1, "tier1"), (2, "tier2"), (3, "tier3")):
        for s in subs.get(key, []) or []:
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append((tier, s))
    return out


def known_subreddits(lib: TermLibrary) -> set[str]:
    return {s.lower() for _, s in subreddit_tiers(lib)}


def discovered_sub_anchors(lib: TermLibrary):
    """Anchor list used for subreddits found at runtime (tier-1 and tier-2 anchors)."""
    texts = [a.text for a in anchors(lib, max_tier=2)]
    return lambda _tier: texts


def plan_reddit_tasks(lib: TermLibrary, settings, *, since: int | None = None, enabled: dict[str, bool] | None = None
                      ) -> list[tuple[str, dict, int, str]]:
    """All Reddit tasks for this configuration. Deterministic payloads make seeding idempotent."""
    since = int(since if since is not None else settings.since_ts)
    until = int(settings.until_ts or FAR_FUTURE)
    enabled = enabled or {}
    tasks: list[tuple[str, dict, int, str]] = []
    base = {"after": since, "before": until, "dry": 0, "depth": 0}

    # 1) link posts to Water Boards domains
    if enabled.get("reddit_arcticshift", True) or enabled.get("reddit_pullpush", True):
        for prefix in settings.get("reddit.url_prefixes", []) or []:
            tasks.append(("reddit_urls", {**base, "prefix": prefix}, 650, "reddit_urls"))

    # 2) global keyword search (PullPush): distinctive tokens only
    if enabled.get("reddit_pullpush", True):
        for tok in pullpush_tokens(lib):
            tasks.append(("reddit_search", {**base, "kind": "posts", "q": tok, "subreddit": None}, 630, "reddit_pullpush"))
            tasks.append(("reddit_search", {**base, "kind": "comments", "q": tok, "subreddit": None}, 625, "reddit_pullpush"))

    # 3) subreddit-scoped search (Arctic Shift), curated subreddits x anchors
    if enabled.get("reddit_arcticshift", True):
        max_anchor_tier = {1: 3, 2: 2, 3: 1}
        for sub_tier, sub in subreddit_tiers(lib):
            cap = int(settings.get(f"reddit.anchors_per_subreddit.{sub_tier}", 40))
            for a in compact(lib, anchors(lib, max_tier=max_anchor_tier[sub_tier]))[:cap]:
                pri = 600 - 40 * (sub_tier - 1) - 20 * (a.tier - 1)
                for kind, bump in (("posts", 0), ("comments", -1)):
                    tasks.append(("reddit_search", {**base, "kind": kind, "q": a.text, "subreddit": sub}, pri + bump,
                                  "reddit_arcticshift"))

    # 4) search-engine discovery of Reddit threads
    if enabled.get("reddit_discovery", True):
        for tier, q in web_queries(lib, prefix="site:reddit.com "):
            tasks.append(("reddit_discover", {"q": q}, 580 - 40 * (tier - 1), "reddit_discovery"))
    return tasks


def plan_all(lib: TermLibrary, settings, *, since: int | None = None) -> list[tuple[str, dict, int, str]]:
    """Reddit tasks plus the tasks of every enabled extra source (each source module provides plan())."""
    from .sources.registry import EXTRA_SOURCES, load
    enabled = {n: settings.source_enabled(n) for n in
               ("reddit_pullpush", "reddit_arcticshift", "reddit_discovery", "reddit_praw")}
    tasks = plan_reddit_tasks(lib, settings, since=since, enabled=enabled)
    for spec in EXTRA_SOURCES:
        if settings.source_enabled(spec.name):
            mod, _ = load(spec)
            tasks.extend(mod.plan(lib, settings, since))
    return tasks


def seed(db: Database, lib: TermLibrary, settings, *, since: int | None = None) -> int:
    return db.add_tasks(plan_all(lib, settings, since=since))


def plan_stats(lib: TermLibrary, settings) -> dict[str, int]:
    tasks = plan_all(lib, settings)
    out: dict[str, int] = {}
    for t, p, _pri, fam in tasks:
        out[fam] = out.get(fam, 0) + 1
    out["total"] = len(tasks)
    return out
