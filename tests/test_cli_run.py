"""`wbscraper run` wired end to end against mocked HTTP and a 3-task plan (nothing real is contacted)."""
import csv
import shutil

import httpx
import respx
import yaml

from conftest import REPO_CONFIG
from wbscraper import queries
from wbscraper.cli import main
from wbscraper.sources.reddit_tasks import FAR_FUTURE


def test_run_command_collects_exports_and_summarises(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, cfg)
    settings = yaml.safe_load((cfg / "settings.yaml").read_text())
    for dom in settings["domains"]:
        settings["domains"][dom] = {"interval": 0.0, "jitter": 0.0}
    settings["reddit"]["page_size"] = 5
    (cfg / "settings.yaml").write_text(yaml.safe_dump(settings))

    def tiny_plan(lib, settings, since=None, enabled=None):
        base = {"after": 1117584000, "before": FAR_FUTURE, "dry": 0, "depth": 0}
        return [("reddit_search", {**base, "kind": "posts", "q": "CIWQS", "subreddit": None}, 630, "reddit_pullpush"),
                ("reddit_discover", {"q": 'site:reddit.com "CIWQS"'}, 580, "reddit_discovery")]

    monkeypatch.setattr(queries, "plan_reddit_tasks", tiny_plan)
    monkeypatch.setattr("wbscraper.sources.reddit_search_discovery._default_search", lambda q, n: [])

    pp = "https://api.pullpush.io/reddit/search/"
    with respx.mock:
        respx.get(pp + "submission/").mock(return_value=httpx.Response(200, json={"data": [
            {"id": "zz1", "title": "CIWQS login keeps failing", "selftext": "Contact me: a@b.com", "created_utc": 1_700_000_000,
             "subreddit": "Wastewater", "author": "should_never_be_stored"}]}))
        respx.get(pp + "comment/").mock(return_value=httpx.Response(200, json={"data": []}))
        respx.get(url__regex=r"https://arctic-shift\.photon-reddit\.com/.*").mock(
            return_value=httpx.Response(200, json={"data": []}))
        rc = main(["run", "--config-dir", str(cfg), "--data-dir", str(tmp_path / "d"), "--min-minutes", "0",
                   "--max-minutes", "5", "--no-dashboard"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Run summary" in out and "exhausted" in out
    rows = list(csv.reader(open(tmp_path / "d" / "output" / "waterboards_feedback.csv", newline="", encoding="utf-8")))
    assert rows[0] == ["text", "date", "source_url"] and len(rows) == 2
    assert "CIWQS login keeps failing" in rows[1][0]
    assert "a@b.com" not in rows[1][0] and "should_never_be_stored" not in "".join(rows[1])
    assert rows[1][1] == "2023-11-14T22:13:20Z"
    assert rows[1][2] == "https://www.reddit.com/r/Wastewater/comments/zz1/"
    assert list((tmp_path / "d" / "logs").glob("run-*.log"))          # details go to the log file, not the terminal
    assert main(["status", "--config-dir", str(cfg), "--data-dir", str(tmp_path / "d")]) == 0
    assert main(["report", "--config-dir", str(cfg), "--data-dir", str(tmp_path / "d")]) == 0
    assert (tmp_path / "d" / "output" / "coverage_report.md").exists()


def test_live_dashboard_renders_without_error(tmp_path):
    import io
    from rich.console import Console
    from helpers import make_app
    from wbscraper.progress import Dashboard, print_summary

    app, _ = make_app(tmp_path, {})
    app.stats.relevant, app.stats.restricted, app.stats.cooling = 3, {"pullpush"}, {"ddgs": 42.0}
    app.stats.last_error = "boom"
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    dash = Dashboard(app.stats, app.db, min_minutes=120, max_minutes=480, console=console, plain=False)
    dash.start()
    dash.update(force=True)
    dash.stop()
    print_summary(console, app.db, app.stats, "exhausted", 61.0, "x.csv", 3, False)
    text = buf.getvalue()
    assert "Ctrl+C" in text and "Run summary" in text and "pullpush" in text
