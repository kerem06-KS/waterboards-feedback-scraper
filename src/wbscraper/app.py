"""Wires settings, database, HTTP, providers and handlers together (used by the CLI and by tests)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import queries
from .config import Settings
from .db import Database
from .http import HttpClient
from .pipeline import Pipeline
from .redact import redactor_from_settings
from .relevance import RelevanceScorer
from .sources.reddit_arcticshift import ArcticShiftProvider
from .sources.reddit_praw import PrawProvider
from .sources.reddit_pullpush import PullPushProvider
from .sources.reddit_search_discovery import DiscoveryProvider
from .sources.reddit_tasks import RedditHandler
from .stats import RunStats
from .terms import TermLibrary

SOURCE_NAMES = ("reddit_pullpush", "reddit_arcticshift", "reddit_discovery", "reddit_praw")


@dataclass
class App:
    settings: Settings
    lib: TermLibrary
    db: Database
    http: HttpClient
    stats: RunStats
    scorer: RelevanceScorer
    pipeline: Pipeline
    providers: dict
    handlers: list = field(default_factory=list)

    def close(self) -> None:
        self.http.close()
        self.db.close()

    def term_files(self) -> list[Path]:
        d = self.settings.config_dir
        return [d / "settings.yaml", d / "relevance.yaml", *sorted((d / "terms").glob("*.yaml")),
                *sorted((d / "sources").glob("*.yaml"))]


def build_providers(settings: Settings, search_fn=None) -> dict:
    providers = {}
    if settings.source_enabled("reddit_pullpush"):
        providers["pullpush"] = PullPushProvider()
    if settings.source_enabled("reddit_arcticshift"):
        providers["arcticshift"] = ArcticShiftProvider()
    if settings.source_enabled("reddit_discovery"):
        providers["discovery"] = DiscoveryProvider(search_fn=search_fn,
                                                   max_results=int(settings.get("reddit.discovery_results", 15)))
    if settings.source_enabled("reddit_praw"):
        providers["praw"] = PrawProvider()
    return providers


def build_app(settings: Settings, *, db: Database | None = None, http: HttpClient | None = None, search_fn=None,
              clock=time.time, sleep=time.sleep, lib: TermLibrary | None = None) -> App:
    lib = lib or TermLibrary.load(settings.config_dir)
    db = db or Database(settings.db_path, clock=clock)
    http = http or HttpClient(settings, clock=clock, sleep=sleep)
    stats = RunStats(csv_path=str(settings.csv_path))
    http.on_request = lambda domain, ok: setattr(stats, "failed_requests", stats.failed_requests + (0 if ok else 1))
    scorer = RelevanceScorer(lib)
    pipeline = Pipeline(db, scorer, redactor_from_settings(settings), stats,
                        min_chars=int(settings.get("export.min_text_chars", 12)))
    providers = build_providers(settings, search_fn=search_fn)
    reddit = RedditHandler(db, http, pipeline, settings, providers, stats,
                           known_subreddits=queries.known_subreddits(lib), clock=clock)
    reddit.anchor_provider = queries.discovered_sub_anchors(lib)
    return App(settings, lib, db, http, stats, scorer, pipeline, providers, [reddit])
