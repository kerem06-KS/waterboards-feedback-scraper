"""Mastodon: public hashtag timelines (keyless) and, with a free account token, full-text status search.

* keyless: `/api/v1/timelines/tag/<tag>` on configured instances, paged with `max_id`; relevance filtering removes the
  (many) unrelated posts under broad tags such as #stormwater.
* optional: MASTODON_INSTANCE + MASTODON_ACCESS_TOKEN enable `/api/v2/search?type=statuses` on that instance.
Status URLs contain the account name (Mastodon's own public URL format); the account is never read or stored otherwise.
"""
from __future__ import annotations

import os

from ..dates import to_iso
from ..htmltext import html_to_text
from ..http import FetchError
from .base import RawItem
from .handler_base import ApiHandler


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    cfg = lib.forums.get("mastodon", {}) or {}
    tasks = [("masto_tag", {"instance": inst, "tag": tag, "max_id": None, "dry": 0, "page": 0}, 500 - i, "mastodon")
             for i, inst in enumerate(cfg.get("instances", [])) for tag in cfg.get("tags", [])]
    if os.environ.get("MASTODON_INSTANCE") and os.environ.get("MASTODON_ACCESS_TOKEN"):
        inst = os.environ["MASTODON_INSTANCE"].replace("https://", "").strip("/")
        terms = [a.text for a in queries.compact(lib, queries.anchors(lib, 1))] + queries.pullpush_tokens(lib)
        seen = set()
        for q in terms:
            if q.lower() not in seen:
                seen.add(q.lower())
                tasks.append(("masto_search", {"instance": inst, "q": q, "offset": 0}, 515, "mastodon"))
    return tasks


def status_raw(inst: str, st: dict) -> RawItem | None:
    if st.get("reblog"):
        return None                                  # boosts repeat someone else's post
    text = html_to_text(st.get("content"))
    if st.get("spoiler_text"):
        text = f"{st['spoiler_text']}\n\n{text}".strip()
    url = st.get("url") or st.get("uri")
    if not text or not url:
        return None
    parent = st.get("in_reply_to_id")
    return RawItem(uid=f"masto:{inst}:{st['id']}", source="mastodon", kind="comment" if parent else "post", url=url,
                   text=text, date=to_iso(st.get("created_at")), thread_uid=f"masto:{inst}:{parent or st['id']}",
                   parent_uid=f"masto:{inst}:{parent}" if parent else None, community=inst)


class MastodonHandler(ApiHandler):
    TASK_TYPES = {"masto_tag", "masto_search", "masto_context"}
    PROVIDER = "mastodon"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.token = os.environ.get("MASTODON_ACCESS_TOKEN")
        self.max_pages = int(self.cfg.get("max_pages", 25))

    def domain_for(self, task_type: str, payload: dict) -> str:
        return payload["instance"]

    def _get(self, inst: str, path: str, params: dict, authed: bool = False):
        headers = {"Authorization": f"Bearer {self.token}"} if (authed and self.token) else None
        try:
            return self.http.get_json(f"https://{inst}{path}", params=params, headers=headers, domain=inst)
        except FetchError as err:
            if err.kind in ("client", "forbidden") and err.status in (401, 403, 404, 422):
                self.db.bump_counter("masto_instance_refused")     # timeline disabled for anonymous users: skip politely
                return None
            raise

    def _run_masto_tag(self, p: dict, priority: int):
        inst, tag, max_id = p["instance"], p["tag"], p.get("max_id")
        self.stats.current_task = f"Mastodon {inst} #{tag}"
        params = {"limit": 40, **({"max_id": max_id} if max_id else {})}
        data = self._get(inst, f"/api/v1/timelines/tag/{tag}", params)
        if not isinstance(data, list):
            return 0, None
        raws = [r for r in (status_raw(inst, st) for st in data) if r]
        results = self.ingest(raws, "masto_tag")
        meta = {f"masto:{inst}:{st['id']}": st for st in data if isinstance(st, dict) and "id" in st}
        hits = 0
        for raw, res in zip(raws, results):
            if res.entity_hit or res.tier != "irrelevant":
                hits += 1
            st = meta.get(raw.uid, {})
            if res.status == "stored" and res.own_relevant and st.get("replies_count"):
                self.db.add_task("masto_context", {"instance": inst, "id": st["id"]}, 640, "mastodon")
        dry = 0 if hits else int(p.get("dry", 0)) + 1
        if len(data) >= 40 and dry < 3 and int(p.get("page", 0)) + 1 < self.max_pages:
            last = data[-1]["id"]
            self.db.add_task("masto_tag", {**p, "max_id": last, "dry": dry, "page": int(p.get("page", 0)) + 1}, priority, "mastodon")
        return len(data), None

    def _run_masto_search(self, p: dict, priority: int):
        inst, q, offset = p["instance"], p["q"], int(p.get("offset", 0))
        self.stats.current_task = f"Mastodon search: {q}"
        data = self._get(inst, "/api/v2/search", {"q": q, "type": "statuses", "limit": 40, "offset": offset,
                                                  "resolve": "false"}, authed=True)
        if not isinstance(data, dict):
            return 0, None
        statuses = data.get("statuses") or []
        raws = [r for r in (status_raw(inst, st) for st in statuses) if r]
        self.ingest(raws, "masto_search")
        if len(statuses) >= 40 and offset < 400:
            self.db.add_task("masto_search", {**p, "offset": offset + 40}, priority, "mastodon")
        return len(statuses), None

    def _run_masto_context(self, p: dict, priority: int):
        inst, sid = p["instance"], p["id"]
        self.stats.current_task = f"Mastodon thread {sid}"
        data = self._get(inst, f"/api/v1/statuses/{sid}/context", {})
        if not isinstance(data, dict):
            return 0, None
        raws = []
        for st in (data.get("ancestors") or []) + (data.get("descendants") or []):
            r = status_raw(inst, st)
            if r:
                r.thread_uid = f"masto:{inst}:{sid}"
                raws.append(r)
        self.ingest(raws, "masto_context")
        return len(raws), None
