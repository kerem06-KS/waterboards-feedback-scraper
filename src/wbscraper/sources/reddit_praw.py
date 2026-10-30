"""Optional official-API provider (PRAW). Active only with REDDIT_CLIENT_ID/SECRET and `pip install .[reddit]`.

Reddit now requires manual approval for new API apps, so this is a bonus path for users who already have access.
PRAW cannot page by time or search comments, so searches are single-shot (Page.final=True).
"""
from __future__ import annotations

import os
from typing import Any

from ..http import FetchError
from .base import Page, Unsupported
from .reddit_common import bare_id, comment_from_json, post_from_json
from .reddit_provider import RedditProvider, build_page


def _map_error(exc: Exception) -> FetchError:
    name = type(exc).__name__.lower()
    msg = f"praw: {type(exc).__name__}: {exc}"[:200]
    if "toomany" in name or "ratelimit" in name:
        return FetchError("rate_limited", msg, retry_after=60)
    if "forbidden" in name or "unauthorized" in name or "oauth" in name:
        return FetchError("forbidden", msg)
    if "notfound" in name:
        return FetchError("client", msg, status=404)
    if "timeout" in name:
        return FetchError("timeout", msg)
    return FetchError("network", msg)


def _submission_dict(s: Any) -> dict:
    return {"id": s.id, "title": s.title, "selftext": getattr(s, "selftext", ""), "created_utc": s.created_utc,
            "subreddit": str(s.subreddit), "score": getattr(s, "score", None)}


def _comment_dict(c: Any) -> dict:
    return {"id": c.id, "body": getattr(c, "body", ""), "created_utc": c.created_utc, "link_id": c.link_id,
            "parent_id": c.parent_id, "subreddit": str(c.subreddit), "score": getattr(c, "score", None)}


class PrawProvider(RedditProvider):
    name = "praw"
    domain = "oauth.reddit.com"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 user_agent: str | None = None, reddit: Any = None):
        self.client_id = client_id or os.environ.get("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET")
        self.user_agent = user_agent or os.environ.get("REDDIT_USER_AGENT") or "waterboards-feedback-scraper/0.1"
        self._reddit = reddit

    def available(self) -> bool:
        if self._reddit is not None:
            return True
        if not (self.client_id and self.client_secret):
            return False
        try:
            import praw  # noqa: F401
            return True
        except ImportError:
            return False

    def _r(self):
        if self._reddit is None:
            import praw
            self._reddit = praw.Reddit(client_id=self.client_id, client_secret=self.client_secret,
                                       user_agent=self.user_agent, check_for_async=False)
            self._reddit.read_only = True
        return self._reddit

    def _call(self, http, fn):
        http.acquire(self.domain)
        try:
            out = fn()
        except Exception as exc:
            err = _map_error(exc)
            http.record_failure(self.domain, err)
            raise err from exc
        http.record_success(self.domain)
        return out

    def ping(self, http) -> str:
        n = self._call(http, lambda: len(list(self._r().subreddit("all").search("CIWQS", limit=1))))
        return f"ok ({n} rows)"

    def search_posts(self, http, *, q, subreddit, after, before, limit) -> Page:
        def run():
            sub = self._r().subreddit(subreddit or "all")
            return [_submission_dict(s) for s in sub.search(q, sort="new", time_filter="all", limit=250)]

        rows = [r for r in self._call(http, run) if after <= int(float(r["created_utc"])) < before]
        page = build_page(rows, post_from_json)
        page.final = True
        return page

    def posts_by_ids(self, http, ids) -> Page:
        names = [f"t3_{bare_id(i) or i}" for i in ids]
        rows = self._call(http, lambda: [_submission_dict(s) for s in self._r().info(fullnames=names)])
        page = build_page(rows, post_from_json)
        page.final = True
        return page

    def thread_comments(self, http, post_id, *, after, before, limit) -> Page:
        def run():
            sub = self._r().submission(id=bare_id(post_id) or post_id)
            sub.comments.replace_more(limit=None)
            return [_comment_dict(c) for c in sub.comments.list()]

        page = build_page(self._call(http, run), comment_from_json)
        page.final = True
        return page
