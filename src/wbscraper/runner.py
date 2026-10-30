"""Main collection loop: one task at a time, highest priority first, domains paced independently.

Stop conditions (no artificial sleeping): all work exhausted, max runtime reached, Ctrl+C, nothing ready for a long
time after the minimum runtime (only retry-waiting work left), or no new relevant item for `stale_stop_minutes`.
Before the minimum runtime, deferred work (e.g. a provider that is down) keeps being retried on its cooldown schedule.
"""
from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass
from typing import Callable

from .db import Database
from .http import FetchError, HttpClient
from .sources.base import AllProvidersFailed
from .stats import RunStats

log = logging.getLogger("wbscraper.runner")

SEARCH_SUFFIXES = ("_search", "_urls", "_discover")


@dataclass
class RunResult:
    stop_reason: str
    elapsed_seconds: float
    session_id: int


class Runner:
    def __init__(self, settings, db: Database, http: HttpClient, handlers: list, stats: RunStats, *,
                 progress=None, exporter: Callable[[], object] | None = None, clock=time.time, sleep=time.sleep):
        self.settings, self.db, self.http, self.stats = settings, db, http, stats
        self.handlers = {t: h for h in handlers for t in h.TASK_TYPES}
        self.progress = progress
        self.exporter = exporter
        self.clock, self.sleep = clock, sleep
        rt = settings.get("runtime", {}) or {}
        self.max_attempts = int(rt.get("max_task_attempts", 4))
        self.retry_base = float(rt.get("retry_base_seconds", 600))
        self.retry_max = float(rt.get("retry_max_seconds", 3600))
        self.idle_stop_wait = float(rt.get("idle_stop_wait_seconds", 300))
        self.export_every = float(rt.get("export_every_minutes", 10)) * 60
        self.stale_stop = float(rt.get("stale_stop_minutes", 60)) * 60
        self._stop = False
        self._wait_hint = 0.0

    # ------------------------------------------------------------------ control
    def request_stop(self) -> None:
        self._stop = True

    def _install_signal(self):
        previous = signal.getsignal(signal.SIGINT)

        def handler(signum, frame):
            if self._stop:  # second Ctrl+C: leave immediately (state is already committed)
                signal.signal(signal.SIGINT, previous)
                raise KeyboardInterrupt
            self._stop = True
            log.warning("Ctrl+C received: finishing the current task, then saving and exiting")

        try:
            signal.signal(signal.SIGINT, handler)
        except ValueError:  # not in main thread (tests)
            return None
        return previous

    # ------------------------------------------------------------------ scheduling
    def _pick(self):
        now = self.clock()
        shortest = float("inf")
        for row in self.db.ready_tasks(now, 300):
            handler = self.handlers.get(row["type"])
            if handler is None:
                self.db.retry_task(row["id"], f"no handler for {row['type']}", now + 86400, count_attempt=False)
                continue
            payload = json.loads(row["payload"])
            domain = handler.next_domain(row["type"], payload)
            if domain is None:
                rem = handler.earliest_ready(row["type"], payload)
                delay = 3600.0 if rem == float("inf") else rem + 1
                self.db.retry_task(row["id"], "all providers cooling down or unavailable", now + delay,
                                   count_attempt=False)
                continue
            wait = self.http.wait_needed(domain)
            if wait <= 0:
                return row, payload, handler
            shortest = min(shortest, wait)
        earliest = self.db.earliest_pending_eligible()
        until_eligible = max(0.0, earliest - now) if earliest is not None else float("inf")
        self._wait_hint = min(shortest, until_eligible) if shortest != float("inf") or until_eligible != float("inf") else 0.0
        return None

    def _execute(self, row, payload, handler) -> None:
        task_id, task_type = row["id"], row["type"]
        self.db.mark_in_progress(task_id)
        try:
            handler.run(task_type, payload, row["priority"])
        except AllProvidersFailed as exc:
            self._fail(row, str(exc), counted=exc.counted, not_before=exc.retry_at)
            return
        except FetchError as exc:
            self._fail(row, str(exc), counted=True)
            return
        except Exception as exc:  # an adapter bug must never stop the run
            log.exception("task %s (%s) crashed", task_id, task_type)
            self._fail(row, f"{type(exc).__name__}: {exc}", counted=True)
            return
        self.db.complete_task(task_id)
        self.stats.tasks_done += 1
        if task_type.endswith(SEARCH_SUFFIXES):
            self.stats.queries_done += 1
        self.db.commit()

    def _fail(self, row, error: str, *, counted: bool, not_before: float | None = None) -> None:
        now = self.clock()
        attempts = row["attempts"] + (1 if counted else 0)
        if counted and attempts >= self.max_attempts:
            self.db.retry_task(row["id"], error, now, count_attempt=True, final=True)
            self.stats.tasks_failed += 1
            log.error("task %s (%s) failed permanently: %s", row["id"], row["type"], error)
            return
        backoff = min(self.retry_max, self.retry_base * (2 ** max(attempts - 1, 0))) if counted else 60.0
        retry_at = max(now + backoff, not_before or 0.0) if counted else (not_before or now + backoff)
        self.db.retry_task(row["id"], error, retry_at, count_attempt=counted)
        self.stats.tasks_deferred += 1
        self.stats.last_error = error[:160]
        log.warning("task %s (%s) deferred %.0fs: %s", row["id"], row["type"], retry_at - now, error)

    # ------------------------------------------------------------------ main loop
    def run(self, min_minutes: float, max_minutes: float, config_snapshot: dict | None = None) -> RunResult:
        min_s, max_s = float(min_minutes) * 60, float(max_minutes) * 60
        self.db.reset_in_progress()
        session = self.db.start_session(config_snapshot or {})
        start = self.clock()
        self.stats.started = start
        self.stats.prior_runtime = self.db.total_runtime_seconds()
        last_export = start
        last_new = start
        seen_good = self.stats.relevant + self.stats.borderline
        reason = "exhausted"
        previous = self._install_signal()
        try:
            while True:
                now = self.clock()
                elapsed = now - start
                if self._stop:
                    reason = "interrupted"
                    break
                if elapsed >= max_s:
                    reason = "max_minutes_reached"
                    break
                good = self.stats.relevant + self.stats.borderline
                if good > seen_good:
                    seen_good, last_new = good, now
                if elapsed >= min_s and self.stale_stop > 0 and now - last_new >= self.stale_stop:
                    reason = "diminishing_returns"
                    break
                picked = self._pick()
                if picked:
                    row, payload, handler = picked
                    self._execute(row, payload, handler)
                    self._refresh(now)
                else:
                    if self.db.pending_count() == 0:
                        reason = "exhausted"
                        break
                    if elapsed >= min_s and self._wait_hint > self.idle_stop_wait:
                        reason = "only_deferred_work_remains"
                        break
                    self._refresh(now)
                    self.sleep(max(0.05, min(self._wait_hint or 1.0, 1.0)))
                if self.exporter and now - last_export >= self.export_every:
                    self.exporter()
                    last_export = now
        except KeyboardInterrupt:
            reason = "interrupted"
        finally:
            if previous is not None:
                try:
                    signal.signal(signal.SIGINT, previous)
                except ValueError:
                    pass
            elapsed = self.clock() - start
            self.db.end_session(session, elapsed, reason)
            if self.exporter:
                try:
                    self.exporter()
                except Exception:
                    log.exception("final export failed")
            self._refresh(0, force=True)
        return RunResult(reason, elapsed, session)

    def _refresh(self, _now: float, force: bool = False) -> None:
        for dom in ("api.pullpush.io", "arctic-shift.photon-reddit.com", "ddgs", "oauth.reddit.com"):
            rem = self.http.cooldown_remaining(dom)
            if rem > 0:
                self.stats.cooling[dom] = rem
            else:
                self.stats.cooling.pop(dom, None)
        if self.progress:
            self.progress.update(force=force)
