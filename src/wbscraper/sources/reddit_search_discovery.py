"""Search-engine discovery of Reddit threads via the free `ddgs` metasearch library.

Reddit URLs found this way become post-id lookups (then thread expansion); non-Reddit URLs are recorded in
`urls_seen` for the page extractors added in later phases. Rate-limited hard (default 1 query / ~15 s).
"""
from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from ..http import FetchError
from .base import Page, Unsupported
from .reddit_common import parse_reddit_url
from .reddit_provider import RedditProvider


def _default_search(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS  # imported lazily so the rest of the tool works without it

    return list(DDGS().text(query, max_results=max_results))


class DiscoveryProvider(RedditProvider):
    name = "discovery"
    domain = "ddgs"

    def __init__(self, search_fn: Callable[[str, int], list[dict]] | None = None, max_results: int = 15):
        self._search = search_fn
        self.max_results = max_results

    def available(self) -> bool:
        if self._search is not None:
            return True
        try:
            import ddgs  # noqa: F401
            return True
        except ImportError:
            return False

    def ping(self, http) -> str:
        page = self.discover(http, 'site:reddit.com "CIWQS"')
        return f"ok ({len(page.post_ids)} reddit threads, {len(page.urls)} other urls)"

    def discover(self, http, query: str) -> Page:
        search = self._search or _default_search
        http.acquire(self.domain)
        try:
            results = search(query, self.max_results) or []
        except ImportError as exc:
            raise Unsupported(str(exc)) from exc
        except Exception as exc:  # ddgs raises its own exception types; classify by name/message
            name = f"{type(exc).__name__} {exc}".lower()
            kind = "rate_limited" if ("ratelimit" in name or "202" in name or "429" in name) else \
                   "timeout" if "timeout" in name else "network"
            err = FetchError(kind, f"ddgs: {type(exc).__name__}: {exc}"[:200])
            http.record_failure(self.domain, err)
            raise err from exc
        http.record_success(self.domain)

        page = Page(n_raw=len(results), final=True)
        seen: set[str] = set()
        for r in results:
            href = (r.get("href") or r.get("url") or "") if isinstance(r, dict) else ""
            if not href:
                continue
            ref = parse_reddit_url(href)
            if ref:
                if ref["post"] not in seen:
                    seen.add(ref["post"])
                    page.post_ids.append(ref["post"])
            else:
                page.urls.append((href, query))
        return page
