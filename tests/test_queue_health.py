"""Reddit search queue health: random timeouts must not blow up the task tree, deep windows must not starve other
searches, discovered subreddits get a bounded budget, and the maintenance tools never delete anything."""
import json
import random

from helpers import T0, FakeArchive, make_app, post, run_all
from wbscraper.docs_recovery import requeue_unclassified_pdfs
from wbscraper.http import FetchError
from wbscraper.queue_tidy import rebalance_split_priorities
from wbscraper.rescore import prune_stale_work
from wbscraper.sources.reddit_tasks import FAR_FUTURE

GOOD = "CIWQS website login is broken, upload keeps failing"
TIMEOUT = FetchError("query_timeout", "timed out", status=422)


def search_task(app, q="CIWQS", sub="Wastewater", priority=600, **extra):
    payload = {"kind": "posts", "q": q, "subreddit": sub, "after": app.settings.since_ts, "before": FAR_FUTURE,
               "dry": 0, "depth": 0, **extra}
    app.db.add_task("reddit_search", payload, priority, "reddit_arcticshift")


def searches(app):
    return [(json.loads(r["payload"]), r["priority"], r["status"])
            for r in app.db.conn.execute("SELECT payload, priority, status FROM tasks WHERE type='reddit_search'")]


class RandomTimeouts(FakeArchive):
    """Times out at random whatever the window (the behaviour measured on the real archive)."""

    def __init__(self, *a, rate=0.67, seed=1, **kw):
        super().__init__(*a, **kw)
        self.rng, self.rate = random.Random(seed), rate

    def _maybe_fail(self, after=0, before=0):
        if self.rng.random() < self.rate:
            raise TIMEOUT
        super()._maybe_fail(after, before)


def test_a_timed_out_window_is_retried_before_it_is_split(tmp_path):
    posts = [post("p1", GOOD, T0 - 100)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    arc.errors = [TIMEOUT, TIMEOUT]                    # two random timeouts, then the archive answers
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app)
    run_all(app, clock)
    rows = searches(app)
    assert not [1 for p, _, _ in rows if p["depth"] > 0]                       # never split
    assert sorted(p.get("timeouts", 0) for p, _, _ in rows) == [0, 1, 2]       # same window, three attempts
    assert {r["uid"] for r in app.db.conn.execute("SELECT uid FROM items")} == {"reddit:t3_p1"}


def test_a_window_that_keeps_timing_out_is_split_with_lower_priority_children(tmp_path):
    posts = [post(f"p{i}", f"{GOOD} #{i}", T0 - i * 86400 * 400) for i in range(12)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts, timeout_if_window_over=3 * 365 * 86400)
    app, clock = make_app(tmp_path, {"arcticshift": arc})
    search_task(app)
    run_all(app, clock)
    rows = searches(app)
    assert len(app.db.conn.execute("SELECT 1 FROM items").fetchall()) == 12            # still finds everything
    by_depth = {}
    for p, pri, _ in rows:
        by_depth.setdefault(p["depth"], set()).add(pri)
    assert by_depth[0] == {600} and by_depth[1] == {590} and by_depth[2] == {580}       # breadth before depth
    children = [p for p, _, _ in rows if p["depth"] == 1]
    assert children and all(p.get("timeouts", 0) <= 2 for p in children)


def test_random_timeouts_no_longer_explode_the_task_tree(tmp_path):
    def run(retries):
        posts = [post(f"s{s}p{i}", f"{GOOD} #{i}", T0 - i * 86400 * 30, sub=f"sub{s}") for s in range(4) for i in range(2)]
        arc = RandomTimeouts("arcticshift", "fake-arctic", posts=posts, rate=0.67, seed=11)
        app, clock = make_app(tmp_path / f"r{retries}", {"arcticshift": arc}, reddit={"timeout_retries": retries, "max_split_depth": 6})
        for s in range(4):
            search_task(app, q="CIWQS", sub=f"sub{s}")
        run_all(app, clock, max_minutes=15)
        n = app.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE type='reddit_search'").fetchone()[0]
        found = app.db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return n, found

    old_n, old_found = run(0)                              # the previous behaviour: split on the first timeout
    new_n, new_found = run(2)
    assert new_n * 3 < old_n, (new_n, old_n)                # far fewer tasks for the same searches ...
    assert new_found == 8 and new_found >= old_found       # ... and every post still found


def test_paging_continuations_do_not_inherit_the_retry_counter(tmp_path):
    posts = [post(f"p{i}", f"{GOOD} #{i}", T0 - 10 * i) for i in range(8)]
    arc = FakeArchive("arcticshift", "fake-arctic", posts=posts)
    arc.errors = [TIMEOUT]
    app, clock = make_app(tmp_path, {"arcticshift": arc}, reddit={"page_size": 3})
    search_task(app)
    run_all(app, clock)
    rows = searches(app)
    paged = [p for p, _, _ in rows if p["before"] != FAR_FUTURE and p["depth"] == 0]
    assert paged and all("timeouts" not in p for p in paged)
    assert len(app.db.conn.execute("SELECT 1 FROM items").fetchall()) == 8


def test_a_discovered_subreddit_gets_a_bounded_search_budget(tmp_path):
    p = post("p1", GOOD, T0 - 10, sub="SomeNewSub")
    pull = FakeArchive("pullpush", "fake-pullpush", posts=[p])
    arc = FakeArchive("arcticshift", "fake-arctic", posts=[])
    app, clock = make_app(tmp_path, {"pullpush": pull, "arcticshift": arc})
    search_task(app, sub=None)
    run_all(app, clock, max_minutes=1)
    kinds = {k: n for k, n in app.db.conn.execute(
        "SELECT json_extract(payload,'$.kind'), COUNT(*) FROM tasks WHERE family='reddit_discovered_sub' GROUP BY 1")}
    assert kinds == {"posts": 40, "comments": 8}            # was ~266 + ~266 (every tier 1-2 anchor, uncompacted)


def test_rebalance_ranks_split_children_breadth_first_and_is_repeatable(tmp_path):
    app, _ = make_app(tmp_path, {})
    search_task(app, q="A", priority=600)                                          # root (done below)
    search_task(app, q="A", priority=600, depth=1, after=1, before=2)
    search_task(app, q="A", priority=600, depth=2, after=1, before=3)
    search_task(app, q="A", priority=600, depth=3, after=1, before=4)
    search_task(app, q="B", sub="Hydrology", priority=560, depth=2, after=1, before=3)   # root B missing from the queue
    app.db.conn.execute("UPDATE tasks SET status='done' WHERE json_extract(payload,'$.depth')=0")
    app.db.conn.execute("UPDATE tasks SET status='done' WHERE json_extract(payload,'$.q')='A' AND json_extract(payload,'$.depth')=3")
    app.db.commit()
    dry = rebalance_split_priorities(app.db, 10, dry_run=True)
    assert dry["reprioritised"] == 2 and {r[0] for r in app.db.conn.execute("SELECT priority FROM tasks")} == {600, 560}
    res = rebalance_split_priorities(app.db, 10)
    assert res["reprioritised"] == 2 and res["root_not_found"] == 1 and res["by_depth"] == {1: 1, 2: 1}
    got = {json.loads(r["payload"])["depth"]: r["priority"] for r in app.db.conn.execute(
        "SELECT payload, priority FROM tasks WHERE json_extract(payload,'$.q')='A'")}
    assert got == {0: 600, 1: 590, 2: 580, 3: 600}                                  # the done task is untouched
    assert rebalance_split_priorities(app.db, 10)["reprioritised"] == 0              # idempotent


def test_prune_dry_run_reports_without_changing_anything(tmp_path):
    app, _ = make_app(tmp_path, {})
    app.db.set_meta("discovered_subs", json.dumps(["junk"]))
    app.db.add_task("reddit_search", {"kind": "posts", "q": "X", "subreddit": "junk", "after": 1, "before": 2, "dry": 0,
                                      "depth": 0}, 520, "reddit_discovered_sub")
    app.db.commit()
    dry = prune_stale_work(app.db, [], dry_run=True)
    assert dry["discovered_subreddit_searches"] == 1 and app.db.task_counts() == {"pending": 1}
    assert json.loads(app.db.get_meta("discovered_subs")) == ["junk"]
    real = prune_stale_work(app.db, [])
    assert real["discovered_subreddit_searches"] == 1 and app.db.task_counts() == {"cancelled": 1}


def test_unclassified_pdfs_are_requeued_with_the_letter_hint_and_nothing_else(tmp_path):
    H = "https://www.waterboards.ca.gov"
    app, _ = make_app(tmp_path, {})
    index = f"{H}/public_notices/comments/2015.html"
    other = f"{H}/about/staff.html"
    cases = {
        f"{H}/docs/attachment3.pdf": (index, "new", False),          # unclassified legacy PDF on a comments page -> requeue
        f"{H}/docs/jsmith.pdf": (index, "new", False),               # same
        f"{H}/docs/letter_a.pdf": (index, "new", True),              # already became a letter (in items) -> no
        f"{H}/docs/staff_report.pdf": (index, "new", False),         # agency file name -> no
        f"{H}/docs/misc.pdf": (other, "new", False),                 # not from a comments page -> no
        f"{H}/docs/scan1.pdf": (index, "scanned", False),            # outcome known -> no
    }
    for url, (parent, status, is_letter) in cases.items():
        app.db.add_url_seen(url, "www.waterboards.ca.gov", parent)
        app.db.set_url_status(url, status)
        app.db.add_task("wb_pdf", {"url": url}, 480, "waterboards_docs")
        if is_letter:
            app.db.insert_item(uid="wb:aaaa:1:0", source="waterboards", kind="letter", url=f"{url}#page=1", thread_uid=None,
                               parent_uid=None, community="www.waterboards.ca.gov", text="A letter about the portal.", date=None,
                               tier="irrelevant", own_relevant=0, score=0, entity_hit=0, reasons="[]", text_hash="h", flags="")
    app.db.conn.execute("UPDATE tasks SET status='done' WHERE type='wb_pdf'")
    app.db.commit()
    assert requeue_unclassified_pdfs(app.db, dry_run=True) == {"candidates": 2, "requeued": 0, "dry_run": True}
    res = requeue_unclassified_pdfs(app.db)
    assert res == {"candidates": 2, "requeued": 2, "dry_run": False}
    pend = [json.loads(r["payload"]) for r in app.db.conn.execute("SELECT payload FROM tasks WHERE status='pending'")]
    assert {p["url"].rsplit("/", 1)[-1] for p in pend} == {"attachment3.pdf", "jsmith.pdf"}
    assert all(p["ctx"] == "letter" and p["pass"] == 2 for p in pend)
    assert app.db.task_counts() == {"done": 6, "pending": 2}                          # finished tasks untouched
    assert requeue_unclassified_pdfs(app.db)["requeued"] == 0                          # idempotent
