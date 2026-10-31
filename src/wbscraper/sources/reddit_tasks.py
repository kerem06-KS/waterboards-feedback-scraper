"""Reddit task handlers: provider fallback chains, time-cursor paging, window splitting, thread expansion.

Task types
  reddit_search    keyword search (global via PullPush, or subreddit-scoped via Arctic Shift), posts or comments
  reddit_urls      link posts pointing at Water Boards domains (Arctic Shift `url=` prefix search)
  reddit_discover  search-engine query (ddgs) that yields Reddit thread ids
  reddit_posts     look up posts by id (then expand their threads)
  reddit_thread    fetch every comment of one thread, paged by time

Every provider failure is contained: the chain moves on to the next provider; if all fail the task is deferred.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..db import Database
from ..http import FetchError, HttpClient
from ..pipeline import IngestResult, Pipeline
from ..stats import RunStats
from .base import AllProvidersFailed, Page, Unsupported
from .reddit_common import thread_uid
from .reddit_provider import RedditProvider

log = logging.getLogger("wbscraper.reddit")

FAR_FUTURE = 4102444800  # 2100-01-01: fixed "no upper bound" so task keys stay stable across runs

TASK_TYPES = {"reddit_search", "reddit_urls", "reddit_discover", "reddit_posts", "reddit_thread"}

CHAINS = {
    "search_global_posts": ["pullpush", "praw", "discovery"],
    "search_global_comments": ["pullpush"],
    "search_sub_posts": ["arcticshift", "pullpush", "praw"],
    "search_sub_comments": ["arcticshift", "pullpush"],
    "urls": ["arcticshift", "pullpush"],
    "posts": ["arcticshift", "pullpush", "praw"],
    "thread": ["arcticshift", "pullpush", "praw"],
    "discover": ["discovery"],
}

PRI_THREAD = 900
PRI_POSTS = 850
PRI_THREAD_DISCOVERED = 700


@dataclass
class PageOutcome:
    entity_hits: int = 0
    stored: int = 0
    new_relevant_thread: bool = False


class RedditHandler:
    TASK_TYPES = TASK_TYPES

    def __init__(self, db: Database, http: HttpClient, pipeline: Pipeline, settings, providers: dict[str, RedditProvider],
                 stats: RunStats, known_subreddits: set[str] | None = None, clock: Callable[[], float] = time.time):
        self.db, self.http, self.pipeline, self.settings, self.stats = db, http, pipeline, settings, stats
        self.providers = providers
        self.clock = clock
        cfg = settings.get("reddit", {}) or {}
        self.page_size = int(cfg.get("page_size", 100))
        self.min_window = int(cfg.get("min_window_days", 7)) * 86400
        self.max_depth = int(cfg.get("max_split_depth", 9))
        # Measured on a real run: Arctic Shift answers "query timed out" for ~2/3 of requests regardless of the window's
        # width or age (older and newer halves of a split fail equally often, siblings fail independently). Splitting
        # on the first timeout therefore multiplies the work without helping, so the same window is retried first.
        self.timeout_retries = int(cfg.get("timeout_retries", 2))
        self.split_decay = int(cfg.get("split_priority_decay", 10))      # deeper windows yield to untouched searches
        self.discovered_caps = {"posts": int((cfg.get("discovered_subreddit_anchors") or {}).get("posts", 40)),
                                "comments": int((cfg.get("discovered_subreddit_anchors") or {}).get("comments", 8))}
        self.max_dry = {"pullpush": 2, "arcticshift": 4, "praw": 1, **(cfg.get("max_dry_pages") or {})}
        self.thread_max_pages = int(cfg.get("thread_max_pages", 80))
        self.max_discovered_subs = int(cfg.get("max_discovered_subreddits", 60))
        self.lookup_batch = int(cfg.get("lookup_batch", 50))
        self.since = settings.since_ts
        self.until = settings.until_ts or FAR_FUTURE
        self.known_subs = {s.lower() for s in (known_subreddits or set())}
        raw = self.db.get_meta("discovered_subs")
        self.discovered_subs: list[str] = json.loads(raw) if raw else []
        self.known_subs.update(s.lower() for s in self.discovered_subs)
        self.anchor_provider: Callable[[str], list[str]] | None = None  # kind -> anchors, set by the planner

    # ------------------------------------------------------------------ chain helpers
    def _chain_for(self, task_type: str, p: dict) -> str:
        if task_type == "reddit_search":
            return f"search_{'sub' if p.get('subreddit') else 'global'}_{p['kind']}"
        return {"reddit_urls": "urls", "reddit_posts": "posts", "reddit_thread": "thread",
                "reddit_discover": "discover"}[task_type]

    def usable_providers(self, chain: str) -> list[RedditProvider]:
        return [self.providers[n] for n in CHAINS[chain] if n in self.providers and self.providers[n].available()]

    def next_domain(self, task_type: str, payload: dict) -> str | None:
        """Domain of the first provider in the chain that is usable and not cooling down (None if none)."""
        for prov in self.usable_providers(self._chain_for(task_type, payload)):
            if self.http.cooldown_remaining(prov.domain) <= 0:
                return prov.domain
        return None

    def earliest_ready(self, task_type: str, payload: dict) -> float:
        """Seconds until any provider in the chain exits cooldown (inf if no provider is usable)."""
        provs = self.usable_providers(self._chain_for(task_type, payload))
        return min((self.http.cooldown_remaining(p.domain) for p in provs), default=float("inf"))

    def _call_chain(self, chain: str, method: str, fn: Callable[[RedditProvider], Page], *,
                    split_ok: bool = False, skip: set[str] | None = None) -> tuple[str, Page]:
        errors: list[str] = []
        real_failure = False
        soonest = float("inf")
        for prov in self.usable_providers(chain):
            if skip and prov.name in skip:
                continue
            rem = self.http.cooldown_remaining(prov.domain)
            if rem > 0:
                errors.append(f"{prov.name}: cooling down {rem:.0f}s")
                soonest = min(soonest, rem)
                continue
            self.stats.current_source = prov.name
            try:
                page = fn(prov)
            except Unsupported:
                continue
            except FetchError as err:
                if err.kind == "query_timeout" and split_ok:
                    self.db.bump_stat(prov.name, method, requests=1, failures=1)
                    err.provider = prov.name  # type: ignore[attr-defined]
                    raise
                real_failure = True
                errors.append(f"{prov.name}: {err}")
                self.db.bump_stat(prov.name, method, requests=1, failures=1,
                                  restricted=1 if err.kind == "forbidden" else 0)
                self.db.set_stat_error(prov.name, method, str(err))
                if err.kind != "query_timeout":
                    self.db.health_failure(prov.name, str(err))
                if err.kind == "forbidden":
                    self.stats.restricted.add(prov.name)
                self.stats.last_error = f"{prov.name}: {err}"[:160]
                log.warning("%s %s failed: %s", prov.name, method, err)
                soonest = min(soonest, self.http.cooldown_remaining(prov.domain) or 300)
                continue
            self.db.bump_stat(prov.name, method, requests=1, ok=1, items_seen=page.n_raw)
            self.db.health_success(prov.name, page.newest_ts)
            self.stats.pages_fetched += 1
            return prov.name, page
        retry_in = max(60.0, 0.0 if soonest == float("inf") else soonest)
        raise AllProvidersFailed(errors or ["no provider available"], self.clock() + retry_in, counted=real_failure)

    # ------------------------------------------------------------------ ingestion + follow-ups
    def _ingest(self, page: Page, provider: str, method: str, *, priority: int) -> PageOutcome:
        out = PageOutcome()
        for item in page.items:
            res: IngestResult = self.pipeline.ingest(item, provider, method)
            self.db.bump_stat(provider, method, items_new=1 if res.status == "stored" else 0,
                              relevant_new=1 if res.status == "stored" and res.tier == "relevant" else 0)
            if res.entity_hit or res.tier != "irrelevant":
                out.entity_hits += 1
            if res.status != "stored":
                continue
            out.stored += 1
            if res.new_relevant_thread:
                out.new_relevant_thread = True
                row = self.db.thread_row(item.thread_uid) if item.thread_uid else None
                self._queue_thread(item.thread_uid, priority=PRI_THREAD, need_post=(item.kind != "post"),
                                   rescan=bool(row and row["expanded"]))
            if res.own_relevant and item.community:
                self._maybe_discover_subreddit(item.community)
        self.db.commit()
        return out

    def _queue_thread(self, tuid: str | None, *, priority: int, need_post: bool = False, discovered: bool = False,
                      rescan: bool = False) -> None:
        if not tuid or not tuid.startswith("reddit:t3_"):
            return
        post_id = tuid.split("t3_", 1)[1]
        row = self.db.thread_row(tuid)
        if row is not None and row["expanded"] and not rescan:
            return
        if need_post and not self.db.item_row(tuid):
            self.db.add_task("reddit_posts", {"ids": [post_id], "discovered": False}, PRI_POSTS, "reddit_posts")
        payload = {"post_id": post_id, "after": self.since, "before": self.until, "page": 0}
        if rescan:
            payload["rescan"] = True
        self.db.add_task("reddit_thread", payload, priority, "reddit_thread")

    def _maybe_discover_subreddit(self, community: str) -> None:
        key = community.lower()
        if key in self.known_subs or len(self.discovered_subs) >= self.max_discovered_subs:
            return
        self.known_subs.add(key)
        self.discovered_subs.append(community)
        self.db.set_meta("discovered_subs", json.dumps(self.discovered_subs))
        if self.anchor_provider is None:
            return
        for kind in ("posts", "comments"):
            for anchor in self.anchor_provider(kind)[: self.discovered_caps[kind]]:
                self.db.add_task("reddit_search", {
                    "kind": kind, "q": anchor, "subreddit": community, "after": self.since, "before": self.until,
                    "dry": 0, "depth": 0}, 520 - (1 if kind == "comments" else 0), "reddit_discovered_sub")

    # ------------------------------------------------------------------ paging
    def _continue(self, task_type: str, p: dict, page: Page, provider: str, outcome: PageOutcome, priority: int) -> None:
        if page.final or page.n_raw < self.page_size or page.oldest_ts is None:
            return
        dry = 0 if outcome.entity_hits else int(p.get("dry", 0)) + 1
        if dry >= self.max_dry.get(provider, 3):
            log.info("%s %s: stopping after %d dry pages", task_type, p.get("q") or p.get("prefix"), dry)
            return
        nb = page.oldest_ts + 1                     # re-include the boundary second; duplicates are dropped by uid
        if nb >= int(p["before"]):
            # a whole page sits inside one second: step past it so paging always terminates
            log.warning("more than %d items share one second (%s); some may be skipped", self.page_size, page.oldest_ts)
            nb = page.oldest_ts
        if nb >= int(p["before"]) or nb <= int(p["after"]):
            return
        self.db.add_task(task_type, {**self._fresh(p), "before": nb, "dry": dry}, priority, self._family(task_type, p))

    @staticmethod
    def _family(task_type: str, p: dict) -> str:
        return task_type

    @staticmethod
    def _fresh(p: dict) -> dict:
        """A payload for a follow-up task: the timeout-retry counter belongs to one window only."""
        return {k: v for k, v in p.items() if k != "timeouts"}

    def _on_timeout(self, task_type: str, p: dict, priority: int, split_payload: dict | None = None) -> bool:
        """A provider answered "query timed out": retry the same window, and only split it once it keeps timing out.
        Returns True if follow-up work was queued."""
        tries = int(p.get("timeouts", 0))
        if tries < self.timeout_retries:
            self.db.add_task(task_type, {**p, "timeouts": tries + 1}, priority, self._family(task_type, p))
            log.info("timeout for %s: retrying the same window (%d/%d)", p.get("q") or p.get("prefix") or p.get("post_id"),
                     tries + 1, self.timeout_retries)
            return True
        return self._split(task_type, split_payload or p, priority)

    def _split(self, task_type: str, p: dict, priority: int) -> bool:
        """Halve a time window that timed out repeatedly. The upper bound is clamped to 'now' so the empty future
        never keeps a half-window artificially wide. Children run at a slightly lower priority per level so that
        deepening one search never starves searches that have not run at all (breadth before depth)."""
        after = int(p["after"])
        before = min(int(p["before"]), int(self.clock()) + 86400)
        depth = int(p.get("depth", 0))
        if depth >= self.max_depth or (before - after) <= self.min_window:
            return False
        mid = (after + before) // 2
        base = self._fresh(p)
        for lo, hi in ((after, mid + 1), (mid - 1, before)):
            self.db.add_task(task_type, {**base, "after": lo, "before": hi, "depth": depth + 1, "dry": 0},
                             priority - self.split_decay, self._family(task_type, p))
        log.info("split window for %s into halves (depth %d)", p.get("q") or p.get("prefix"), depth + 1)
        return True

    # ------------------------------------------------------------------ task entry point
    def run(self, task_type: str, p: dict, priority: int) -> None:
        handler = getattr(self, f"_run_{task_type}")
        handler(p, priority)

    def _run_reddit_search(self, p: dict, priority: int) -> None:
        kind, q, sub = p["kind"], p["q"], p.get("subreddit")
        chain, method = self._chain_for("reddit_search", p), f"search_{kind}"

        def fn(prov: RedditProvider) -> Page:
            if prov.name == "discovery":
                return prov.discover(self.http, f"site:reddit.com {q}")
            args = dict(q=q, subreddit=sub, after=int(p["after"]), before=int(p["before"]), limit=self.page_size)
            return prov.search_posts(self.http, **args) if kind == "posts" else prov.search_comments(self.http, **args)

        self.stats.current_task = f"{kind} '{q}'" + (f" in r/{sub}" if sub else "")
        try:
            provider, page = self._call_chain(chain, method, fn, split_ok=True)
        except FetchError as err:
            if err.kind != "query_timeout":
                raise
            if self._on_timeout("reddit_search", p, priority):
                return
            provider, page = self._call_chain(chain, method, fn, skip={getattr(err, "provider", "")})
        self._after_page("reddit_search", p, page, provider, method, priority)

    def _run_reddit_urls(self, p: dict, priority: int) -> None:
        def fn(prov: RedditProvider) -> Page:
            return prov.posts_by_url(self.http, p["prefix"], after=int(p["after"]), before=int(p["before"]),
                                     limit=self.page_size)

        self.stats.current_task = f"links to {p['prefix']}"
        try:
            provider, page = self._call_chain("urls", "posts_by_url", fn, split_ok=True)
        except FetchError as err:
            if err.kind != "query_timeout":
                raise
            if self._on_timeout("reddit_urls", p, priority):
                return
            provider, page = self._call_chain("urls", "posts_by_url", fn, skip={getattr(err, "provider", "")})
        self._after_page("reddit_urls", p, page, provider, "posts_by_url", priority)

    def _after_page(self, task_type: str, p: dict, page: Page, provider: str, method: str, priority: int) -> None:
        if page.post_ids or page.urls:
            self._handle_discovery(page, p.get("q", ""), priority)
        outcome = self._ingest(page, provider, method, priority=priority)
        self._continue(task_type, p, page, provider, outcome, priority)

    def _run_reddit_discover(self, p: dict, priority: int) -> None:
        self.stats.current_task = f"web: {p['q'][:60]}"
        _, page = self._call_chain("discover", "discover", lambda prov: prov.discover(self.http, p["q"]))
        self._handle_discovery(page, p["q"], priority)

    def _handle_discovery(self, page: Page, query: str, priority: int) -> None:
        new_urls = 0
        from .web_pages import queue_discovered_url
        for url, q in page.urls:
            if queue_discovered_url(self.db, self.settings, url, q):
                new_urls += 1
            else:
                self.db.add_url_seen(url, url.split("/")[2] if "//" in url else url, q)
        self.stats.urls_discovered += new_urls + len(page.post_ids)
        ids = []
        for pid in page.post_ids:
            tuid = thread_uid(pid)
            self.db.upsert_thread(tuid, "reddit", discovered=True)
            row = self.db.thread_row(tuid)
            if row and row["expanded"]:
                continue
            ids.append(pid)
        for i in range(0, len(ids), self.lookup_batch):
            self.db.add_task("reddit_posts", {"ids": sorted(ids[i:i + self.lookup_batch]), "discovered": True},
                             PRI_POSTS - 50, "reddit_posts")
        self.db.commit()

    def _run_reddit_posts(self, p: dict, priority: int) -> None:
        ids = list(p["ids"])
        self.stats.current_task = f"lookup {len(ids)} post(s)"
        provider, page = self._call_chain("posts", "posts_by_ids", lambda prov: prov.posts_by_ids(self.http, ids))
        self._ingest(page, provider, "posts_by_ids", priority=priority)
        if p.get("discovered"):
            for pid in ids:  # search engines surfaced these threads for our queries: read their comments too
                self._queue_thread(thread_uid(pid), priority=PRI_THREAD_DISCOVERED, discovered=True)
        self.db.commit()

    def _run_reddit_thread(self, p: dict, priority: int) -> None:
        post_id, page_no = p["post_id"], int(p.get("page", 0))
        tuid = thread_uid(post_id)
        self.stats.current_task = f"thread {post_id} p{page_no + 1}"

        def fn(prov: RedditProvider) -> Page:
            return prov.thread_comments(self.http, post_id, after=int(p["after"]), before=int(p["before"]),
                                        limit=self.page_size)

        try:
            provider, page = self._call_chain("thread", "thread_comments", fn, split_ok=True)
        except FetchError as err:
            if err.kind != "query_timeout":
                raise
            if self._on_timeout("reddit_thread", p, priority, split_payload={**p, "page": 0}):
                return                                   # a retry or the halves expand the thread; each marks it expanded
            provider, page = self._call_chain("thread", "thread_comments", fn, skip={getattr(err, "provider", "")})
        outcome = self._ingest(page, provider, "thread_comments", priority=priority)
        more = (not page.final and page.n_raw >= self.page_size and page.oldest_ts is not None
                and page_no + 1 < self.thread_max_pages)
        if more:
            nb = page.oldest_ts + 1 if page.oldest_ts + 1 < int(p["before"]) else page.oldest_ts
            if int(p["after"]) < nb < int(p["before"]):
                self.db.add_task("reddit_thread", {**self._fresh(p), "before": nb, "page": page_no + 1}, priority, "reddit_thread")
            else:
                more = False
        if not more:
            self.db.mark_thread_expanded(tuid)
        # a comment made this thread relevant mid-way: earlier comments were scored without context -> rescan once
        if outcome.new_relevant_thread and not p.get("rescan"):
            self.db.add_task("reddit_thread", {"post_id": post_id, "after": self.since, "before": self.until,
                                               "page": 0, "rescan": True}, PRI_THREAD, "reddit_thread")
        self.db.commit()
