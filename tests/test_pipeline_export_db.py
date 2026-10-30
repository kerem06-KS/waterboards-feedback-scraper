import csv
import sqlite3

from conftest import REPO_CONFIG
from helpers import FakeClock, make_app
from wbscraper.db import Database
from wbscraper.export import HEADER, export_csv
from wbscraper.sources.base import RawItem


def raw(uid, text, kind="post", thread="reddit:t3_a1", date="2024-05-01T10:00:00Z", url=None):
    return RawItem(uid=uid, source="reddit", kind=kind, url=url or f"https://www.reddit.com/{uid}", text=text,
                   date=date, thread_uid=thread, community="Wastewater")


def test_task_queue_is_idempotent_and_ordered(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    assert db.add_task("x", {"q": 1}, 5)
    assert not db.add_task("x", {"q": 1}, 5)           # same payload -> same task
    db.add_task("x", {"q": 2}, 9)
    first = db.ready_tasks(0)[0]
    assert first["priority"] == 9
    db.mark_in_progress(first["id"])
    assert db.reset_in_progress() == 1                  # crash recovery
    assert db.pending_count() == 2


def test_pipeline_stores_redacts_and_dedupes(tmp_path):
    app, _ = make_app(tmp_path)
    p = app.pipeline
    r = p.ingest(raw("reddit:t3_a1", "CIWQS login is broken, email me at bob@example.com or call 916-555-1234"), "pullpush", "search_posts")
    assert r.status == "stored" and r.tier == "relevant"
    row = app.db.item_row("reddit:t3_a1")
    assert "bob@example.com" not in row["text"] and "555-1234" not in row["text"]
    assert p.ingest(raw("reddit:t3_a1", "CIWQS login is broken"), "arcticshift", "x").status == "duplicate"
    methods = {(m["provider"]) for m in app.db.conn.execute("SELECT provider FROM item_methods WHERE uid='reddit:t3_a1'")}
    assert methods == {"pullpush", "arcticshift"}


def test_long_cross_posts_are_deduped_but_short_replies_are_not(tmp_path):
    app, _ = make_app(tmp_path)
    long = "CIWQS website login is broken and the upload keeps failing every single time I try to submit " * 2
    assert app.pipeline.ingest(raw("reddit:t3_a", long, thread="reddit:t3_a"), "p", "m").status == "stored"
    assert app.pipeline.ingest(raw("reddit:t3_b", long, thread="reddit:t3_b"), "p", "m").status == "duplicate"
    short = "CIWQS login broken for me too"
    assert app.pipeline.ingest(raw("reddit:t1_c", short, "comment", "reddit:t3_x"), "p", "m").status == "stored"
    assert app.pipeline.ingest(raw("reddit:t1_d", short, "comment", "reddit:t3_y"), "p", "m").status == "stored"


def test_skips_deleted_bots_and_irrelevant_noise(tmp_path):
    app, _ = make_app(tmp_path)
    p = app.pipeline
    assert p.ingest(raw("reddit:t1_1", "[removed]", "comment"), "p", "m").status == "skipped"
    assert p.ingest(raw("reddit:t1_2", "I am a bot, and this action was performed automatically.", "comment"), "p", "m").status == "skipped"
    assert p.ingest(raw("reddit:t1_3", "what a lovely day for a walk in the park", "comment"), "p", "m").status == "below_threshold"
    assert app.db.item_row("reddit:t1_3") is None


def test_thread_context_changes_how_comments_are_scored(tmp_path):
    app, _ = make_app(tmp_path)
    app.pipeline.ingest(raw("reddit:t3_t", "SMARTS stormwater annual report website is down", thread="reddit:t3_t"), "p", "m")
    assert app.db.thread_relevant("reddit:t3_t")
    r = app.pipeline.ingest(raw("reddit:t1_z", "the upload kept timing out for me too", "comment", "reddit:t3_t"), "p", "m")
    assert r.status == "stored" and r.tier == "relevant" and not r.own_relevant


def test_export_has_exactly_three_columns_and_preserves_text(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    texts = {
        "a": 'Multi-line\n"quoted", comma, émoji 😀 and\ttab',
        "b": "second item without date",
    }
    for uid, t in texts.items():
        db.insert_item(uid=uid, source="reddit", kind="post", url=f"https://x.test/{uid}", thread_uid=None,
                       parent_uid=None, community=None, text=t, date="2024-01-02T03:04:05Z" if uid == "a" else None,
                       tier="relevant", own_relevant=1, score=5, entity_hit=1, reasons="[]", text_hash=None, flags=None)
    db.insert_item(uid="c", source="reddit", kind="post", url="https://x.test/c", thread_uid=None, parent_uid=None,
                   community=None, text="borderline only", date=None, tier="borderline", own_relevant=0, score=3,
                   entity_hit=1, reasons="[]", text_hash=None, flags=None)
    db.commit()
    out = tmp_path / "out" / "f.csv"
    res = export_csv(db, out)
    assert res.rows == 2
    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == HEADER == ["text", "date", "source_url"]
    assert all(len(r) == 3 for r in rows)
    by_url = {r[2]: r for r in rows[1:]}
    assert by_url["https://x.test/a"][0] == texts["a"] and by_url["https://x.test/a"][1] == "2024-01-02T03:04:05Z"
    assert by_url["https://x.test/b"][1] == ""                      # unknown dates stay empty
    assert export_csv(db, out, include_borderline=True).rows == 3
    assert export_csv(db, out, excel=True).path.read_bytes().startswith(b"\xef\xbb\xbf")
