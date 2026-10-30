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
]
EXTRA_NAMES = tuple(s.name for s in EXTRA_SOURCES)


def load(spec: SourceSpec):
    import importlib
    mod = importlib.import_module(spec.module)
    return mod, getattr(mod, spec.handler)
