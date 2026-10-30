"""Bluesky, Lemmy, Mastodon and YouTube adapters (mocked HTTP only)."""
import httpx
import respx

from helpers import T0, FakeClock, make_app, run_all
from wbscraper import queries
from wbscraper.cli import main

BSKY = "https://api.bsky.app/xrpc/"


def bpost(rkey, text, created, did="did:plc:abc123", replies=0, reply=None):
    rec = {"text": text, "createdAt": created}
    if reply:
        rec["reply"] = reply
    return {"uri": f"at://{did}/app.bsky.feed.post/{rkey}", "cid": "c", "record": rec, "replyCount": replies,
            "author": {"did": did, "handle": "someone.bsky.social"}}


@respx.mock
def test_bluesky_window_splitting_collects_beyond_one_page_without_login(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("bsky_search", {"q": "CIWQS", "since": 1672531200, "until": None, "depth": 0}, 510, "bluesky")
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        assert "cursor" not in params                       # never uses the cursor (anonymous paging is refused)
        if len(calls) == 1:                                  # first window returns a full page -> must be split
            posts = [bpost(f"k{i}", f"CIWQS website login fails again #{i}", "2024-02-01T00:00:00Z") for i in range(100)]
        else:
            posts = [bpost(f"w{len(calls)}", "CIWQS upload keeps failing on the portal", "2024-03-01T00:00:00Z")]
        return httpx.Response(200, json={"posts": posts})

    respx.get(BSKY + "app.bsky.feed.searchPosts").mock(side_effect=handler)
    run_all(app, clock, max_minutes=60)
    assert len(calls) >= 3 and "since" in calls[0] and "until" in calls[0]
    n = app.db.conn.execute("SELECT COUNT(*) FROM items WHERE source='bluesky'").fetchone()[0]
    assert n >= 100
    url = app.db.conn.execute("SELECT url FROM items WHERE uid='bsky:did:plc:abc123/k1'").fetchone()[0]
    assert url == "https://bsky.app/profile/did:plc:abc123/post/k1"      # DID based: no handle in stored data


@respx.mock
def test_bluesky_thread_expansion_and_auth_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("BSKY_HANDLE", "me.bsky.social")
    monkeypatch.setenv("BSKY_APP_PASSWORD", "app-pass")
    app, clock = make_app(tmp_path, {})
    app.db.add_task("bsky_search", {"q": "CalWATRS", "since": 1672531200, "until": None, "depth": 0}, 510, "bluesky")
    anon = respx.get(BSKY + "app.bsky.feed.searchPosts").mock(return_value=httpx.Response(403, text="forbidden"))
    respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(200, json={"accessJwt": "jwt-token"}))
    root = bpost("root1", "CalWATRS PIN login is broken and the website is confusing", "2025-01-10T00:00:00Z", replies=1)
    authed = respx.get("https://bsky.social/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json={"posts": [root]}))
    respx.get(BSKY + "app.bsky.feed.getPostThread").mock(return_value=httpx.Response(200, json={"thread": {
        "post": root, "replies": [{"post": bpost("r1", "same login problem, the upload fails", "2025-01-11T00:00:00Z",
                                                reply={"root": {"uri": root["uri"]}, "parent": {"uri": root["uri"]}}),
                                   "replies": []}, {"$type": "app.bsky.feed.defs#blockedPost"}]}}))
    run_all(app, clock)
    assert anon.called and authed.calls[0].request.headers["authorization"] == "Bearer jwt-token"
    uids = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert uids["bsky:did:plc:abc123/root1"] == "relevant" and uids["bsky:did:plc:abc123/r1"] == "relevant"


@respx.mock
def test_lemmy_search_posts_comments_and_thread(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("lemmy_search", {"instance": "lemmy.world", "q": "GeoTracker", "page": 1, "dry": 0}, 505, "lemmy")
    respx.get("https://lemmy.world/api/v3/search").mock(return_value=httpx.Response(200, json={
        "posts": [{"post": {"id": 11, "name": "GeoTracker EDF upload error", "body": "UST cleanup consultant here: the website "
                            "rejects our upload.", "ap_id": "https://lemmy.world/post/11", "published": "2024-06-01T10:00:00.000Z"}},
                  {"post": {"id": 12, "name": "GeoTracker for Houdini", "body": "GPS plugin", "ap_id": "https://lemmy.world/post/12",
                            "published": "2024-06-02T10:00:00Z"}}],
        "comments": [], "communities": [], "users": []}))
    respx.get("https://lemmy.world/api/v3/comment/list").mock(return_value=httpx.Response(200, json={"comments": [
        {"comment": {"id": 21, "content": "the upload kept timing out for me too", "ap_id": "https://lemmy.world/comment/21",
                     "published": "2024-06-03T10:00:00Z"}, "post": {"id": 11, "ap_id": "https://lemmy.world/post/11"}}]}))
    run_all(app, clock)
    rows = {r["url"]: r["tier"] for r in app.db.conn.execute("SELECT url, tier FROM items")}
    assert rows["https://lemmy.world/post/11"] == "relevant" and rows["https://lemmy.world/comment/21"] == "relevant"
    assert "https://lemmy.world/post/12" not in rows


@respx.mock
def test_lemmy_instance_that_refuses_anonymous_search_is_skipped_politely(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("lemmy_search", {"instance": "closed.example", "q": "CIWQS", "page": 1, "dry": 0}, 505, "lemmy")
    respx.get("https://closed.example/api/v3/search").mock(return_value=httpx.Response(401, json={"error": "not_logged_in"}))
    result = run_all(app, clock)
    assert result.stop_reason == "exhausted" and app.db.counters()["lemmy_instance_refused"] == 1


@respx.mock
def test_mastodon_tag_timeline_pages_then_stops_when_nothing_relevant(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("masto_tag", {"instance": "mastodon.social", "tag": "stormwater", "max_id": None, "dry": 0, "page": 0}, 500, "mastodon")

    def status(i, html, reblog=None, replies=0):
        return {"id": str(1000 - i), "content": html, "created_at": "2024-07-01T00:00:00.000Z",
                "url": f"https://mastodon.social/@someone/{1000 - i}", "in_reply_to_id": None, "reblog": reblog,
                "replies_count": replies, "spoiler_text": ""}

    seen_max_ids = []

    def handler(request):
        seen_max_ids.append(request.url.params.get("max_id"))
        if len(seen_max_ids) == 1:
            data = [status(0, "<p>SMARTS stormwater annual report: the website login keeps failing</p>", replies=2)] + \
                   [status(i, "<p>lovely weather for a stormwater drain walk</p>") for i in range(1, 39)] + \
                   [status(39, "<p>boost</p>", reblog={"id": "9"})]
        else:
            data = [status(40 + i + 40 * len(seen_max_ids), "<p>nothing about our topic here at all</p>") for i in range(40)]
        return httpx.Response(200, json=data)

    respx.get("https://mastodon.social/api/v1/timelines/tag/stormwater").mock(side_effect=handler)
    respx.get("https://mastodon.social/api/v1/statuses/1000/context").mock(return_value=httpx.Response(200, json={
        "ancestors": [], "descendants": [{"id": "1500", "content": "<p>the upload kept timing out for me too</p>",
                                          "created_at": "2024-07-02T00:00:00Z", "url": "https://mastodon.social/@x/1500",
                                          "in_reply_to_id": "1000", "reblog": None}]}))
    run_all(app, clock)
    assert len(seen_max_ids) <= 5                                          # dry pages stop the paging
    urls = {r["url"]: r["tier"] for r in app.db.conn.execute("SELECT url, tier FROM items")}
    assert urls["https://mastodon.social/@someone/1000"] == "relevant"
    assert urls["https://mastodon.social/@x/1500"] == "relevant"
    assert not any("/@someone/961" in u for u in urls)


@respx.mock
def test_mastodon_full_text_search_needs_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MASTODON_INSTANCE", "https://fosstodon.org")
    monkeypatch.setenv("MASTODON_ACCESS_TOKEN", "tok")
    app, _ = make_app(tmp_path, {})
    from wbscraper.sources import mastodon
    tasks = mastodon.plan(app.lib, app.settings)
    assert any(t[0] == "masto_search" and t[1]["instance"] == "fosstodon.org" for t in tasks)
    monkeypatch.delenv("MASTODON_ACCESS_TOKEN")
    assert not any(t[0] == "masto_search" for t in mastodon.plan(app.lib, app.settings))


def test_youtube_is_disabled_by_default_and_needs_a_key(tmp_path, monkeypatch, capsys):
    app, _ = make_app(tmp_path, {})
    assert "YouTubeHandler" not in [type(h).__name__ for h in app.handlers]
    assert not any(t[3] == "youtube" for t in queries.plan_all(app.lib, app.settings))
    assert main(["sources", "--data-dir", str(tmp_path / "d")]) == 0
    assert "youtube" in capsys.readouterr().out


@respx.mock
def test_youtube_search_comments_and_comments_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    app, clock = make_app(tmp_path, {}, sources={"youtube": {"enabled": True}})
    assert any(type(h).__name__ == "YouTubeHandler" for h in app.handlers)
    app.db.add_task("yt_search", {"q": "SMARTS stormwater"}, 495, "youtube")
    respx.get("https://www.googleapis.com/youtube/v3/search").mock(return_value=httpx.Response(200, json={"items": [
        {"id": {"videoId": "vid1"}, "snippet": {"title": "How to use SMARTS stormwater annual report website",
                                                "description": "Login and upload walkthrough", "publishedAt": "2023-05-01T00:00:00Z"}},
        {"id": {"videoId": "vid2"}, "snippet": {"title": "Cat video", "description": "cats", "publishedAt": "2023-05-02T00:00:00Z"}}]}))
    ct = respx.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(return_value=httpx.Response(200, json={
        "items": [{"snippet": {"topLevelComment": {"id": "Ug1", "snippet": {
            "textDisplay": "The SMARTS login always fails for me, thanks for the walkthrough", "publishedAt": "2023-05-05T00:00:00Z"}}},
                   "replies": {"comments": [{"id": "Ug1.r", "snippet": {"textDisplay": "same here, the upload is broken",
                                                                          "publishedAt": "2023-05-06T00:00:00Z"}}]}}]}))
    run_all(app, clock)
    assert ct.call_count == 1                                              # only the relevant video's comments were fetched
    urls = {r["url"] for r in app.db.conn.execute("SELECT url FROM items")}
    assert "https://www.youtube.com/watch?v=vid1" in urls and "https://www.youtube.com/watch?v=vid1&lc=Ug1" in urls
    assert not any("vid2" in u for u in urls)


@respx.mock
def test_youtube_quota_exhaustion_pauses_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    app, clock = make_app(tmp_path, {}, sources={"youtube": {"enabled": True}})
    app.db.add_task("yt_search", {"q": "CIWQS"}, 495, "youtube")
    respx.get("https://www.googleapis.com/youtube/v3/search").mock(return_value=httpx.Response(
        403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}))
    run_all(app, clock, min_minutes=0, max_minutes=2)
    assert app.http.cooldown_remaining("www.googleapis.com") > 3600
