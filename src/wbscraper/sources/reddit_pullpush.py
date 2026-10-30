"""PullPush (api.pullpush.io): free Pushshift-style archive. Global keyword search over posts and comments.

Notes from live probing (Oct 2026): `q` is fuzzy - quotes are ignored and multi-word queries return noise - so the
query planner only sends single distinctive tokens here. Volunteer-run, outages (502/429) happen: callers fall back.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .base import Page
from .reddit_common import bare_id, comment_from_json, post_from_json
from .reddit_provider import RedditProvider, build_page, data_list

BASE = "https://api.pullpush.io/reddit/search/"


class PullPushProvider(RedditProvider):
    name = "pullpush"
    domain = "api.pullpush.io"

    def _get(self, http, kind: str, params: dict) -> list[dict]:
        url = f"{BASE}{kind}/"
        return data_list(http.get_json(url, params=params, domain=self.domain), url)

    def ping(self, http) -> str:
        rows = self._get(http, "submission", {"q": "CIWQS", "size": 1, "sort": "desc"})
        return f"ok ({len(rows)} rows)"

    @staticmethod
    def _window(params: dict, after: int, before: int, limit: int, subreddit: str | None) -> dict:
        params.update({"size": limit, "sort": "desc", "sort_type": "created_utc", "after": int(after), "before": int(before)})
        if subreddit:
            params["subreddit"] = subreddit
        return params

    def search_posts(self, http, *, q, subreddit, after, before, limit) -> Page:
        rows = self._get(http, "submission", self._window({"q": q}, after, before, limit, subreddit))
        return build_page(rows, post_from_json)

    def search_comments(self, http, *, q, subreddit, after, before, limit) -> Page:
        rows = self._get(http, "comment", self._window({"q": q}, after, before, limit, subreddit))
        return build_page(rows, comment_from_json)

    def posts_by_ids(self, http, ids) -> Page:
        rows = self._get(http, "submission", {"ids": ",".join(bare_id(i) or i for i in ids)})
        return build_page(rows, post_from_json)

    def thread_comments(self, http, post_id, *, after, before, limit) -> Page:
        rows = self._get(http, "comment", self._window({"link_id": post_id}, after, before, limit, None))
        return build_page(rows, comment_from_json)

    def posts_by_url(self, http, prefix, *, after, before, limit) -> Page:
        host = urlparse(prefix).hostname or prefix
        rows = self._get(http, "submission", self._window({"q": host}, after, before, limit, None))

        def mentions(d: dict) -> bool:
            blob = f"{d.get('url') or ''} {d.get('selftext') or ''}".lower()
            return host.lower() in blob

        return build_page(rows, post_from_json, keep=mentions)
