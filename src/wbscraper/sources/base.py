"""Shared models and exceptions for source adapters."""
from __future__ import annotations

from dataclasses import dataclass, field


class Unsupported(Exception):
    """The provider cannot perform this capability (e.g. no credentials or no such endpoint)."""


class AllProvidersFailed(Exception):
    """Every provider in a chain failed or is cooling down. `counted` is False when nothing was actually tried."""

    def __init__(self, errors: list[str], retry_at: float, counted: bool = True):
        super().__init__("; ".join(errors) or "no provider available")
        self.errors = errors
        self.retry_at = retry_at
        self.counted = counted


@dataclass
class RawItem:
    """A public post/comment as returned by a provider, before filtering. Never carries author data."""
    uid: str
    source: str
    kind: str                      # post | comment | letter | answer | ...
    url: str
    text: str
    date: str | None = None        # ISO 8601 UTC
    thread_uid: str | None = None
    parent_uid: str | None = None
    community: str | None = None
    title: str | None = None
    score: float | None = None
    score_hint: str = ""          # extra evidence used only for relevance scoring (e.g. a link post's URL); never stored
    tier_cap: str = ""            # highest tier this item may reach ("borderline": kept for review only)


@dataclass
class Page:
    items: list[RawItem] = field(default_factory=list)
    n_raw: int = 0                 # raw records returned (before dropping deleted/empty), used to detect the last page
    oldest_ts: int | None = None
    newest_ts: int | None = None
    post_ids: list[str] = field(default_factory=list)       # search-engine discovery: Reddit post ids
    urls: list[tuple[str, str]] = field(default_factory=list)  # (url, query) pairs for non-Reddit pages
    final: bool = False            # provider cannot continue past this page (no time-cursor paging)
