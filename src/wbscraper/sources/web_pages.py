"""Forum / blog / Q&A pages found through search engines. robots.txt is obeyed; login-walled platforms are skipped.

Extraction (in order): schema.org JSON-LD (DiscussionForumPosting, QAPage, Question/Answer, Comment, Article…),
Discourse topic JSON, WordPress comments API, then the page's main article text. Pages that yield nothing are recorded as
"discovered, not extracted" so the coverage report is honest about them.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..dates import to_iso
from ..htmltext import html_to_text
from ..http import FetchError
from ..robots import RobotsCache
from .base import RawItem
from .handler_base import ApiHandler
from .reddit_search_discovery import DiscoveryProvider

POST_TYPES = {"discussionforumposting", "socialmediaposting", "blogposting", "article", "newsarticle", "question",
              "qapage", "answer", "comment", "review"}


def queue_discovered_url(db, settings, url: str, query: str) -> bool:
    """Record a search-engine result and queue the right fetch task. Returns True if it was new."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        return False
    skip = settings.get("web_pages.skip_domains", None)
    if skip is None:
        skip = (settings.raw.get("_skip_domains") or [])
    if any(host == d or host.endswith("." + d) for d in skip):
        db.add_url_seen(url, host, query)
        return False
    if not db.add_url_seen(url, host, query):
        return False
    if host.endswith("waterboards.ca.gov") and settings.source_enabled("waterboards_docs"):
        if p.path.lower().endswith(".pdf"):
            db.add_task("wb_pdf", {"url": url}, 480, "waterboards_docs")
        else:
            db.add_task("wb_page", {"url": url, "depth": 1}, 485, "waterboards_docs")
        return True
    if settings.source_enabled("web_pages") and not p.path.lower().endswith(".pdf"):
        db.add_task("web_page", {"url": url}, 460, "web_pages")
        return True
    return False


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    cfg = lib.forums.get("web_pages", {})
    limit = int(cfg.get("max_discovery_queries", 220))
    qs = [(t, q) for t, q in queries.web_queries(lib, prefix="") if t <= 2]
    # Water Boards documents via search engines: comment letters that mention the online systems
    for e in lib.entities:
        if e.kind == "system" and (e.anchors.get(1) or []):
            qs.append((1, f'site:waterboards.ca.gov "comment" "{e.anchors[1][0]}"'))
    qs = sorted(qs, key=lambda x: x[0])[:limit]
    return [("web_discover", {"q": q}, 470 - 30 * (t - 1), "web_pages") for t, q in qs]


# ----------------------------------------------------------------------------- extraction
def _iter_jsonld(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for key in ("@graph", "mainEntity", "comment", "suggestedAnswer", "acceptedAnswer", "hasPart"):
                    if key in node:
                        stack.append(node[key])


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    t = t if isinstance(t, list) else [t]
    return {str(x).lower() for x in t if x}


def extract_jsonld(html: str, page_url: str) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawItem] = []
    thread = "web:" + hashlib.sha1(page_url.encode()).hexdigest()[:16]
    host = urlparse(page_url).hostname
    seen = set()
    for i, node in enumerate(_iter_jsonld(soup)):
        if not (_types(node) & POST_TYPES):
            continue
        body = node.get("articleBody") or node.get("text") or node.get("description") or ""
        title = node.get("headline") or node.get("name") or ""
        text = html_to_text(str(body))
        if title and not text.startswith(str(title)) and ("posting" in "".join(_types(node)) or "question" in _types(node) or "article" in "".join(_types(node))):
            text = f"{title}\n\n{text}".strip()
        if len(text) < 12:
            continue
        url = node.get("url") or node.get("@id") or page_url
        url = urljoin(page_url, str(url)) if isinstance(url, str) else page_url
        key = (url, text[:80])
        if key in seen:
            continue
        seen.add(key)
        kind = "comment" if (_types(node) & {"comment", "answer"}) else "post"
        out.append(RawItem(uid="web:" + hashlib.sha1(f"{url}|{text[:200]}".encode()).hexdigest()[:20], source="web",
                           kind=kind, url=url, text=text, date=to_iso(node.get("datePublished") or node.get("dateCreated")),
                           thread_uid=thread, community=host, title=str(title) or None))
    return out


def extract_article(html: str, page_url: str, max_chars: int = 8000) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        t.decompose()
    node = soup.find("article") or soup.find("main") or soup.body
    if node is None:
        return []
    text = html_to_text(str(node))[:max_chars]
    if len(text) < 40:
        return []
    title = soup.title.get_text(strip=True) if soup.title else None
    meta = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find("time")
    date = to_iso((meta.get("content") or meta.get("datetime")) if meta else None)
    host = urlparse(page_url).hostname
    return [RawItem(uid="web:" + hashlib.sha1(page_url.encode()).hexdigest()[:20], source="web", kind="post",
                    url=page_url, text=(f"{title}\n\n{text}" if title and not text.startswith(title) else text),
                    date=date, thread_uid="web:" + hashlib.sha1(page_url.encode()).hexdigest()[:16], community=host,
                    title=title)]


class WebPagesHandler(ApiHandler):
    TASK_TYPES = {"web_discover", "web_page"}
    PROVIDER = "web_pages"
    DOMAIN = "ddgs"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.max_bytes = int(self.cfg.get("max_page_bytes", 5_000_000))
        self.robots = RobotsCache(self.http)
        self.discovery: DiscoveryProvider | None = None

    def attach_discovery(self, provider: DiscoveryProvider) -> None:
        self.discovery = provider

    def domain_for(self, task_type: str, payload: dict) -> str:
        return "ddgs" if task_type == "web_discover" else (urlparse(payload["url"]).hostname or "default")

    def next_domain(self, task_type: str, payload: dict):
        if task_type == "web_discover" and (self.discovery is None or not self.discovery.available()):
            return None
        return super().next_domain(task_type, payload)

    def earliest_ready(self, task_type: str, payload: dict) -> float:
        if task_type == "web_discover" and (self.discovery is None or not self.discovery.available()):
            return float("inf")
        return super().earliest_ready(task_type, payload)

    def _run_web_discover(self, p: dict, priority: int):
        self.stats.current_task = f"web: {p['q'][:60]}"
        page = self.discovery.discover(self.http, p["q"])
        new = 0
        for url, q in page.urls:
            new += 1 if queue_discovered_url(self.db, self.settings, url, q) else 0
        reddit_on = any(self.settings.source_enabled(n) for n in ("reddit_arcticshift", "reddit_pullpush"))
        if page.post_ids and reddit_on:       # search engines also surface Reddit threads: hand them to the Reddit handler
            self.db.add_task("reddit_posts", {"ids": sorted(page.post_ids), "discovered": True}, 800, "reddit_posts")
        self.stats.urls_discovered += new + len(page.post_ids)
        return page.n_raw, None

    def _run_web_page(self, p: dict, priority: int):
        url = p["url"]
        host = urlparse(url).hostname or ""
        self.stats.current_task = f"page {host}"
        if not self.robots.allowed(url):
            self.db.bump_counter("web_robots_blocked")
            return 0, None
        res = self.http.get(url, domain=host, max_bytes=self.max_bytes, headers={"Accept": "text/html,*/*;q=0.5"})
        ctype = res.headers.get("content-type", "").lower()
        if "html" not in ctype and "xml" not in ctype:
            self.db.bump_counter("web_not_html")
            return 0, None
        html = res.text
        raws = extract_jsonld(html, res.url)
        raws += self._discourse(html, res.url)
        raws += self._wordpress(html, res.url)
        if not raws:
            raws = extract_article(html, res.url)
        if not raws:
            self.db.bump_counter("web_not_extracted")
            return 1, None
        self.ingest(raws, "web_page")
        return len(raws), None

    # -- platform-specific comment sources --------------------------------------------------------
    def _discourse(self, html: str, page_url: str) -> list[RawItem]:
        if "discourse" not in html[:20000].lower():
            return []
        m = re.search(r"/t/(?:[^/]+/)?(\d+)", urlparse(page_url).path)
        if not m:
            return []
        p = urlparse(page_url)
        api = f"{p.scheme}://{p.netloc}/t/{m.group(1)}.json"
        if not self.robots.allowed(api):
            return []
        try:
            data = self.http.get_json(api, domain=p.hostname)
        except FetchError:
            return []
        posts = ((data or {}).get("post_stream") or {}).get("posts") or []
        thread = "web:" + hashlib.sha1(page_url.encode()).hexdigest()[:16]
        slug = data.get("slug", "topic")
        out = []
        for post in posts:
            text = html_to_text(post.get("cooked"))
            if post.get("post_number") == 1 and data.get("title"):
                text = f"{data['title']}\n\n{text}"
            if len(text) < 12:
                continue
            out.append(RawItem(uid=f"web:disc:{p.hostname}:{post.get('id')}", source="web",
                               kind="post" if post.get("post_number") == 1 else "comment",
                               url=f"{p.scheme}://{p.netloc}/t/{slug}/{data.get('id')}/{post.get('post_number')}",
                               text=text, date=to_iso(post.get("created_at")), thread_uid=thread, community=p.hostname))
        return out

    def _wordpress(self, html: str, page_url: str) -> list[RawItem]:
        if "wp-json" not in html[:50000]:
            return []
        m = re.search(r"<link[^>]+rel=['\"]shortlink['\"][^>]+href=['\"][^'\"]*\?p=(\d+)", html) or \
            re.search(r"postid-(\d+)", html)
        if not m:
            return []
        p = urlparse(page_url)
        api = f"{p.scheme}://{p.netloc}/wp-json/wp/v2/comments"
        if not self.robots.allowed(api):
            return []
        try:
            data = self.http.get_json(api, params={"post": m.group(1), "per_page": 100}, domain=p.hostname)
        except FetchError:
            return []
        thread = "web:" + hashlib.sha1(page_url.encode()).hexdigest()[:16]
        out = []
        for c in data if isinstance(data, list) else []:
            text = html_to_text((c.get("content") or {}).get("rendered"))
            if len(text) < 12:
                continue
            out.append(RawItem(uid=f"web:wp:{p.hostname}:{c.get('id')}", source="web", kind="comment",
                               url=c.get("link") or page_url, text=text, date=to_iso(c.get("date_gmt") or c.get("date")),
                               thread_uid=thread, community=p.hostname))
        return out
