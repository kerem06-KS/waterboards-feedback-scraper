"""Registry of source adapters beyond the Reddit providers: planners (seed tasks) and handler factories."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class SourceSpec:
    name: str                       # settings key under `sources:` and the --sources value
    role: str
    credentials: str
    domains: str
    module: str                     # module with plan() and a Handler class
    handler: str                    # class name
    required_env: str = ""          # source is inactive until this environment variable is set


EXTRA_SOURCES: list[SourceSpec] = [
    SourceSpec("stackexchange", "Stack Exchange network: questions, answers, comments",
               "STACKEXCHANGE_KEY (optional, raises quota)", "api.stackexchange.com",
               "wbscraper.sources.stackexchange", "StackExchangeHandler"),
    SourceSpec("hackernews", "Hacker News stories and comments (Algolia API)", "none", "hn.algolia.com",
               "wbscraper.sources.hackernews", "HackerNewsHandler"),
    SourceSpec("github", "GitHub issues and comments (Water Boards data-team repos + global search)",
               "GITHUB_TOKEN (optional, raises rate limits)", "api.github.com",
               "wbscraper.sources.github", "GitHubHandler"),
    SourceSpec("waterboards_docs", "Public comment letters/documents on waterboards.ca.gov (PDF + HTML)", "none",
               "waterboards.ca.gov", "wbscraper.sources.waterboards_docs", "WaterBoardsDocsHandler"),
    SourceSpec("web_pages", "Forum / blog / Q&A pages found via search engines", "none", "(many; robots.txt obeyed)",
               "wbscraper.sources.web_pages", "WebPagesHandler"),
    SourceSpec("bluesky", "Bluesky posts (AT Protocol search, date-window paging) and reply threads",
               "none (BSKY_HANDLE/BSKY_APP_PASSWORD optional fallback)", "api.bsky.app",
               "wbscraper.sources.bluesky", "BlueskyHandler"),
    SourceSpec("lemmy", "Lemmy posts and comments (public API on configured instances)", "none",
               "lemmy.world, lemmy.ml, …", "wbscraper.sources.lemmy", "LemmyHandler"),
    SourceSpec("mastodon", "Mastodon hashtag timelines; full-text search with a token",
               "none (MASTODON_INSTANCE + MASTODON_ACCESS_TOKEN optional)", "mastodon.social, fosstodon.org, …",
               "wbscraper.sources.mastodon", "MastodonHandler"),
    SourceSpec("youtube", "YouTube video descriptions and comments (Data API v3)", "YOUTUBE_API_KEY (required, free)",
               "www.googleapis.com", "wbscraper.sources.youtube", "YouTubeHandler", required_env="YOUTUBE_API_KEY"),
]
EXTRA_NAMES = tuple(s.name for s in EXTRA_SOURCES)


def load(spec: SourceSpec):
    import importlib
    mod = importlib.import_module(spec.module)
    return mod, getattr(mod, spec.handler)
