"""Fair scheduling: a large high-priority backlog (Reddit searches) must not starve other sources (PDF tasks)."""
from collections import Counter

import httpx
import respx

from helpers import FakeArchive, T0, make_app, make_pdf, post
from wbscraper.runner import Runner
from wbscraper.scheduler import FairLanes, lane_of
from wbscraper.sources.reddit_tasks import FAR_FUTURE

HOST = "https://www.waterboards.ca.gov"


class Stub:
    """Handler that completes instantly and records what ran, in order."""

    def __init__(self, types, domain, log):
        self.TASK_TYPES = tuple(types)
        self.domain, self.log = domain, log

    def next_domain(self, task_type, payload):
        return self.domain

    def earliest_ready(self, task_type, payload):
        return 0.0

    def run(self, task_type, payload, priority):
        self.log.append((task_type, payload.get("n")))


def runner_for(app, clock, handlers):
    return Runner(app.settings, app.db, app.http, handlers, app.stats, clock=clock.time, sleep=clock.sleep)


def step(runner, n):
    for _ in range(n):
        picked = runner._pick()
        if not picked:
            return
        runner._execute(*picked)


def user_shaped_queue(db):
    """The shape of the real queue: thousands of Reddit searches (600..519) beat 763 pages (489/490) beat 1,500 PDFs (480)."""
    for i in range(400):
        db.add_task("reddit_search", {"n": i}, 600, "reddit_arcticshift")
    for i in range(300):
        db.add_task("reddit_search", {"n": i}, 625, "reddit_search")
    for i in range(300):
        db.add_task("reddit_search", {"n": i}, 519, "reddit_discovered_sub")
    for i in range(200):
        db.add_task("wb_page", {"n": i}, 490, "waterboards_docs")
    for i in range(500):
        db.add_task("wb_pdf", {"n": i}, 480, "waterboards_docs")
    db.commit()


def handlers(log):
    return [Stub(["reddit_search"], "fake-arctic", log), Stub(["wb_page", "wb_pdf"], "www.waterboards.ca.gov", log)]


def test_lane_of_groups_reddit_families_and_splits_the_documents_family():
    assert lane_of("reddit_arcticshift", "reddit_search") == "reddit"
    assert lane_of("reddit_thread", "reddit_thread") == "reddit"
    assert lane_of("reddit_discovered_sub", "reddit_search") == "reddit"
    assert lane_of("reddit_discovery", "reddit_discover") == "reddit_discovery"      # slow search engine: its own lane
    assert lane_of("waterboards_docs", "wb_pdf") == "wb_pdf"
    assert lane_of("waterboards_docs", "wb_page") == "wb_page"
    assert lane_of("github", "gh_search") == "github"
    assert lane_of(None, "odd_type") == "odd_type"


def test_pdfs_are_not_starved_by_a_large_reddit_backlog(tmp_path):
    app, clock = make_app(tmp_path, {})
    user_shaped_queue(app.db)
    log: list = []
    step(runner_for(app, clock, handlers(log)), 90)
    ran = Counter(t for t, _ in log)
    assert len(log) == 90
    assert ran["wb_pdf"] >= 20 and ran["wb_page"] >= 20 and ran["reddit_search"] >= 20, ran


def test_every_lane_keeps_progressing_over_time_and_nothing_is_dropped(tmp_path):
    app, clock = make_app(tmp_path, {})
    user_shaped_queue(app.db)
    log: list = []
    runner = runner_for(app, clock, handlers(log))
    total = app.db.pending_count()
    step(runner, 150)
    first = Counter(t for t, _ in log)
    step(runner, 150)
    second = Counter(t for t, _ in log[150:])
    assert all(first[t] and second[t] for t in ("wb_pdf", "wb_page", "reddit_search")), (first, second)
    step(runner, 5000)                                           # drain: coverage is unchanged, only the order differs
    assert app.db.pending_count() == 0 and len(log) == total
    assert app.db.task_counts() == {"done": total}


def test_priority_still_orders_work_inside_a_lane(tmp_path):
    app, clock = make_app(tmp_path, {})
    for i, pri in enumerate((480, 485, 489, 483)):
        app.db.add_task("wb_pdf", {"n": pri}, pri, "waterboards_docs")
    for pri in (519, 700, 600):
        app.db.add_task("reddit_search", {"n": pri}, pri, "reddit_thread")
    app.db.commit()
    log: list = []
    step(runner_for(app, clock, handlers(log)), 7)
    assert [n for t, n in log if t == "wb_pdf"] == [489, 485, 483, 480]
    assert [n for t, n in log if t == "reddit_search"] == [700, 600, 519]


def test_a_lane_whose_domain_is_busy_is_skipped_and_not_charged_for_it(tmp_path):
    app, clock = make_app(tmp_path, {})
    user_shaped_queue(app.db)
    log: list = []
    runner = runner_for(app, clock, handlers(log))
    app.http.pacer._next_ok["fake-arctic"] = clock.now + 1000          # Reddit's provider is rate limited
    step(runner, 30)
    assert {t for t, _ in log} == {"wb_page", "wb_pdf"}                   # others carry on at full speed
    app.http.pacer._next_ok["fake-arctic"] = 0
    log.clear()
    step(runner, 40)
    ran = Counter(t for t, _ in log)
    assert 15 <= ran["reddit_search"] <= 25 and ran["wb_pdf"] >= 8, ran    # default 2:1:1 share; no catch-up burst


def test_weights_bias_the_share_but_never_starve(tmp_path):
    app, clock = make_app(tmp_path, {}, scheduler={"weights": {"reddit": 3}})
    user_shaped_queue(app.db)
    log: list = []
    step(runner_for(app, clock, handlers(log)), 120)
    ran = Counter(t for t, _ in log)
    assert ran["reddit_search"] > ran["wb_pdf"] > 0 and ran["wb_pdf"] >= 15, ran


def test_fair_lanes_start_tags_do_not_let_a_late_lane_burst():
    fl = FairLanes({})
    for _ in range(100):
        fl.charge("a")
    fl.charge("b")                         # b was idle for a long time
    order = [fl.order({"a": 1, "b": 1})[0] for _ in range(1)]
    picks = []
    for _ in range(10):
        lane = fl.order({"a": 1, "b": 1})[0]
        fl.charge(lane)
        picks.append(lane)
    assert picks.count("a") >= 4 and picks.count("b") >= 4, picks        # alternate instead of b catching up 100 turns
    assert order


@respx.mock
def test_real_handlers_process_pdfs_while_reddit_has_a_deep_backlog(tmp_path):
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[post("p1", "unrelated gardening", T0 - 100)])
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    for i in range(200):
        app.db.add_task("reddit_search", {"kind": "posts", "q": f"q{i}", "subreddit": "Wastewater", "after": 1,
                                          "before": FAR_FUTURE, "dry": 0, "depth": 0}, 600, "reddit_arcticshift")
    for i in range(30):
        app.db.add_task("wb_page", {"url": f"{HOST}/public_notices/comments/p{i}.html", "depth": 1}, 489, "waterboards_docs")
    for i in range(5):
        app.db.add_task("wb_pdf", {"url": f"{HOST}/docs/l{i}.pdf"}, 480, "waterboards_docs")
    app.db.commit()
    respx.get(f"{HOST}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /dev/\n"))
    respx.get(url__regex=rf"{HOST}/public_notices/.*").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html><body>nothing</body></html>"))
    respx.get(url__regex=rf"{HOST}/docs/l\d\.pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=make_pdf(["A page with no letter markers."])))
    step(Runner(app.settings, app.db, app.http, app.handlers, app.stats, clock=clock.time, sleep=clock.sleep), 40)
    by = {(r["type"], r["status"]): r["n"] for r in app.db.task_counts_by_type()}
    assert by.get(("wb_pdf", "done"), 0) >= 3, by                    # old scheduler: 0 until every page and search finished
