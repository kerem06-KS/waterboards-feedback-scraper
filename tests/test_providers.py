import httpx
import pytest
import respx

from helpers import FakeClock, make_settings
from wbscraper.http import FetchError, HttpClient
from wbscraper.sources.base import Unsupported
from wbscraper.sources.reddit_arcticshift import ArcticShiftProvider
from wbscraper.sources.reddit_common import comment_url, parse_reddit_url, post_from_json
from wbscraper.sources.reddit_praw import PrawProvider
from wbscraper.sources.reddit_pullpush import PullPushProvider
from wbscraper.sources.reddit_search_discovery import DiscoveryProvider

PP = "https://api.pullpush.io/reddit/search/"
AS = "https://arctic-shift.photon-reddit.com/api/"


@pytest.fixture
def http(tmp_path):
    clock = FakeClock()
    return HttpClient(make_settings(tmp_path), clock=clock.time, sleep=clock.sleep)


@respx.mock
def test_pullpush_post_search_normalises_and_never_reads_author(http):
    route = respx.get(PP + "submission/").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "abc12", "title": "CIWQS down?", "selftext": "cannot log in", "created_utc": 1700000100.0,
         "subreddit": "Wastewater", "author": "someone", "score": 3},
        {"id": "abc11", "title": "older", "selftext": "[removed]", "created_utc": 1700000000, "subreddit": "Wastewater"},
    ]}))
    page = PullPushProvider().search_posts(http, q="CIWQS", subreddit=None, after=0, before=2_000_000_000, limit=100)
    assert page.n_raw == 2 and page.oldest_ts == 1700000000 and page.newest_ts == 1700000100
    assert page.items[0].text == "CIWQS down?\n\ncannot log in"
    assert page.items[1].text == "older"                     # [removed] body dropped, title kept
    assert page.items[0].url == "https://www.reddit.com/r/Wastewater/comments/abc12/"
    assert not hasattr(page.items[0], "author")
    params = dict(route.calls[0].request.url.params)
    assert params["q"] == "CIWQS" and params["sort"] == "desc" and params["size"] == "100"


@respx.mock
def test_pullpush_comment_thread_and_ids(http):
    respx.get(PP + "comment/").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "c1", "link_id": "t3_p1", "parent_id": "t3_p1", "body": "same here", "created_utc": 1700000200, "subreddit": "x"}]}))
    page = PullPushProvider().thread_comments(http, "p1", after=0, before=2_000_000_000, limit=100)
    assert page.items[0].uid == "reddit:t1_c1" and page.items[0].thread_uid == "reddit:t3_p1"
    assert page.items[0].url == comment_url("p1", "c1", "x")
    respx.get(PP + "submission/").mock(return_value=httpx.Response(200, json={"data": []}))
    assert PullPushProvider().posts_by_ids(http, ["t3_p1", "p2"]).items == []


@respx.mock
def test_pullpush_malformed_payloads_raise_bad_json(http):
    respx.get(PP + "submission/").mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(FetchError) as e:
        PullPushProvider().search_posts(http, q="x", subreddit=None, after=0, before=1, limit=1)
    assert e.value.kind == "bad_json"


@respx.mock
def test_arcticshift_requests_only_text_fields_and_requires_subreddit(http):
    route = respx.get(AS + "comments/search").mock(return_value=httpx.Response(200, json={"data": []}))
    ArcticShiftProvider().search_comments(http, q="CIWQS", subreddit="Wastewater", after=0, before=10, limit=100)
    params = dict(route.calls[0].request.url.params)
    assert "author" not in params["fields"] and params["body"] == "CIWQS" and params["subreddit"] == "Wastewater"
    with pytest.raises(Unsupported):
        ArcticShiftProvider().search_posts(http, q="CIWQS", subreddit=None, after=0, before=10, limit=100)


@respx.mock
def test_arcticshift_timeout_is_reported_as_query_timeout(http):
    respx.get(AS + "posts/search").mock(return_value=httpx.Response(422, json={"data": None, "error": "Timeout. Maybe slow down a bit"}))
    with pytest.raises(FetchError) as e:
        ArcticShiftProvider().search_posts(http, q="x", subreddit="California", after=0, before=10, limit=100)
    assert e.value.kind == "query_timeout"


@respx.mock
def test_arcticshift_other_client_errors_stay_client_errors(http):
    respx.get(AS + "posts/ids").mock(return_value=httpx.Response(400, json={"data": None, "error": "'permalink' is not a valid field"}))
    with pytest.raises(FetchError) as e:
        ArcticShiftProvider().posts_by_ids(http, ["abc12"])
    assert e.value.kind == "client"


@respx.mock
def test_arcticshift_url_prefix_search_is_global(http):
    route = respx.get(AS + "posts/search").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "1c3x19v", "title": "house near site", "selftext": "", "created_utc": 1713110773, "subreddit": "environment",
         "url": "https://geotracker.waterboards.ca.gov/profile_report.asp?global_id=L1"}]}))
    page = ArcticShiftProvider().posts_by_url(http, "https://geotracker.waterboards.ca.gov", after=0, before=2_000_000_000, limit=100)
    assert page.items[0].uid == "reddit:t3_1c3x19v"
    assert "subreddit" not in dict(route.calls[0].request.url.params)


def test_discovery_extracts_reddit_threads_and_records_other_urls(http):
    results = [
        {"href": "https://www.reddit.com/r/Wastewater/comments/1abcde/ciwqs_help/", "title": "t"},
        {"href": "https://old.reddit.com/r/California/comments/1abcde/ciwqs_help/k9x2z1a/", "title": "dup"},
        {"href": "https://www.eng-tips.com/viewthread.cfm?qid=1", "title": "forum"},
    ]
    prov = DiscoveryProvider(search_fn=lambda q, n: results)
    page = prov.discover(http, 'site:reddit.com "CIWQS"')
    assert page.post_ids == ["1abcde"] and page.urls[0][0].startswith("https://www.eng-tips.com")
    assert page.final


def test_discovery_errors_become_fetch_errors(http):
    def boom(q, n):
        raise RuntimeError("Ratelimit 202")
    with pytest.raises(FetchError) as e:
        DiscoveryProvider(search_fn=boom).discover(http, "q")
    assert e.value.kind == "rate_limited"


def test_parse_reddit_url():
    r = parse_reddit_url("https://www.reddit.com/r/Wastewater/comments/1abcde/title/xyz1234/?context=3")
    assert r == {"subreddit": "Wastewater", "post": "1abcde", "comment": "xyz1234"}
    assert parse_reddit_url("https://example.com/") is None


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Comments(list):
    def replace_more(self, limit=None): self.replaced = True
    def list(self): return list(self)


class FakeReddit:
    def __init__(self):
        c = _Comments([_Obj(id="k1", body="login broken", created_utc=1700000300, link_id="t3_p9", parent_id="t3_p9", subreddit="x")])
        self.sub = _Obj(id="p9", title="CIWQS help", selftext="", created_utc=1700000000, subreddit="x", score=2, comments=c)

    def subreddit(self, name):
        outer = self
        return _Obj(search=lambda q, **kw: [outer.sub])

    def info(self, fullnames): return [self.sub]
    def submission(self, id): return self.sub


def test_praw_provider_with_a_fake_client(http):
    prov = PrawProvider(client_id="x", client_secret="y", reddit=FakeReddit())
    assert prov.available()
    page = prov.search_posts(http, q="CIWQS", subreddit=None, after=0, before=2_000_000_000, limit=100)
    assert page.final and page.items[0].uid == "reddit:t3_p9"
    assert prov.thread_comments(http, "p9", after=0, before=1, limit=1).items[0].uid == "reddit:t1_k1"
    assert not PrawProvider(client_id=None, client_secret=None).available()
