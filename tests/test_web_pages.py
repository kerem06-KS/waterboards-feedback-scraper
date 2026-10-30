import json

import httpx
import respx

from helpers import make_app, run_all
from wbscraper.sources.reddit_search_discovery import DiscoveryProvider
from wbscraper.sources.web_pages import extract_article, extract_jsonld, queue_discovered_url

JSONLD = json.dumps({"@context": "https://schema.org", "@type": "DiscussionForumPosting",
                     "headline": "CIWQS login keeps failing", "text": "Our eSMR upload fails on the CIWQS website every time.",
                     "datePublished": "2024-04-02T09:30:00-07:00", "url": "https://forum.example.org/t/ciwqs-login/12",
                     "author": {"@type": "Person", "name": "Should Never Appear"},
                     "comment": [{"@type": "Comment", "text": "Same here, the website is broken.",
                                  "datePublished": "2024-04-03T10:00:00Z", "url": "https://forum.example.org/t/ciwqs-login/12#post-2"}]})
PAGE = f'<html><head><script type="application/ld+json">{JSONLD}</script></head><body>x</body></html>'


def test_jsonld_extraction_never_reads_authors():
    items = extract_jsonld(PAGE, "https://forum.example.org/t/ciwqs-login/12")
    assert len(items) == 2
    post = next(i for i in items if i.kind == "post")
    assert post.text.startswith("CIWQS login keeps failing") and post.date == "2024-04-02T16:30:00Z"
    assert next(i for i in items if i.kind == "comment").url.endswith("#post-2")
    assert "Should Never Appear" not in json.dumps([i.__dict__ for i in items])


def test_article_fallback_uses_main_text_and_publish_date():
    html = ("<html><head><title>Why the State Water Board website frustrates operators</title>"
            "<meta property='article:published_time' content='2023-06-01T12:00:00Z'></head>"
            "<body><nav>menu</nav><article><p>The SMARTS portal login fails and the website is slow.</p></article></body></html>")
    items = extract_article(html, "https://blog.example.com/post")
    assert len(items) == 1 and "menu" not in items[0].text and items[0].date == "2023-06-01T12:00:00Z"


def test_discovered_urls_are_routed_and_login_walled_sites_skipped(tmp_path):
    app, _ = make_app(tmp_path, {})
    s, db = app.settings, app.db
    assert queue_discovered_url(db, s, "https://forum.example.org/t/1", "q")
    assert not queue_discovered_url(db, s, "https://forum.example.org/t/1", "q")                 # already seen
    assert not queue_discovered_url(db, s, "https://www.facebook.com/groups/x/posts/1", "q")     # login wall
    assert queue_discovered_url(db, s, "https://www.waterboards.ca.gov/x/letter.pdf", "q")
    assert queue_discovered_url(db, s, "https://www.waterboards.ca.gov/x/page.html", "q")
    types = sorted(r["type"] for r in db.conn.execute("SELECT type FROM tasks"))
    assert types == ["wb_page", "wb_pdf", "web_page"]
    assert db.urls_seen_count() == 4


@respx.mock
def test_web_discover_to_page_end_to_end(tmp_path):
    results = [{"href": "https://forum.example.org/t/ciwqs-login/12"},
               {"href": "https://www.facebook.com/groups/abc/posts/9"},
               {"href": "https://www.reddit.com/r/Wastewater/comments/1abcde/thread/"}]
    app, clock = make_app(tmp_path, {})
    for h in app.handlers:
        if hasattr(h, "attach_discovery"):
            h.attach_discovery(DiscoveryProvider(search_fn=lambda q, n: results))
    app.db.add_task("web_discover", {"q": '"CIWQS" login problem'}, 470, "web_pages")
    respx.get("https://forum.example.org/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /"))
    respx.get("https://forum.example.org/t/ciwqs-login/12").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"}, text=PAGE))
    # the Reddit thread is handed to the Reddit handler, which has no providers in this test -> stays pending
    run_all(app, clock, min_minutes=0, max_minutes=3)
    urls = {r["url"]: r["tier"] for r in app.db.conn.execute("SELECT url, tier FROM items")}
    assert urls["https://forum.example.org/t/ciwqs-login/12"] == "relevant"
    assert "https://forum.example.org/t/ciwqs-login/12#post-2" in urls
    assert not any("facebook" in u for u in urls)
    assert app.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE type='reddit_posts'").fetchone()[0] == 1


@respx.mock
def test_pages_blocked_by_robots_are_not_fetched(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("web_page", {"url": "https://nope.example.org/t/1"}, 460, "web_pages")
    respx.get("https://nope.example.org/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /"))
    page = respx.get("https://nope.example.org/t/1").mock(return_value=httpx.Response(200, text=PAGE))
    run_all(app, clock)
    assert page.call_count == 0 and app.db.counters()["web_robots_blocked"] == 1


@respx.mock
def test_discourse_and_wordpress_comment_sources(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("web_page", {"url": "https://community.example.org/t/ciwqs-help/77"}, 460, "web_pages")
    app.db.add_task("web_page", {"url": "https://blog.example.com/2024/ciwqs-post/"}, 460, "web_pages")
    respx.get("https://community.example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://blog.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://community.example.org/t/ciwqs-help/77").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html><meta name='generator' content='Discourse 3.2'><body>hi</body></html>"))
    respx.get("https://community.example.org/t/77.json").mock(return_value=httpx.Response(200, json={
        "id": 77, "slug": "ciwqs-help", "title": "CIWQS login help",
        "post_stream": {"posts": [
            {"id": 1, "post_number": 1, "cooked": "<p>The CIWQS website login fails for our team.</p>", "created_at": "2024-05-01T00:00:00Z"},
            {"id": 2, "post_number": 2, "cooked": "<p>Same problem, the upload is broken too.</p>", "created_at": "2024-05-02T00:00:00Z"}]}}))
    respx.get("https://blog.example.com/2024/ciwqs-post/").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"},
        text="<html><head><link rel='shortlink' href='https://blog.example.com/?p=55'/>"
             "<link rel='https://api.w.org/' href='https://blog.example.com/wp-json/'/></head>"
             "<body><article><p>Notes about the CIWQS website.</p></article></body></html>"))
    respx.get("https://blog.example.com/wp-json/wp/v2/comments").mock(return_value=httpx.Response(200, json=[
        {"id": 9, "link": "https://blog.example.com/2024/ciwqs-post/#comment-9", "date_gmt": "2024-06-01T00:00:00",
         "content": {"rendered": "<p>The CIWQS login is broken for me too</p>"}}]))
    run_all(app, clock)
    urls = {r["url"] for r in app.db.conn.execute("SELECT url FROM items")}
    assert "https://community.example.org/t/ciwqs-help/77/1" in urls and "https://community.example.org/t/ciwqs-help/77/2" in urls
    assert "https://blog.example.com/2024/ciwqs-post/#comment-9" in urls
