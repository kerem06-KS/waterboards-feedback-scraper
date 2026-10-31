"""AI-assisted review file: precedence, persistence, validation, and VADER values kept alongside."""
import csv
import hashlib

from analysis_helpers import FakeClient, cfg_for, lib, write_input
from wbscraper.analysis.review import COLUMNS, post_key
from wbscraper.analysis.reviewed import COLUMNS as AI_COLUMNS, read_ai_reviews, write_ai_reviews
from wbscraper.analysis.run import analyze

NEG = "I tried to file my stormwater annual report in SMARTS and the upload doesn't work. It's a nightmare."
NEU = "Check GeoTracker for cleanup cases near your house, there may be spills."
URLS = ["https://www.reddit.com/r/Stormwater/comments/a/b/", "https://github.com/x/y/issues/1"]
ROWS = [(NEG, "2026-01-02", URLS[0]), (NEU, "2026-03-04", URLS[1])]
TASK_A, TASK_B = "Submit reports or data", "Check a property or area for contamination"
PAIN = "Online systems (e.g., SMARTS)"


def setup(tmp_path):
    cfg = cfg_for(tmp_path)
    inp = write_input(tmp_path / "data" / "output" / "waterboards_feedback.csv", ROWS)
    return cfg, inp


def ai_row(text, url, **kw):
    r = {"key": post_key(url, text), "source_url": url, "ai_label": "neutral", "ai_task": TASK_B, "ai_pain_point": "",
         "ai_confidence": "high", "ai_uncertain": "", "ai_explanation": "because", "reviewed_by": "test", "reviewed_on": "2026-10-31"}
    r.update(kw)
    return r


def read(p):
    with open(p, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_without_a_review_file_nothing_changes_and_sources_say_so(tmp_path):
    cfg, _ = setup(tmp_path)
    out = read(analyze(cfg, lib()).paths["scored"])
    assert [r["label_source"] for r in out] == ["vader", "vader"] and [r["tag_source"] for r in out] == ["none", "none"]
    assert [r["sentiment_label"] for r in out] == [r["vader_label"] for r in out]


def test_ai_review_sets_final_columns_but_keeps_the_vader_score_and_label(tmp_path):
    cfg, inp = setup(tmp_path)
    before = analyze(cfg, lib()).paths["scored"].read_text()
    vader = {r["source_url"]: (r["sentiment_score"], r["vader_label"]) for r in read(cfg.path("scored"))}
    write_ai_reviews(cfg.path("ai_review"), [
        ai_row(NEG, URLS[0], ai_label="negative", ai_task=TASK_A, ai_pain_point=PAIN, ai_explanation="Upload fails."),
        ai_row(NEU, URLS[1], ai_label="positive", ai_task=TASK_B, ai_confidence="low", ai_uncertain="yes")])
    res = analyze(cfg, lib())
    out = {r["source_url"]: r for r in read(res.paths["scored"])}
    a, b = out[URLS[0]], out[URLS[1]]
    assert (a["sentiment_label"], a["task"], a["pain_point"], a["label_source"], a["tag_source"]) == ("negative", TASK_A, PAIN, "ai_review", "ai_review")
    assert a["confidence"] == "high" and a["explanation"] == "Upload fails." and a["needs_review"] == ""
    assert b["sentiment_label"] == "positive" and b["pain_point"] == "" and b["needs_review"] == "yes"
    for url, row in out.items():                                    # original VADER values survive in their own columns
        assert (row["sentiment_score"], row["vader_label"]) == vader[url]
    assert res.ai_reviewed == 2 and "ai review: 2" in res.paths["summary"].read_text()
    assert before != res.paths["scored"].read_text()


def test_pain_point_is_filled_wherever_a_genuine_complaint_exists_even_if_not_negative(tmp_path):
    cfg, _ = setup(tmp_path)
    write_ai_reviews(cfg.path("ai_review"), [ai_row(NEU, URLS[1], ai_label="neutral", ai_pain_point="Can't find information")])
    row = [r for r in read(analyze(cfg, lib()).paths["scored"]) if r["source_url"] == URLS[1]][0]
    assert row["sentiment_label"] == "neutral" and row["pain_point"] == "Can't find information"


def test_human_override_beats_ai_review_and_survives_reruns(tmp_path):
    cfg, _ = setup(tmp_path)
    write_ai_reviews(cfg.path("ai_review"), [ai_row(NEG, URLS[0], ai_label="negative", ai_task=TASK_A, ai_pain_point=PAIN)])
    rev = analyze(cfg, lib()).paths["review"]
    rows = read(rev)
    assert "ai_explanation" in rows[0] and "final_label" in rows[0]
    for r in rows:
        if r["source_url"] == URLS[0]:
            r["label_override"], r["pain_point_override"], r["note"] = "neutral", "Reaching staff", "checked by hand"
    with open(rev, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    for _ in range(2):                                              # twice: nothing gets lost on a second run
        res = analyze(cfg, lib())
        row = [r for r in read(res.paths["scored"]) if r["source_url"] == URLS[0]][0]
        assert (row["sentiment_label"], row["label_source"], row["pain_point"], row["task"]) == ("neutral", "human_override", "Reaching staff", TASK_A)
        assert row["needs_review"] == ""
        kept = [r for r in read(rev) if r["source_url"] == URLS[0]][0]
        assert kept["label_override"] == "neutral" and kept["note"] == "checked by hand" and kept["ai_label"] == "negative"


def test_analyze_never_rewrites_or_loses_the_ai_review_file(tmp_path):
    cfg, _ = setup(tmp_path)
    write_ai_reviews(cfg.path("ai_review"), [ai_row(NEG, URLS[0], ai_label="negative")])
    p = cfg.path("ai_review")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    analyze(cfg, lib())
    analyze(cfg, lib(), tag=True, client=FakeClient())               # even a tagging run leaves it alone
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest


def test_ai_review_beats_the_api_tag_for_task_and_pain_point(tmp_path):
    cfg, _ = setup(tmp_path)
    write_ai_reviews(cfg.path("ai_review"), [ai_row(NEG, URLS[0], ai_label="negative", ai_task=TASK_A, ai_pain_point=PAIN)])
    out = read(analyze(cfg, lib(), tag=True, client=FakeClient()).paths["scored"])
    a = [r for r in out if r["source_url"] == URLS[0]][0]
    b = [r for r in out if r["source_url"] == URLS[1]][0]
    assert a["task"] == TASK_A and a["tag_source"] == "ai_review"
    assert b["tag_source"] == "api_tag" and b["label_source"] == "vader"       # posts without a review still get the API tag


def test_invalid_rows_and_stale_rows_are_reported_not_applied(tmp_path):
    cfg, inp = setup(tmp_path)
    rows = [ai_row(NEG, URLS[0], ai_label="furious"), ai_row(NEU, URLS[1], ai_task="Make coffee"),
            ai_row("a post that is gone", "https://example.org/x")]
    write_ai_reviews(cfg.path("ai_review"), rows)
    keys = {post_key(u, t) for t, _, u in ROWS}
    rep = read_ai_reviews(cfg.path("ai_review"), cfg, keys)
    assert rep.reviews == {} and len(rep.warnings) == 3
    assert any("not one of" in w for w in rep.warnings) and any("no longer in the input" in w for w in rep.warnings)
    out = read(analyze(cfg, lib()).paths["scored"])
    assert [r["label_source"] for r in out] == ["vader", "vader"]


def test_file_columns_are_stable():
    assert AI_COLUMNS[:3] == ["key", "source_url", "ai_label"] and "ai_explanation" in AI_COLUMNS
