import json

from helpers import T0, FakeArchive, FakeClock, comment, make_app, post, run_all
from wbscraper.export import export_csv
from wbscraper.http import FetchError
from wbscraper.runner import Runner
from wbscraper.sources.reddit_tasks import FAR_FUTURE
from wbscraper import queries

GOOD = "CIWQS website login is broken, upload keeps failing"


def add(app, q="CIWQS", kind="posts", sub=None):
    app.db.add_task("reddit_search", {"kind": kind, "q": q, "subreddit": sub, "after": app.settings.since_ts,
                                      "before": FAR_FUTURE, "dry": 0, "depth": 0}, 600, "t")


def test_mini_run_end_to_end_produces_the_csv(tmp_path):
    posts = [post("p1", GOOD, T0 - 100), post("p2", "weather chat about tomatoes", T0 - 200)]
    app, clock = make_app(tmp_path, {"pullpush": FakeArchive("pullpush", "fake-pullpush", posts=posts)})
    add(app, q="")
    result = run_all(app, clock)
    res = export_csv(app.db, app.settings.csv_path)
    assert result.stop_reason == "exhausted" and res.rows == 1
    lines = app.settings.csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '"text","date","source_url"'


def test_run_does_not_generate_filler_requests_when_work_is_exhausted(tmp_path):
    arc = FakeArchive("pullpush", "fake-pullpush", posts=[post("p1", GOOD, T0 - 100)])
    app, clock = make_app(tmp_path, {"pullpush": arc})
    add(app)
    run_all(app, clock, min_minutes=120)                      # minimum runtime must NOT pad with fake work
    assert [c[0] for c in arc.calls] == ["search_posts", "thread_comments"]   # one search + its thread, nothing else
    assert clock.now - T0 < 60                                # and must not sleep for two hours


def test_interrupted_run_resumes_without_repeating_finished_work(tmp_path):
    posts = [post(f"p{i}", f"{GOOD} #{i}", T0 - i * 10) for i in range(9)]
    arc = FakeArchive("pullpush", "fake-pullpush", posts=posts)
    app, clock = make_app(tmp_path, {"pullpush": arc})
    add(app)
    runner = Runner(app.settings, app.db, app.http, app.handlers, app.stats, clock=clock.time, sleep=clock.sleep)
    original = app.handlers[0].run
    state = {"n": 0}

    def stop_after_first(task_type, payload, priority):
        original(task_type, payload, priority)
        state["n"] += 1
        runner.request_stop()                                 # what Ctrl+C does: finish the task, then stop

    app.handlers[0].run = stop_after_first
    first = runner.run(0, 600)
    assert first.stop_reason == "interrupted"
    calls_before = len(arc.calls)
    assert app.db.pending_count() >= 1 and 0 < len(app.db.tier_counts()) 

    app.handlers[0].run = original
    second = run_all(app, clock)                              # "wbscraper resume"
    assert second.stop_reason == "exhausted"
    searches = [c for c in arc.calls if c[0] == "search_posts"]
    assert len({c[4] for c in searches}) == len(searches)       # no search cursor was fetched twice
    assert len(arc.calls) > calls_before
    assert app.db.tier_counts()["relevant"] == 9


def test_crash_leaves_in_progress_tasks_that_are_recovered(tmp_path):
    arc = FakeArchive("pullpush", "fake-pullpush", posts=[post("p1", GOOD, T0 - 100)])
    app, clock = make_app(tmp_path, {"pullpush": arc})
    add(app)
    row = app.db.ready_tasks(0)[0]
    app.db.mark_in_progress(row["id"])                        # simulate process killed mid-task
    assert app.db.pending_count() == 0
    run_all(app, clock)
    assert app.db.tier_counts().get("relevant") == 1


def test_adapter_exception_only_fails_that_task(tmp_path):
    arc = FakeArchive("pullpush", "fake-pullpush", posts=[post("p1", GOOD, T0 - 100)])
    app, clock = make_app(tmp_path, {"pullpush": arc})
    add(app, q="CIWQS")
    add(app, q="boom")
    real = arc.search_posts

    def flaky(http, **kw):
        if kw["q"] == "boom":
            raise ValueError("adapter bug")
        return real(http, **kw)

    arc.search_posts = flaky
    result = run_all(app, clock, min_minutes=0)
    assert result.stop_reason in ("exhausted", "only_deferred_work_remains")
    assert app.db.tier_counts().get("relevant") == 1


def test_max_runtime_is_enforced(tmp_path):
    arc = FakeArchive("pullpush", "fake-pullpush")
    arc.always_fail = FetchError("server", "HTTP 500", status=500)
    app, clock = make_app(tmp_path, {"pullpush": arc})
    add(app)
    result = run_all(app, clock, min_minutes=1000, max_minutes=30)
    assert result.stop_reason == "max_minutes_reached"
    assert 30 * 60 <= clock.now - T0 < 31 * 60 + 5


def test_diminishing_returns_stops_after_the_minimum_runtime(tmp_path):
    arc = FakeArchive("pullpush", "fake-pullpush", posts=[])
    arc.always_fail = FetchError("server", "HTTP 500", status=500)
    app, clock = make_app(tmp_path, {"pullpush": arc}, runtime={"stale_stop_minutes": 20, "idle_stop_wait_seconds": 99999})
    add(app)
    result = run_all(app, clock, min_minutes=10, max_minutes=600)
    assert result.stop_reason == "diminishing_returns"


def test_seeding_is_idempotent_so_resume_does_not_duplicate_work(tmp_path):
    app, _ = make_app(tmp_path, {})
    first = queries.seed(app.db, app.lib, app.settings)
    second = queries.seed(app.db, app.lib, app.settings)
    assert first > 1000 and second == 0
    keys = [r[0] for r in app.db.conn.execute("SELECT key FROM tasks")]
    assert len(keys) == len(set(keys))


def test_a_busy_domain_does_not_hide_work_for_idle_domains(tmp_path):
    """300+ high-priority tasks waiting on one rate-limited domain must not starve another domain's low-priority task."""
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[])
    from wbscraper.sources.reddit_search_discovery import DiscoveryProvider
    disc = DiscoveryProvider(search_fn=lambda q, n: [])
    app, clock = make_app(tmp_path, {"arcticshift": arc, "discovery": disc})
    for i in range(400):
        app.db.add_task("reddit_search", {"kind": "posts", "q": f"q{i}", "subreddit": "Wastewater", "after": 1,
                                          "before": FAR_FUTURE, "dry": 0, "depth": 0}, 600, "reddit_arcticshift")
    app.db.add_task("reddit_discover", {"q": 'site:reddit.com "CIWQS"'}, 100, "reddit_discovery")
    app.http.pacer._next_ok["fake-arctic"] = clock.now + 1000      # Arctic Shift is "busy" (rate limited)
    arc.domain = "fake-arctic"
    runner = Runner(app.settings, app.db, app.http, app.handlers, app.stats, clock=clock.time, sleep=clock.sleep)
    picked = runner._pick()
    # the Arctic provider's real domain is fake-arctic here, so the search-engine task must be chosen
    assert picked is not None and picked[0]["type"] == "reddit_discover"
