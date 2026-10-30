"""Stack Exchange, Hacker News and GitHub adapters against mocked HTTP."""
import gzip
import json

import httpx
import respx

from helpers import FakeClock, make_app, run_all

SE = "https://api.stackexchange.com/2.3/"
HN = "https://hn.algolia.com/api/v1/"
GH = "https://api.github.com"


def se_page(items, **extra):
    return httpx.Response(200, json={"items": items, "has_more": False, "quota_remaining": 250, **extra})


@respx.mock
def test_stackexchange_full_flow(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("se_search", {"site": "gis", "q": "GeoTracker", "page": 1}, 560, "stackexchange")
    respx.get(SE + "search/excerpts").mock(return_value=se_page([
        {"item_type": "question", "question_id": 101, "title": "x"},
        {"item_type": "answer", "question_id": 101, "answer_id": 201},
        {"item_type": "question", "question_id": 102}]))
    respx.get(url__regex=r".*/questions/101;102(\?.*)?$").mock(return_value=se_page([
        {"question_id": 101, "title": "GeoTracker EDF upload fails", "body": "<p>Our <b>consultant</b> gets a validation error "
         "when uploading an EDF to GeoTracker for the UST cleanup site.</p>", "creation_date": 1700000000,
         "link": "https://gis.stackexchange.com/questions/101/geotracker-edf-upload-fails"},
        {"question_id": 102, "title": "Garmin GeoTracker app GPS", "body": "<p>GPS track on my android phone</p>",
         "creation_date": 1700000100}]))
    respx.get(url__regex=r".*/questions/101;102/answers(\?.*)?$").mock(return_value=se_page([
        {"answer_id": 201, "question_id": 101, "body": "<p>The upload kept timing out for us too; "
         "call the help desk.</p>", "creation_date": 1700000500, "link": "https://gis.stackexchange.com/a/201"}]))
    respx.get(url__regex=r".*/questions/101;102/comments(\?.*)?$").mock(return_value=se_page([
        {"comment_id": 301, "post_id": 101, "body": "Same problem here, the website is so slow.", "creation_date": 1700000600}]))
    respx.get(url__regex=r".*/answers/201/comments(\?.*)?$").mock(return_value=se_page([
        {"comment_id": 302, "post_id": 201, "body": "The upload still times out in Chrome for me.", "creation_date": 1700000700}]))
    run_all(app, clock)
    rows = {r["uid"]: r for r in app.db.conn.execute("SELECT * FROM items")}
    assert rows["se:gis:q101"]["tier"] == "relevant"
    assert "se:gis:q102" not in rows                                     # GPS look-alike is not kept
    assert rows["se:gis:a201"]["tier"] == "relevant"                    # reply in a relevant thread
    assert rows["se:gis:c301"]["url"] == "https://gis.stackexchange.com/q/101#comment301_101"
    assert rows["se:gis:c302"]["url"] == "https://gis.stackexchange.com/q/101#comment302_201"   # comment on an answer
    assert "<b>" not in rows["se:gis:q101"]["text"] and "consultant" in rows["se:gis:q101"]["text"]
    assert rows["se:gis:q101"]["date"] == "2023-11-14T22:13:20Z"


@respx.mock
def test_stackexchange_pauses_when_quota_is_nearly_used_and_obeys_backoff(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("se_search", {"site": "gis", "q": "CIWQS", "page": 1}, 560, "stackexchange")
    app.db.add_task("se_search", {"site": "law", "q": "CIWQS", "page": 1}, 559, "stackexchange")
    route = respx.get(SE + "search/excerpts").mock(return_value=httpx.Response(
        200, json={"items": [], "has_more": False, "quota_remaining": 3, "backoff": 5}))
    run_all(app, clock, min_minutes=0, max_minutes=5)
    assert route.call_count == 1                                          # second search waits: quota cooldown
    assert app.http.cooldown_remaining("api.stackexchange.com") > 3600


@respx.mock
def test_stackexchange_throttle_error_becomes_rate_limited(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("se_search", {"site": "gis", "q": "CIWQS", "page": 1}, 560, "stackexchange")
    respx.get(SE + "search/excerpts").mock(return_value=httpx.Response(
        400, json={"error_id": 502, "error_message": "too many requests from this IP", "error_name": "throttle_violation"}))
    run_all(app, clock, min_minutes=0, max_minutes=5)
    assert app.db.task_counts().get("done", 0) == 0
    assert app.http.cooldown_remaining("api.stackexchange.com") > 0


@respx.mock
def test_hackernews_search_and_thread_expansion(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("hn_search", {"q": "CalWATRS", "page": 0, "dry": 0}, 540, "hackernews")
    respx.get(HN + "search").mock(return_value=httpx.Response(200, json={"nbPages": 1, "hits": [
        {"objectID": "5001", "_tags": ["story"], "title": "CalWATRS water rights reporting site keeps failing login",
         "story_text": None, "created_at_i": 1700000000, "url": "https://example.com/x"},
        {"objectID": "5002", "_tags": ["comment"], "comment_text": "The CalWATRS &amp; eWRIMS website is confusing",
         "story_id": 5001, "parent_id": 5001, "created_at_i": 1700000100},
        {"objectID": "5003", "_tags": ["story"], "title": "Totally unrelated gardening news", "created_at_i": 1700000200}]}))
    respx.get(HN + "items/5001").mock(return_value=httpx.Response(200, json={"id": 5001, "children": [
        {"id": 6001, "parent_id": 5001, "created_at_i": 1700000300, "text": "<p>the upload kept timing out for me too</p>",
         "children": [{"id": 6002, "parent_id": 6001, "created_at_i": 1700000400, "text": "same login problem on the website here",
                       "children": []}]}]}))
    run_all(app, clock)
    rows = {r["uid"]: r for r in app.db.conn.execute("SELECT * FROM items")}
    assert rows["hn:s5001"]["tier"] == "relevant" and rows["hn:s5001"]["url"] == "https://news.ycombinator.com/item?id=5001"
    assert "hn:s5003" not in rows
    assert rows["hn:c5002"]["text"] == "The CalWATRS & eWRIMS website is confusing"
    assert "hn:c6001" in rows and "hn:c6002" in rows                      # nested comments reached through the item tree


@respx.mock
def test_github_org_issues_skip_staff_and_use_implicit_context(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("gh_org_repos", {"org": "CAWaterBoardDataCenter", "page": 1}, 640, "github")
    respx.get(GH + "/orgs/CAWaterBoardDataCenter/repos").mock(return_value=httpx.Response(200, json=[
        {"full_name": "CAWaterBoardDataCenter/tool", "archived": False, "open_issues_count": 2}]))
    respx.get(GH + "/repos/CAWaterBoardDataCenter/tool/issues").mock(return_value=httpx.Response(200, json=[
        {"number": 7, "title": "Dashboard map does not load", "body": "The website map is blank and the page never loads for me.",
         "created_at": "2024-02-01T10:00:00Z", "html_url": "https://github.com/CAWaterBoardDataCenter/tool/issues/7",
         "author_association": "NONE"},
        {"number": 8, "title": "Refactor map module", "body": "internal website refactor, page loading",
         "created_at": "2024-02-02T10:00:00Z", "html_url": "https://github.com/CAWaterBoardDataCenter/tool/issues/8",
         "author_association": "MEMBER"}]))
    respx.get(GH + "/repos/CAWaterBoardDataCenter/tool/issues/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 90, "body": "Same here, the dashboard login is broken.", "created_at": "2024-02-03T10:00:00Z",
         "html_url": "https://github.com/CAWaterBoardDataCenter/tool/issues/7#issuecomment-90",
         "issue_url": "https://api.github.com/repos/CAWaterBoardDataCenter/tool/issues/7", "author_association": "NONE"}]))
    run_all(app, clock)
    uids = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert uids["gh:CAWaterBoardDataCenter/tool#7"] == "borderline"        # repository context alone is never proof
    assert "gh:CAWaterBoardDataCenter/tool#8" not in uids                  # staff-authored: not public feedback
    assert uids["gh:CAWaterBoardDataCenter/tool#c90"] in ("borderline", "irrelevant")


@respx.mock
def test_github_rate_limit_sets_a_cooldown_instead_of_hammering(tmp_path):
    clock = FakeClock()
    app, _ = make_app(tmp_path, {}, clock=clock)
    app.db.add_task("gh_search", {"q": "CIWQS", "page": 1}, 520, "github")
    reset = int(clock.now) + 3000
    respx.get(GH + "/search/issues").mock(return_value=httpx.Response(
        403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}))
    run_all(app, clock, min_minutes=0, max_minutes=2)
    assert app.http.cooldown_remaining("api.github.com") > 1000
    assert app.db.task_counts().get("done", 0) == 0


@respx.mock
def test_github_global_search_fetches_comments_for_relevant_issues(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("gh_search", {"q": "CIWQS", "page": 1}, 520, "github")
    respx.get(GH + "/search/issues").mock(return_value=httpx.Response(200, json={"items": [
        {"number": 3, "title": "CIWQS login broken", "body": "eSMR upload fails on the website", "comments": 1,
         "created_at": "2024-03-01T00:00:00Z", "html_url": "https://github.com/acme/tool/issues/3",
         "comments_url": "https://api.github.com/repos/acme/tool/issues/3/comments", "author_association": "NONE"}]}))
    respx.get("https://api.github.com/repos/acme/tool/issues/3/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 5, "body": "the upload kept timing out for me too", "created_at": "2024-03-02T00:00:00Z",
         "html_url": "https://github.com/acme/tool/issues/3#issuecomment-5",
         "issue_url": "https://api.github.com/repos/acme/tool/issues/3"}]))
    run_all(app, clock)
    tiers = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert tiers["gh:acme/tool#3"] == "relevant" and tiers["gh:acme/tool#c5"] == "relevant"
