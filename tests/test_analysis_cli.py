"""End-to-end: the analyze command, manual overrides, the review file and the summary."""
import csv
import hashlib

import pytest

from analysis_helpers import FakeClient, cfg_for, good_tag, lib, response, write_input
from wbscraper.analysis.review import COLUMNS, STALE, post_key
from wbscraper.analysis.run import analyze, read_input
from wbscraper.analysis.summary import build_summary, source_name
from wbscraper.cli import main

NEG = "I tried to file my stormwater annual report in SMARTS and the upload doesn't work. It's a nightmare."
POS = "Geotracker is an amazing site for contamination and cleanup cases, really easy to use."
NEU = "Check GeoTracker for cleanup cases near your house, there may be spills."
ROWS = [(NEG, "2026-01-02", "https://www.reddit.com/r/Stormwater/comments/a/b/"),
        (POS, "2026-02-03", "https://www.reddit.com/r/oakland/comments/c/d/"),
        (NEU, "2026-03-04", "https://github.com/x/y/issues/1")]


def setup(tmp_path):
    cfg = cfg_for(tmp_path)
    inp = write_input(tmp_path / "data" / "output" / "waterboards_feedback.csv", ROWS)
    return cfg, inp


def read_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_scored_csv_keeps_the_first_three_columns_and_never_touches_the_input(tmp_path):
    cfg, inp = setup(tmp_path)
    before = hashlib.sha256(inp.read_bytes()).hexdigest()
    res = analyze(cfg, lib())
    assert hashlib.sha256(inp.read_bytes()).hexdigest() == before
    out = read_csv(res.paths["scored"])
    assert list(out[0])[:7] == ["text", "date", "source_url", "sentiment_score", "sentiment_label", "task", "pain_point"]
    assert list(out[0])[7:] == ["vader_label", "label_source", "tag_source", "confidence", "explanation", "needs_review"]
    assert [(r["text"], r["date"], r["source_url"]) for r in out] == ROWS
    assert [r["sentiment_label"] for r in out] == ["negative", "positive", "neutral"]
    assert all(r["task"] == "" and r["pain_point"] == "" for r in out)          # no tagging without --tag


def test_dry_run_makes_no_calls_and_writes_nothing(tmp_path):
    cfg, _ = setup(tmp_path)
    res = analyze(cfg, lib(), dry_run=True, client=FakeClient())
    assert res.tagging["dry_run"] and res.tagging["to_tag"] == 3 and not res.paths
    assert not (tmp_path / "data" / "output" / "waterboards_feedback_scored.csv").exists()


def test_tagging_fills_task_for_all_rows_and_pain_point_only_for_negative_ones(tmp_path):
    cfg, _ = setup(tmp_path)
    client = FakeClient(lambda kw: response(good_tag()))
    res = analyze(cfg, lib(), tag=True, client=client)
    out = read_csv(res.paths["scored"])
    assert all(r["task"] == "Look up data, reports or records" for r in out)
    assert [r["pain_point"] for r in out] == ["Online systems (e.g., SMARTS)", "", ""]
    assert len(client.calls) == 3 and res.tagging["calls"] == 3
    again = analyze(cfg, lib(), tag=True, client=client)                          # cached: no new calls
    assert len(client.calls) == 3 and again.tagging["calls"] == 0


def test_plain_analyze_reuses_cached_tags_without_a_client(tmp_path):
    cfg, _ = setup(tmp_path)
    analyze(cfg, lib(), tag=True, client=FakeClient())
    out = read_csv(analyze(cfg, lib()).paths["scored"])
    assert out[0]["task"] == "Look up data, reports or records"


def test_tag_without_a_key_still_writes_sentiment_and_warns(tmp_path, monkeypatch):
    cfg, _ = setup(tmp_path)
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    res = analyze(cfg, lib(), tag=True)
    assert any("Tagging skipped" in w for w in res.warnings)
    assert read_csv(res.paths["scored"])[0]["sentiment_label"] == "negative"


def test_review_file_flags_rows_and_overrides_are_applied_and_kept(tmp_path):
    cfg, _ = setup(tmp_path)
    res = analyze(cfg, lib())
    rev = res.paths["review"]
    rows = read_csv(rev)
    assert list(rows[0]) == COLUMNS and len(rows) == 3
    # user turns the neutral GitHub row negative and fixes its task
    for r in rows:
        if "github.com" in r["source_url"]:
            r["label_override"], r["task_override"], r["note"] = "Negative", "find or whatever", "checked by hand"
            r["task_override"] = "Find or pay a fee"
    with open(rev, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    res2 = analyze(cfg, lib())
    out = {r["source_url"]: r for r in read_csv(res2.paths["scored"])}
    gh = out["https://github.com/x/y/issues/1"]
    assert gh["sentiment_label"] == "negative" and gh["task"] == "Find or pay a fee"
    assert res2.reviewed == 1 and res2.changed == 1 and res2.counts["negative"] == 2
    kept = [r for r in read_csv(rev) if "github.com" in r["source_url"]][0]
    assert kept["label_override"] == "negative" and kept["note"] == "checked by hand"
    assert rev.with_suffix(".csv.bak").exists()
    assert "1 final labels differ from VADER" in res2.paths["summary"].read_text()


def test_invalid_override_values_are_ignored_with_a_warning(tmp_path):
    cfg, _ = setup(tmp_path)
    rev = analyze(cfg, lib()).paths["review"]
    rows = read_csv(rev)
    rows[0]["label_override"] = "angry"
    rows[0]["task_override"] = "Make coffee"
    with open(rev, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    res = analyze(cfg, lib())
    assert len([w for w in res.warnings if "ignored" in w]) == 2 and res.changed == 0


def test_override_for_a_post_that_no_longer_exists_is_kept_but_not_applied(tmp_path):
    cfg, inp = setup(tmp_path)
    rev = analyze(cfg, lib()).paths["review"]
    rows = read_csv(rev)
    rows[0]["label_override"] = "positive"
    with open(rev, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    write_input(inp, ROWS[1:])                                    # the overridden post disappears
    res = analyze(cfg, lib())
    assert res.changed == 0 and any("no longer in the input" in w for w in res.warnings)
    assert any(r["review_reasons"] == STALE for r in read_csv(rev))


def test_hand_label_disagreement_is_flagged(tmp_path):
    cfg, _ = setup(tmp_path)
    gold = tmp_path / "data" / "analysis" / "gold_relevant.csv"
    gold.parent.mkdir(parents=True, exist_ok=True)
    with open(gold, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "text_sha1", "label"])
        w.writerow(["r0", hashlib.sha1(POS.encode()).hexdigest()[:12], "neutral"])
    rows = read_csv(analyze(cfg, lib()).paths["review"])
    flagged = [r for r in rows if "hand label is neutral, scorer says positive" in r["review_reasons"]]
    assert len(flagged) == 1 and rows[0]["review_reasons"]       # flagged rows come first


def test_summary_frames_shares_as_collected_posts_and_flags_small_groups(tmp_path):
    cfg, _ = setup(tmp_path)
    text = analyze(cfg, lib()).paths["summary"].read_text()
    plain = text.replace("**", "").replace("_", "")
    assert "not a survey" in plain and "Share of collected posts" in plain or "collected posts" in plain
    assert "By source" in text and "n<10" in text
    assert "Reddit" in text and "GitHub" in text
    assert "were not produced" in plain


def test_summary_with_tags_has_task_pain_point_and_cross_table(tmp_path):
    cfg, _ = setup(tmp_path)
    text = analyze(cfg, lib(), tag=True, client=FakeClient()).paths["summary"].read_text()
    assert "What people were trying to do (all posts)" in text and "posts with a genuine complaint, n=1" in text
    assert "Task × pain point" in text


def test_source_names():
    assert source_name("https://www.reddit.com/r/x") == "Reddit"
    assert source_name("https://bsky.app/profile/x") == "Bluesky"
    assert source_name("https://news.ycombinator.com/item?id=1") == "Hacker News"
    assert source_name("https://water.waterboards.ca.gov/a.pdf") == "Water Boards documents"
    assert source_name("https://example.org/a") == "Other websites"


def test_empty_input_summary_does_not_crash():
    assert "No posts" in build_summary([])


def test_missing_columns_are_reported(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text('"text","date"\n"a","b"\n')
    with pytest.raises(ValueError, match="source_url"):
        read_input(p)


def test_post_key_depends_on_url_and_text():
    assert post_key("u", "t") != post_key("u", "t2") != post_key("u2", "t")


def test_cli_analyze_runs_end_to_end(tmp_path, capsys):
    _, inp = setup(tmp_path)
    rc = main(["analyze", "--data-dir", str(tmp_path / "data"), "--config-dir", str(cfg_for(tmp_path).config_dir)])
    assert rc == 0
    assert (tmp_path / "data" / "output" / "waterboards_feedback_scored.csv").exists()
    assert (tmp_path / "data" / "analysis" / "review.csv").exists()


def test_cli_analyze_reports_a_missing_input(tmp_path):
    rc = main(["analyze", "--data-dir", str(tmp_path / "empty"), "--config-dir", str(cfg_for(tmp_path).config_dir)])
    assert rc == 2
