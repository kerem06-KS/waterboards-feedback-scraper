"""Terminal progress: a live dashboard on a TTY, periodic one-line status otherwise, plus the final summary."""
from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .stats import RunStats


def _fmt_td(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}h {s % 3600 // 60:02d}m {s % 60:02d}s"


class Dashboard:
    def __init__(self, stats: RunStats, db, *, min_minutes: float, max_minutes: float, clock=time.time,
                 console: Console | None = None, plain: bool | None = None, refresh_every: float = 1.0):
        self.stats, self.db, self.clock = stats, db, clock
        self.min_minutes, self.max_minutes = min_minutes, max_minutes
        self.console = console or Console()
        self.plain = (not self.console.is_terminal) if plain is None else plain
        self.refresh_every = refresh_every
        self._last = 0.0
        self._live: Live | None = None
        self._queue_cache: tuple[float, dict, int] = (0.0, {}, 0)

    def start(self) -> None:
        if not self.plain:
            self._live = Live(self._render(), console=self.console, refresh_per_second=2, transient=False)
            self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.update(self._render())
            self._live.stop()
            self._live = None

    def _queue(self) -> tuple[dict, int]:
        now = self.clock()
        if now - self._queue_cache[0] > 5:
            q = {r["type"]: r["n"] for r in self.db.conn.execute(
                "SELECT type, COUNT(*) AS n FROM tasks WHERE status='pending' GROUP BY type")}
            threads = self.db.conn.execute(
                "SELECT COUNT(DISTINCT thread_uid) FROM items WHERE tier='relevant' AND thread_uid IS NOT NULL").fetchone()[0]
            self._queue_cache = (now, q, threads)
        return self._queue_cache[1], self._queue_cache[2]

    def update(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last < self.refresh_every * (30 if self.plain else 1):
            return
        self._last = now
        if self.plain:
            s = self.stats
            print(f"[{_fmt_td(now - s.started)}] src={s.current_source} queries={s.queries_done} pages={s.pages_fetched} "
                  f"relevant={s.relevant} borderline={s.borderline} dup={s.duplicates} failed_req={s.failed_requests}",
                  file=sys.stdout, flush=True)
        elif self._live:
            self._live.update(self._render())

    def _render(self) -> Panel:
        s = self.stats
        queue, threads = self._queue()
        elapsed = self.clock() - s.started
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", justify="right")
        t.add_column()
        t.add_row("Elapsed", f"{_fmt_td(elapsed)}   (min {self.min_minutes:g}m / max {self.max_minutes:g}m; "
                             f"{_fmt_td(s.prior_runtime)} in earlier sessions)")
        t.add_row("Current", f"{s.current_source}: {s.current_task}")
        t.add_row("Searches done", f"{s.queries_done}   pages fetched: {s.pages_fetched}   URLs discovered: {s.urls_discovered}")
        t.add_row("Candidates", f"{s.candidates}   relevant: [green]{s.relevant}[/]   borderline: [yellow]{s.borderline}[/]"
                                f"   below threshold: {s.below_threshold}")
        t.add_row("Unique threads", f"{threads} with relevant items   duplicates removed: {s.duplicates}")
        t.add_row("Requests", f"failed: [red]{s.failed_requests}[/]   tasks done {s.tasks_done}, deferred {s.tasks_deferred}, "
                              f"failed {s.tasks_failed}")
        t.add_row("Queue", ", ".join(f"{k.replace('reddit_', '')}={v}" for k, v in sorted(queue.items())) or "empty")
        if s.restricted:
            t.add_row("Restricted", ", ".join(sorted(s.restricted)))
        if s.cooling:
            t.add_row("Cooling down", ", ".join(f"{k} {v:.0f}s" for k, v in s.cooling.items()))
        t.add_row("CSV", s.csv_path or "-")
        if s.last_error:
            t.add_row("Last error", f"[dim]{s.last_error}[/]")
        return Panel(t, title="Water Boards feedback scraper  (Ctrl+C to stop safely)", border_style="blue")


def print_summary(console: Console, db, stats: RunStats, stop_reason: str, elapsed: float, csv_path: str,
                  csv_rows: int, include_borderline: bool) -> None:
    tiers = db.tier_counts()
    console.print()
    console.rule("[bold]Run summary")
    console.print(f"Stopped: [bold]{stop_reason.replace('_', ' ')}[/] after {_fmt_td(elapsed)}")
    t = Table(title="Contribution by source / method")
    for col in ("Provider", "Method", "Requests", "OK", "Failed", "Records seen", "New stored", "New relevant"):
        t.add_column(col, justify="left" if col in ("Provider", "Method") else "right")
    for r in db.conn.execute("SELECT * FROM stats ORDER BY provider, method"):
        t.add_row(r["provider"], r["method"], str(r["requests"]), str(r["ok"]), str(r["failures"]),
                  str(r["items_seen"]), str(r["items_new"]), str(r["relevant_new"]))
    console.print(t)
    threads = db.conn.execute(
        "SELECT COUNT(DISTINCT thread_uid) FROM items WHERE tier='relevant' AND thread_uid IS NOT NULL").fetchone()[0]
    console.print(f"Stored items: relevant [green]{tiers.get('relevant', 0)}[/], borderline [yellow]{tiers.get('borderline', 0)}[/], "
                  f"other {tiers.get('irrelevant', 0)}  |  unique threads with relevant items: {threads}")
    console.print(f"Duplicates removed: {stats.duplicates}   failed requests: {stats.failed_requests}   "
                  f"tasks failed: {stats.tasks_failed}")
    if stats.restricted:
        console.print(f"[red]Sources restricted this run:[/] {', '.join(sorted(stats.restricted))}")
    qc = db.task_counts()
    if qc.get("pending"):
        console.print(f"Unfinished work remains: {qc['pending']} task(s). Run [bold]wbscraper resume[/] to continue.")
    which = "relevant + borderline" if include_borderline else "relevant only"
    console.print(f"CSV: [bold]{csv_path}[/]  ({csv_rows} rows, {which}; borderline items: "
                  f"{tiers.get('borderline', 0)} -> add --include-borderline to export them)")
