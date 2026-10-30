"""Arctic Shift (arctic-shift.photon-reddit.com): free Reddit archive, current to the present.

Observed behaviour: keyword search (`query`/`selftext`/`body`) requires `subreddit` (or author, never used here);
`url=` prefix search works across all of Reddit; `comments/search?link_id=` returns a whole thread, paged by time.
Heavy queries answer HTTP 422 "Timeout" - surfaced as FetchError(kind="query_timeout") so callers can split the window.
Only text fields are requested: author information never leaves the server.
"""
from __future__ import annotations

from ..http import FetchError
from .base import Page, Unsupported
from .reddit_common import bare_id, comment_from_json, post_from_json
from .reddit_provider import RedditProvider, build_page, data_list

BASE = "https://arctic-shift.photon-reddit.com/api/"
POST_FIELDS = "id,title,selftext,created_utc,subreddit,score,num_comments,url"
COMMENT_FIELDS = "id,body,created_utc,link_id,parent_id,subreddit,score"


def _is_timeout(text: str) -> bool:
    t = (text or "").lower()
    return "timeout" in t or "timed out" in t


class ArcticShiftProvider(RedditProvider):
    name = "arcticshift"
    domain = "arctic-shift.photon-reddit.com"

    def _get(self, http, path: str, params: dict) -> list[dict]:
        url = BASE + path
        try:
            payload = http.get_json(url, params=params, domain=self.domain)
            return data_list(payload, url)
        except FetchError as err:
            if err.kind == "client" and _is_timeout(err.body or str(err)):
                raise FetchError("query_timeout", "Arctic Shift query timed out", status=err.status, body=err.body) from err
            raise

    def ping(self, http) -> str:
        rows = self._get(http, "posts/ids", {"ids": "1c3x19v", "fields": "id,title"})
        return f"ok ({len(rows)} rows)"

    @staticmethod
    def _window(params: dict, after: int, before: int, limit: int) -> dict:
        params.update({"limit": limit, "sort": "desc", "after": int(after), "before": int(before)})
        return params

    def search_posts(self, http, *, q, subreddit, after, before, limit) -> Page:
        if not subreddit:
            raise Unsupported("Arctic Shift keyword search needs a subreddit")
        params = self._window({"query": q, "subreddit": subreddit, "fields": POST_FIELDS}, after, before, limit)
        return build_page(self._get(http, "posts/search", params), post_from_json)

    def search_comments(self, http, *, q, subreddit, after, before, limit) -> Page:
        if not subreddit:
            raise Unsupported("Arctic Shift keyword search needs a subreddit")
        params = self._window({"body": q, "subreddit": subreddit, "fields": COMMENT_FIELDS}, after, before, limit)
        return build_page(self._get(http, "comments/search", params), comment_from_json)

    def posts_by_ids(self, http, ids) -> Page:
        params = {"ids": ",".join(bare_id(i) or i for i in ids), "fields": POST_FIELDS}
        return build_page(self._get(http, "posts/ids", params), post_from_json)

    def thread_comments(self, http, post_id, *, after, before, limit) -> Page:
        params = self._window({"link_id": post_id, "fields": COMMENT_FIELDS}, after, before, limit)
        return build_page(self._get(http, "comments/search", params), comment_from_json)

    def posts_by_url(self, http, prefix, *, after, before, limit) -> Page:
        params = self._window({"url": prefix, "fields": POST_FIELDS}, after, before, limit)
        return build_page(self._get(http, "posts/search", params), post_from_json)
