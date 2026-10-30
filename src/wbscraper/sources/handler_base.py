"""Base class for single-provider API/page handlers (Stack Exchange, Hacker News, GitHub, Water Boards documents…).

A handler implements `_run_<task_type>(payload, priority)` methods; this class supplies domain/cooldown checks for the
scheduler and records statistics and provider health. Exceptions (FetchError) propagate to the runner, which defers
or fails the task - one broken adapter never stops the run.
"""
from __future__ import annotations

import logging
import time

from ..db import Database
from ..http import FetchError, HttpClient
from ..pipeline import IngestResult, Pipeline
from ..stats import RunStats
from .base import RawItem

log = logging.getLogger("wbscraper.handler")


class ApiHandler:
    TASK_TYPES: set[str] = set()
    PROVIDER = "api"
    DOMAIN = "default"

    def __init__(self, db: Database, http: HttpClient, pipeline: Pipeline, settings, stats: RunStats,
                 cfg: dict | None = None, clock=time.time):
        self.db, self.http, self.pipeline, self.settings, self.stats, self.clock = db, http, pipeline, settings, stats, clock
        self.cfg: dict = cfg or {}

    # -- scheduler hooks ---------------------------------------------------------------------
    def domain_for(self, task_type: str, payload: dict) -> str:
        return self.DOMAIN

    def next_domain(self, task_type: str, payload: dict) -> str | None:
        dom = self.domain_for(task_type, payload)
        return dom if self.http.cooldown_remaining(dom) <= 0 else None

    def earliest_ready(self, task_type: str, payload: dict) -> float:
        return self.http.cooldown_remaining(self.domain_for(task_type, payload))

    # -- execution -----------------------------------------------------------------------------
    def run(self, task_type: str, payload: dict, priority: int) -> None:
        self.stats.current_source = self.PROVIDER
        try:
            result = getattr(self, f"_run_{task_type}")(payload, priority)
        except FetchError as err:
            self.db.bump_stat(self.PROVIDER, task_type, requests=1, failures=1,
                              restricted=1 if err.kind == "forbidden" else 0)
            self.db.set_stat_error(self.PROVIDER, task_type, str(err))
            self.db.health_failure(self.PROVIDER, str(err))
            if err.kind == "forbidden":
                self.stats.restricted.add(self.PROVIDER)
            self.stats.last_error = f"{self.PROVIDER}: {err}"[:160]
            raise
        n_raw, newest = result if result else (0, None)
        self.db.bump_stat(self.PROVIDER, task_type, requests=1, ok=1, items_seen=n_raw)
        self.db.health_success(self.PROVIDER, newest)
        self.stats.pages_fetched += 1
        self.db.commit()

    def ingest(self, raws: list[RawItem], method: str) -> list[IngestResult]:
        results = self.pipeline.ingest_many(raws, self.PROVIDER, method)
        stored = sum(1 for r in results if r.status == "stored")
        rel = sum(1 for r in results if r.status == "stored" and r.tier == "relevant")
        self.db.bump_stat(self.PROVIDER, method, items_new=stored, relevant_new=rel)
        return results
