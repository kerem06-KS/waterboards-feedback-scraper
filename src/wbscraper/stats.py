"""In-memory counters shown on the live dashboard (durable counters live in SQLite)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RunStats:
    started: float = field(default_factory=time.time)
    prior_runtime: float = 0.0
    current_source: str = "-"
    current_task: str = "-"
    queries_done: int = 0
    pages_fetched: int = 0
    urls_discovered: int = 0
    candidates: int = 0
    relevant: int = 0
    borderline: int = 0
    duplicates: int = 0
    skipped_empty: int = 0
    below_threshold: int = 0
    failed_requests: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    tasks_deferred: int = 0
    restricted: set = field(default_factory=set)
    cooling: dict = field(default_factory=dict)
    last_error: str = ""
    csv_path: str = ""
    queue_by_type: dict = field(default_factory=dict)
    unique_relevant_threads: int = 0
