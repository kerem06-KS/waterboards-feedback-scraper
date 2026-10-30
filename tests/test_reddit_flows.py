"""End-to-end Reddit behaviour with scripted providers (no network): paging, splitting, fallback, expansion."""
import json

import pytest

from helpers import T0, FakeArchive, FakeClock, comment, make_app, post, run_all
from wbscraper.http import FetchError
from wbscraper.sources.reddit_tasks import FAR_FUTURE

GOOD = "CIWQS website login is broken, upload keeps failing"


def search_task(app, q="CIWQS", kind="posts", sub=None, **extra):
    payload = {"kind": kind, "q": q, "subreddit": sub, "after": app.settings.since_ts, "before": FAR_FUTURE,
               "dry": 0, "depth": 0, **extra}
    app.db.add_task("reddit_search", payload, 600, "test")


def stored(app, tier="relevant"):
    return {r["uid"] for r in app.db.conn.execute("SELECT uid FROM items WHERE tier=?", (tier,))}


def test_paging_covers_every_item_when_a_page_boundary_splits_one_second(tmp_path):
    # three posts share a second and straddle the page boundary (page size 3): none may be lost
    posts = [post(f"p{i}", f"{GOOD} #{i}", ts) for i, ts in
             enumerate([T0 - 10, T0 - 20, T0 - 30, T0 - 30, T0 - 30, T0 - 99, T0 - 120, T0 - 130])]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app, sub="Wastewater")
    result = run_all(app, clock)
    assert result.stop_reason == "exhausted"
    assert stored(app) == {f"reddit:t3_p{i}" for i in range(8)}


def test_paging_always_terminates_even_if_a_whole_second_overflows_a_page(tmp_path):
    posts = [post(f"p{i}", f"{GOOD} #{i}", T0 - 30) for i in range(7)] + [post("old", GOOD + " old", T0 - 999)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app, sub="Wastewater")
    result = run_all(app, clock)
    assert result.stop_reason == "exhausted"
    assert len(arc.calls) < 10                              # bounded: no infinite loop on a stuck cursor
    assert "reddit:t3_old" in stored(app)


def test_paging_goes_back_through_history_in_time_order(tmp_path):
    posts = [post(f"p{i}", f"{GOOD} #{i}", T0 - i * 86400 * 365) for i in range(10)]
    arc = FakeArchive("pullpush", "fake-pullpush", posts=posts)
    app, clock = make_app(tmp_path, {"pullpush": arc})
    search_task(app)
    run_all(app, clock)
    assert len(stored(app)) == 10
    befores = [c[4] for c in arc.calls if c[0] == "search_posts"]
    assert befores == sorted(befores, reverse=True)           # strictly walks backwards in time
    assert len(set(befores)) == len(befores)                  # never repeats a cursor (no infinite loop)


def test_dry_pages_stop_paging_through_junk(tmp_path):
    junk = [post(f"j{i}", "completely unrelated gardening chatter about tomatoes", T0 - i) for i in range(30)]
    arc = FakeArchive("pullpush", "fake-pullpush", posts=junk)
    app, clock = make_app(tmp_path, {"pullpush": arc}, reddit={"max_dry_pages": {"pullpush": 2}})
    search_task(app, q="")                                    # matches everything
    run_all(app, clock)
    assert len([c for c in arc.calls if c[0] == "search_posts"]) == 2   # stops after 2 dry pages, not 10


def test_timeout_splits_the_window_and_still_finds_everything(tmp_path):
    posts = [post(f"p{i}", f"{GOOD} #{i}", T0 - i * 86400 * 400) for i in range(12)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts, timeout_if_window_over=3 * 365 * 86400)
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app, sub="Wastewater")
    result = run_all(app, clock)
    assert result.stop_reason == "exhausted"
    assert len(stored(app)) == 12
    assert app.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE type='reddit_search'").fetchone()[0] > 3  # was split


def test_unsplittable_timeout_falls_back_to_the_next_provider(tmp_path):
    posts = [post("p1", GOOD, T0 - 100)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    arc.always_fail = FetchError("query_timeout", "timed out", status=422)
    pull = FakeArchive("pullpush", "fake-pullpush", posts=posts)
    app, clock = make_app(tmp_path, {"arcticshift": arc, "pullpush": pull}, reddit={"max_split_depth": 0})
    search_task(app, sub="Wastewater")
    run_all(app, clock)
    assert stored(app) == {"reddit:t3_p1"}
    assert any(c[0] == "search_posts" for c in pull.calls)


@pytest.mark.parametrize("error", [
    FetchError("server", "HTTP 502", status=502),
    FetchError("rate_limited", "HTTP 429", status=429, retry_after=30),
    FetchError("timeout", "read timeout"),
    FetchError("bad_json", "malformed"),
    FetchError("forbidden", "HTTP 403", status=403),
])
def test_primary_outage_never_stops_the_run_and_fallback_delivers(tmp_path, error):
    posts = [post("p1", GOOD, T0 - 100), post("p2", GOOD + " again", T0 - 200)]
    primary = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    primary.always_fail = error
    backup = FakeArchive("pullpush", "fake-pullpush", posts=posts)
    app, clock = make_app(tmp_path, {"arcticshift": primary, "pullpush": backup})
    search_task(app, sub="Wastewater")
    result = run_all(app, clock)
    assert result.stop_reason == "exhausted"
    assert stored(app) == {"reddit:t3_p1", "reddit:t3_p2"}
    stat = app.db.conn.execute("SELECT failures FROM stats WHERE provider='arcticshift'").fetchone()
    assert stat["failures"] >= 1
    if error.kind == "forbidden":
        assert "arcticshift" in app.stats.restricted


def test_all_providers_down_defers_then_recovers_within_the_retry_window(tmp_path):
    posts = [post("p1", GOOD, T0 - 100)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    arc.errors = [FetchError("server", "HTTP 502", status=502)] * 3          # outage for the first three calls
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app, sub="Wastewater")
    result = run_all(app, clock, min_minutes=120)
    assert stored(app) == {"reddit:t3_p1"}                   # recovered after the outage
    assert result.stop_reason == "exhausted"
    assert app.stats.tasks_deferred >= 1 and app.stats.tasks_failed == 0


def test_permanent_outage_marks_task_failed_and_run_ends_cleanly(tmp_path):
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[post("p1", GOOD, T0)])
    arc.always_fail = FetchError("server", "HTTP 500", status=500)
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app, sub="Wastewater")
    result = run_all(app, clock, min_minutes=0)
    assert result.stop_reason in ("exhausted", "only_deferred_work_remains")
    assert app.db.task_counts().get("failed", 0) + app.db.task_counts().get("pending", 0) >= 1
    assert not stored(app)


def test_relevant_post_triggers_full_thread_expansion_with_context(tmp_path):
    p = post("p1", "SMARTS stormwater annual report upload keeps failing", T0 - 1000)
    comments = [
        comment("c1", "p1", "same here, the website times out on upload every time", T0 - 900),
        comment("c2", "p1", "the upload kept timing out for me too", T0 - 800),
        comment("c3", "p1", "have you tried calling the help desk about it?", T0 - 700),
        comment("c4", "p1", "Thanks, that worked!", T0 - 600),
        comment("c5", "p1", "I had lunch at noon", T0 - 500),
        comment("c6", "p1", "unrelated gardening comment about tomatoes and soil", T0 - 400),
    ]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[p], comments=comments)
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app, q="SMARTS", sub="Wastewater")
    run_all(app, clock)
    ids = stored(app)
    assert "reddit:t3_p1" in ids and "reddit:t1_c2" in ids        # buried comment w/o system name is kept
    thread_calls = [c for c in arc.calls if c[0] == "thread_comments"]
    assert len(thread_calls) >= 2                                  # 6 comments / page size 3 -> paginated
    assert app.db.thread_row("reddit:t3_p1")["expanded"] == 1


def test_relevant_comment_found_by_search_pulls_in_its_post_and_thread(tmp_path):
    p = post("p7", "need help with an old permit", T0 - 5000)       # post itself is not about the system
    comments = [comment("c9", "p7", "CIWQS login is broken for me, upload keeps failing", T0 - 4000),
                comment("c10", "p7", "my login also fails and the website is slow", T0 - 3000)]
    arc = FakeArchive("pullpush", "fake-pullpush", posts=[p], comments=comments)
    app, clock = make_app(tmp_path, {"pullpush": arc})
    search_task(app, kind="comments", q="CIWQS")
    run_all(app, clock)
    ids = stored(app, "relevant")
    assert "reddit:t1_c9" in ids and "reddit:t1_c10" in ids       # sibling comment kept through thread context
    assert any(c[0] == "posts_by_ids" for c in arc.calls)          # parent post fetched too
    parent = app.db.item_row("reddit:t3_p7")
    assert parent is None or parent["tier"] != "relevant"           # a post with no interface evidence is not exported


def test_thread_that_becomes_relevant_midway_is_rescanned(tmp_path):
    # discovered thread: early comment is only relevant with context; a later comment makes the thread relevant
    p = post("p5", "Help please", T0 - 5000)
    comments = [comment("c1", "p5", "the upload kept timing out for me too", T0 - 100),       # newest first in listing
                comment("c2", "p5", "CIWQS spill report website is broken", T0 - 4000)]        # oldest
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[p], comments=comments)
    app, clock = make_app(tmp_path, {"arcticshift": arc}, reddit={"page_size": 1})
    app.db.add_task("reddit_posts", {"ids": ["p5"], "discovered": True}, 850, "test")
    run_all(app, clock)
    assert "reddit:t1_c1" in stored(app, "relevant")


def test_new_subreddits_with_relevant_hits_are_discovered(tmp_path):
    p = post("p1", GOOD, T0 - 10, sub="SomeNewSub")
    pull = FakeArchive("pullpush", "fake-pullpush", posts=[p])
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[])
    app, clock = make_app(tmp_path, {"pullpush": pull, "arcticshift": arc})
    search_task(app)
    run_all(app, clock, max_minutes=1)
    types = app.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE family='reddit_discovered_sub'").fetchone()[0]
    assert types > 0
    assert json.loads(app.db.get_meta("discovered_subs")) == ["SomeNewSub"]


def test_link_posts_to_water_boards_domains_are_collected(tmp_path):
    p = post("p3", "Is this GeoTracker site report accurate for my house?", T0 - 50, sub="environment")
    p["url"] = "https://geotracker.waterboards.ca.gov/profile_report.asp?global_id=L1"
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[p])
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    app.db.add_task("reddit_urls", {"prefix": "https://geotracker.waterboards.ca.gov", "after": 0,
                                    "before": FAR_FUTURE, "dry": 0, "depth": 0}, 650, "test")
    run_all(app, clock)
    assert app.db.item_row("reddit:t3_p3") is not None


def test_discovery_results_become_lookups_and_expansions(tmp_path):
    p = post("abc123", "Anyone know if the water board portal is down?", T0 - 500)
    comments = [comment("k1", "abc123", "CIWQS is down for me as well, website error", T0 - 400)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[p], comments=comments)

    from wbscraper.sources.reddit_search_discovery import DiscoveryProvider
    disc = DiscoveryProvider(search_fn=lambda q, n: [
        {"href": "https://www.reddit.com/r/Wastewater/comments/abc123/portal_down/"},
        {"href": "https://forum.example.org/thread/1"}])
    app, clock = make_app(tmp_path, {"arcticshift": arc, "discovery": disc})
    app.db.add_task("reddit_discover", {"q": 'site:reddit.com "CIWQS"'}, 580, "test")
    run_all(app, clock)
    assert "reddit:t1_k1" in stored(app)
    assert app.db.urls_seen_count() == 1                           # non-Reddit page recorded for later phases
    assert app.stats.urls_discovered >= 2
