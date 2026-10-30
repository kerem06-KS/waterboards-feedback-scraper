"""Fair scheduling between sources.

Task priority orders work *inside* a lane (thread expansion before ID lookups before broad searches ...). Between
lanes the runner uses start-time fair queueing, so a huge backlog in one lane (thousands of Reddit searches) can no
longer starve the others (Water Boards PDFs, GitHub, Stack Exchange ...). Nothing is dropped or reordered
permanently: every lane keeps its own priority queue and every task still runs; only the interleaving changes.

A lane is charged only when one of its tasks actually runs. A lane whose domain is busy is skipped for that pick at no
cost, and a lane that was idle does not get to "catch up" in a burst afterwards.
"""
from __future__ import annotations

# Reddit's providers, thread expansion and post lookups are one lane: they share the priority order that puts
# thread expansion first. The search-engine discovery runs on its own slow domain and is a separate lane.
_REDDIT_SEPARATE = {"reddit_discovery"}
_SPLIT_BY_TYPE = {"waterboards_docs"}          # HTML crawl pages and PDFs must not wait behind each other


def lane_of(family: str | None, task_type: str) -> str:
    family = family or ""
    if family.startswith("reddit_") and family not in _REDDIT_SEPARATE:
        return "reddit"
    if family in _SPLIT_BY_TYPE or not family:
        return task_type
    return family


class FairLanes:
    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = {k: float(v) for k, v in (weights or {}).items() if float(v) > 0}
        self._finish: dict[str, float] = {}
        self._vclock = 0.0

    def _start(self, lane: str) -> float:
        return max(self._finish.get(lane, 0.0), self._vclock)

    def order(self, best_priority: dict[str, int]) -> list[str]:
        """Lanes with ready work, the one that is most behind its fair share first (higher priority breaks ties)."""
        return sorted(best_priority, key=lambda lane: (self._start(lane), -best_priority[lane], lane))

    def charge(self, lane: str) -> None:
        start = self._start(lane)
        self._vclock = start
        self._finish[lane] = start + 1.0 / self.weights.get(lane, 1.0)
