"""Test helpers: fake clock, app factory with zero pacing, scripted fake Reddit providers."""
from __future__ import annotations

from pathlib import Path

from wbscraper.app import build_app
from wbscraper.config import load_settings
from wbscraper.http import FetchError
from wbscraper.runner import Runner
from wbscraper.sources.reddit_common import comment_from_json, post_from_json
from wbscraper.sources.reddit_provider import RedditProvider, build_page
from wbscraper.stats import RunStats

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
T0 = 1_800_000_000  # fixed "now" (Jan 2027) used with the fake clock


class FakeClock:
    def __init__(self, start: float = T0):
        self.now = float(start)
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds


def make_settings(tmp_path: Path, **overrides):
    zero = {"interval": 0.0, "jitter": 0.0}
    base = {
        "paths": {"data_dir": str(tmp_path / "data")},
        "domains": {k: dict(zero) for k in
                    ("default", "api.pullpush.io", "arctic-shift.photon-reddit.com", "ddgs", "oauth.reddit.com",
                     "fake-arctic", "fake-pullpush", "fake-ddgs")},
        "reddit": {"page_size": 3, "url_prefixes": []},
        "runtime": {"stale_stop_minutes": 0, "idle_stop_wait_seconds": 300},
        "http": {"max_retries": 2, "backoff_base": 0.01, "backoff_max": 0.05},
    }
    from wbscraper.config import deep_merge
    return load_settings(REPO_CONFIG, deep_merge(base, overrides))


def make_app(tmp_path: Path, providers: dict | None = None, clock: FakeClock | None = None, **overrides):
    clock = clock or FakeClock()
    settings = make_settings(tmp_path, **overrides)
    app = build_app(settings, clock=clock.time, sleep=clock.sleep)
    if providers is not None:
        app.providers.clear()
        app.providers.update(providers)
    return app, clock


def run_all(app, clock: FakeClock, min_minutes: float = 0, max_minutes: float = 600):
    runner = Runner(app.settings, app.db, app.http, app.handlers, app.stats, clock=clock.time, sleep=clock.sleep)
    return runner.run(min_minutes, max_minutes)


def post(pid: str, title: str, ts: int, sub: str = "Wastewater", selftext: str = "") -> dict:
    return {"id": pid, "title": title, "selftext": selftext, "created_utc": ts, "subreddit": sub, "score": 1}


def comment(cid: str, pid: str, body: str, ts: int, sub: str = "Wastewater", parent: str | None = None) -> dict:
    return {"id": cid, "body": body, "created_utc": ts, "link_id": f"t3_{pid}", "parent_id": parent or f"t3_{pid}",
            "subreddit": sub, "score": 1}


class FakeArchive(RedditProvider):
    """Scripted archive honouring q/subreddit/after/before/limit with exclusive time bounds, newest first."""

    def __init__(self, name: str, domain: str, posts=(), comments=(), *, timeout_if_window_over: int | None = None,
                 global_search: bool = True, subreddit_search: bool = True):
        self.name, self.domain = name, domain
        self.posts, self.comments = list(posts), list(comments)
        self.errors: list[FetchError] = []     # raised (one per call) before normal behaviour
        self.always_fail: FetchError | None = None
        self.timeout_window = timeout_if_window_over
        self.global_search, self.subreddit_search = global_search, subreddit_search
        self.calls: list[tuple] = []

    def _maybe_fail(self, after=0, before=0):
        if self.always_fail:
            raise self.always_fail
        if self.errors:
            raise self.errors.pop(0)
        if self.timeout_window and (before - after) > self.timeout_window:
            raise FetchError("query_timeout", "timed out", status=422)

    @staticmethod
    def _slice(rows, after, before, limit):
        rows = [r for r in rows if after < r["created_utc"] < before]
        return sorted(rows, key=lambda r: -r["created_utc"])[:limit]

    def _match(self, rows, q, subreddit, field):
        out = []
        for r in rows:
            if subreddit and r.get("subreddit") != subreddit:
                continue
            if q and q.lower() not in " ".join(str(r.get(f, "")) for f in field).lower():
                continue
            out.append(r)
        return out

    def search_posts(self, http, *, q, subreddit, after, before, limit):
        from wbscraper.sources.base import Unsupported
        self.calls.append(("search_posts", q, subreddit, after, before))
        if (subreddit and not self.subreddit_search) or (not subreddit and not self.global_search):
            raise Unsupported
        self._maybe_fail(after, before)
        rows = self._match(self.posts, q, subreddit, ("title", "selftext"))
        return build_page(self._slice(rows, after, before, limit), post_from_json)

    def search_comments(self, http, *, q, subreddit, after, before, limit):
        from wbscraper.sources.base import Unsupported
        self.calls.append(("search_comments", q, subreddit, after, before))
        if (subreddit and not self.subreddit_search) or (not subreddit and not self.global_search):
            raise Unsupported
        self._maybe_fail(after, before)
        rows = self._match(self.comments, q, subreddit, ("body",))
        return build_page(self._slice(rows, after, before, limit), comment_from_json)

    def posts_by_ids(self, http, ids):
        self.calls.append(("posts_by_ids", tuple(ids)))
        self._maybe_fail()
        rows = [r for r in self.posts if r["id"] in ids]
        return build_page(rows, post_from_json)

    def thread_comments(self, http, post_id, *, after, before, limit):
        self.calls.append(("thread_comments", post_id, after, before))
        self._maybe_fail(after, before)
        rows = [r for r in self.comments if r["link_id"].endswith(post_id)]
        return build_page(self._slice(rows, after, before, limit), comment_from_json)

    def posts_by_url(self, http, prefix, *, after, before, limit):
        self.calls.append(("posts_by_url", prefix))
        self._maybe_fail(after, before)
        rows = [r for r in self.posts if r.get("url", "").startswith(prefix)]
        return build_page(self._slice(rows, after, before, limit), post_from_json)
