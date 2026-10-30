import json

from conftest import REPO_CONFIG
from helpers import make_settings
from wbscraper import queries
from wbscraper.cli import main
from wbscraper.terms import TermLibrary


def lib():
    return TermLibrary.load(REPO_CONFIG)


def test_pullpush_gets_only_single_tokens():
    toks = queries.pullpush_tokens(lib())
    assert "CIWQS" in toks and "GeoTracker" in toks and all(" " not in t for t in toks)


def test_anchors_are_unique_and_tier_ordered():
    a = queries.anchors(lib(), 3)
    assert len({x.text.lower() for x in a}) == len(a)
    assert [x.tier for x in a] == sorted(x.tier for x in a)


def test_compaction_removes_subsumed_phrases_but_keeps_ambiguous_acronyms():
    texts = {a.text for a in queries.compact(lib(), queries.anchors(lib(), 3))}
    assert "GeoTracker" in texts and "GeoTracker upload" not in texts       # covered by the broader term
    assert "SMARTS" in texts and "SMARTS NOI" in texts                      # SMARTS alone is too noisy to subsume
    assert "smarts.waterboards.ca.gov" in texts                             # domains are always kept


def test_web_queries_cover_questions_and_modifiers():
    qs = [q for _, q in queries.web_queries(lib(), "site:reddit.com ")]
    assert all(q.startswith("site:reddit.com ") for q in qs)
    assert any("is the water board website down" in q for q in qs)
    assert any('"CIWQS" login problem' in q for q in qs)
    assert len(qs) == len(set(qs))


def test_plan_has_all_families_and_priorities_favour_best_tiers(tmp_path):
    tasks = queries.plan_reddit_tasks(lib(), make_settings(tmp_path, reddit={"url_prefixes": ["https://x.test"]}))
    fams = {t[3] for t in tasks}
    assert fams == {"reddit_urls", "reddit_pullpush", "reddit_arcticshift", "reddit_discovery"}
    top = max(t[2] for t in tasks if t[3] == "reddit_arcticshift")
    low = min(t[2] for t in tasks if t[3] == "reddit_arcticshift")
    assert top > low


def test_term_files_can_be_extended_without_code_changes(tmp_path):
    import shutil
    cfg = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, cfg)
    ent = cfg / "terms" / "entities.yaml"
    ent.write_text(ent.read_text() + """
  - id: newsys
    label: Brand New Portal
    kind: system
    short: the new portal
    strong: [BrandNewPortal]
    anchors: {1: [BrandNewPortal]}
    pullpush: [BrandNewPortal]
""", encoding="utf-8")
    new = TermLibrary.load(cfg)
    assert "BrandNewPortal" in queries.pullpush_tokens(new)
    from wbscraper.relevance import RelevanceScorer
    assert RelevanceScorer(new).score("BrandNewPortal login error, can't submit").tier == "relevant"


def test_cli_offline_commands(tmp_path, capsys):
    data = str(tmp_path / "d")
    assert main(["terms", "--data-dir", data, "--sample", "2"]) == 0
    assert main(["sources", "--data-dir", data]) == 0
    assert main(["status", "--data-dir", data]) == 0
    assert main(["export", "--data-dir", data]) == 0
    out = capsys.readouterr().out
    assert "Planned tasks by family" in out and "Wrote 0 rows" in out
    csv_text = (tmp_path / "d" / "output" / "waterboards_feedback.csv").read_text(encoding="utf-8")
    assert csv_text.strip() == '"text","date","source_url"'
    manifest = json.loads((tmp_path / "d" / "output" / "run_manifest.json").read_text())
    assert manifest["csv"]["columns"] == ["text", "date", "source_url"]


def test_cli_dump_writes_every_planned_query(tmp_path):
    out = tmp_path / "plan.tsv"
    assert main(["terms", "--data-dir", str(tmp_path / "d"), "--dump", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("priority\t") and len(lines) > 1000


def test_year_windows_are_calendar_aligned_stable_and_open_ended():
    from wbscraper.sources.reddit_tasks import FAR_FUTURE
    since = 1117584000                                           # 2005-06-01
    now = 1_790_000_000                                          # Sep 2026
    w = queries.year_windows(since, FAR_FUTURE, 3, now=now)
    assert w[0][0] <= since and w[-1][1] == FAR_FUTURE
    assert all(b <= a2 + 2 for (_, b), (a2, _) in zip(w, w[1:]))   # contiguous (1 s overlap at most)
    assert queries.year_windows(since, FAR_FUTURE, 3, now=now + 86400) == w   # stable within the same period
    assert queries.year_windows(since, FAR_FUTURE, 0, now=now) == [(since, FAR_FUTURE)]
    recent = queries.year_windows(1_700_000_000, FAR_FUTURE, 3, now=now)       # short history -> a single window
    assert len(recent) == 1 and recent[0][1] == FAR_FUTURE


def test_arctic_shift_budget_comments_are_few_and_windowed_posts_are_wide(tmp_path):
    from collections import Counter
    from wbscraper.sources.reddit_tasks import FAR_FUTURE
    settings = make_settings(tmp_path)
    tasks = [t for t in queries.plan_reddit_tasks(lib(), settings) if t[3] == "reddit_arcticshift"]
    comments = [t for t in tasks if t[1]["kind"] == "comments"]
    posts = [t for t in tasks if t[1]["kind"] == "posts"]
    tier1 = set(lib().subreddits["tier1"])
    assert comments and all(t[1]["subreddit"] in tier1 for t in comments)         # comment text search only for tier 1
    per_pair = Counter((t[1]["subreddit"], t[1]["q"]) for t in comments)
    assert max(per_pair.values()) > 1                                              # pre-split into several windows
    assert all(t[1]["before"] - t[1]["after"] < 3 * 366 * 86400 + 10 or t[1]["before"] == FAR_FUTURE for t in comments)
    assert all(t[1]["before"] == FAR_FUTURE and t[1]["after"] == settings.since_ts for t in posts)
    newest_first = [t for t in comments if t[1]["subreddit"] == "Wastewater" and t[1]["q"] == comments[0][1]["q"]]
    assert [t[2] for t in newest_first] == sorted((t[2] for t in newest_first), reverse=True)


def test_settings_lookup_handles_yaml_integer_keys(tmp_path):
    s = make_settings(tmp_path)
    assert s.get("reddit.anchors_per_subreddit.comments.1") == 8 and s.get("reddit.anchors_per_subreddit.posts.3") == 6
