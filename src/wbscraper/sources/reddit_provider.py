"""Interface shared by all Reddit providers, plus page-building helpers."""
from __future__ import annotations

from typing import Any, Callable

from ..http import FetchError, HttpClient
from .base import Page, RawItem, Unsupported
from .reddit_common import comment_from_json, created_ts, post_from_json


class RedditProvider:
    name = "base"
    domain = "default"

    def available(self) -> bool:
        return True

    def ping(self, http: HttpClient) -> str:
        raise Unsupported

    def search_posts(self, http, *, q: str, subreddit: str | None, after: int, before: int, limit: int) -> Page:
        raise Unsupported

    def search_comments(self, http, *, q: str, subreddit: str | None, after: int, before: int, limit: int) -> Page:
        raise Unsupported

    def posts_by_ids(self, http, ids: list[str]) -> Page:
        raise Unsupported

    def thread_comments(self, http, post_id: str, *, after: int, before: int, limit: int) -> Page:
        raise Unsupported

    def posts_by_url(self, http, prefix: str, *, after: int, before: int, limit: int) -> Page:
        raise Unsupported

    def discover(self, http, query: str) -> Page:
        raise Unsupported


def build_page(records: list[dict[str, Any]], convert: Callable[[dict], RawItem | None],
               keep: Callable[[dict], bool] | None = None) -> Page:
    items: list[RawItem] = []
    stamps: list[int] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        ts = created_ts(rec)
        if ts is not None:
            stamps.append(ts)
        if keep is not None and not keep(rec):
            continue
        item = convert(rec)
        if item is not None:
            items.append(item)
    return Page(items=items, n_raw=len(records), oldest_ts=min(stamps) if stamps else None,
                newest_ts=max(stamps) if stamps else None)


def data_list(payload: Any, url: str) -> list[dict]:
    """Extract payload['data'] as a list, raising bad_json for anything unexpected."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if data is None and payload.get("error"):
            raise FetchError("client", str(payload.get("error")), body=str(payload.get("error")))
        if isinstance(data, list):
            return data
    raise FetchError("bad_json", f"unexpected payload shape from {url}")
