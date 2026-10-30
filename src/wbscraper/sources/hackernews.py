"""Hacker News via the free Algolia search API (no key). Stories and comments; relevant stories get their whole
comment tree fetched."""
from __future__ import annotations

from ..dates import to_iso
from ..htmltext import html_to_text
from ..http import FetchError
from .base import RawItem
from .handler_base import ApiHandler

SEARCH = "https://hn.algolia.com/api/v1/search"
ITEM = "https://hn.algolia.com/api/v1/items/"
HN_URL = "https://news.ycombinator.com/item?id="


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    cfg = lib.forums.get("hackernews", {})
    terms = [a.text for a in queries.compact(lib, queries.anchors(lib, 1))] + list(queries.pullpush_tokens(lib)) + \
        list(cfg.get("extra_queries", []))
    seen, tasks = set(), []
    for q in terms:
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        tasks.append(("hn_search", {"q": q, "page": 0, "dry": 0}, 540, "hackernews"))
    return tasks


def _story_item(hit: dict) -> RawItem | None:
    oid = str(hit.get("objectID") or "")
    if not oid:
        return None
    is_comment = "comment" in (hit.get("_tags") or []) or hit.get("comment_text") is not None
    if is_comment:
        text = html_to_text(hit.get("comment_text"))
        story = str(hit.get("story_id") or oid)
        return RawItem(uid=f"hn:c{oid}", source="hackernews", kind="comment", url=HN_URL + oid, text=text,
                       date=to_iso(hit.get("created_at_i")), thread_uid=f"hn:s{story}",
                       parent_uid=f"hn:{hit.get('parent_id')}" if hit.get("parent_id") else None)
    title = (hit.get("title") or "").strip()
    body = html_to_text(hit.get("story_text"))
    link = str(hit.get("url") or "")
    return RawItem(uid=f"hn:s{oid}", source="hackernews", kind="post", url=HN_URL + oid,
                   text=f"{title}\n\n{body}".strip() if body else title, date=to_iso(hit.get("created_at_i")),
                   thread_uid=f"hn:s{oid}", title=title, score_hint=link)


class HackerNewsHandler(ApiHandler):
    TASK_TYPES = {"hn_search", "hn_item"}
    PROVIDER = "hackernews"
    DOMAIN = "hn.algolia.com"

    def _run_hn_search(self, p: dict, priority: int):
        q, page = p["q"], int(p.get("page", 0))
        self.stats.current_task = f"HN: {q} (p{page + 1})"
        data = self.http.get_json(SEARCH, params={"query": q, "tags": "(story,comment)", "hitsPerPage": 100, "page": page},
                                  domain=self.DOMAIN)
        if not isinstance(data, dict) or "hits" not in data:
            raise FetchError("bad_json", "unexpected Hacker News payload")
        hits = data["hits"]
        raws = [r for r in (_story_item(h) for h in hits) if r]
        results = self.ingest(raws, "hn_search")
        hit_count = 0
        for raw, res in zip(raws, results):
            if res.entity_hit or res.tier != "irrelevant":
                hit_count += 1
            if res.status == "stored" and raw.kind == "post" and res.own_relevant:
                story_id = raw.uid.split("hn:s", 1)[1]
                self.db.add_task("hn_item", {"id": story_id}, 650, "hackernews")
        dry = 0 if hit_count else int(p.get("dry", 0)) + 1
        max_pages = int(self.cfg.get("max_pages", 5))
        if page + 1 < min(int(data.get("nbPages", 0)), max_pages) and dry < 2:
            self.db.add_task("hn_search", {**p, "page": page + 1, "dry": dry}, priority, "hackernews")
        newest = max((h.get("created_at_i") or 0 for h in hits), default=None)
        return len(hits), newest or None

    def _run_hn_item(self, p: dict, priority: int):
        sid = str(p["id"])
        self.stats.current_task = f"HN thread {sid}"
        tree = self.http.get_json(ITEM + sid, domain=self.DOMAIN)
        if not isinstance(tree, dict):
            raise FetchError("bad_json", "unexpected Hacker News item payload")
        raws: list[RawItem] = []
        stack = list(tree.get("children") or [])
        while stack:                                   # iterative: threads can be deeply nested
            node = stack.pop()
            stack.extend(node.get("children") or [])
            text = html_to_text(node.get("text"))
            if text:
                raws.append(RawItem(uid=f"hn:c{node['id']}", source="hackernews", kind="comment",
                                    url=HN_URL + str(node["id"]), text=text, date=to_iso(node.get("created_at_i")),
                                    thread_uid=f"hn:s{sid}", parent_uid=f"hn:{node.get('parent_id')}"))
        self.ingest(raws, "hn_item")
        self.db.mark_thread_expanded(f"hn:s{sid}")
        return len(raws), None
