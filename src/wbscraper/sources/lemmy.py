"""Lemmy instances via the public, keyless /api/v3 endpoints (search + comment lists)."""
from __future__ import annotations

from ..dates import to_iso
from ..http import FetchError
from .base import RawItem
from .handler_base import ApiHandler


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    cfg = lib.forums.get("lemmy", {}) or {}
    terms = [a.text for a in queries.compact(lib, queries.anchors(lib, 1))] + queries.pullpush_tokens(lib) + \
        list(cfg.get("extra_queries", []))
    seen, qs = set(), []
    for q in terms:
        if q.lower() not in seen:
            seen.add(q.lower())
            qs.append(q)
    return [("lemmy_search", {"instance": inst, "q": q, "page": 1, "dry": 0}, 505 - i, "lemmy")
            for i, inst in enumerate(cfg.get("instances", [])) for q in qs]


class LemmyHandler(ApiHandler):
    TASK_TYPES = {"lemmy_search", "lemmy_comments"}
    PROVIDER = "lemmy"

    def domain_for(self, task_type: str, payload: dict) -> str:
        return payload["instance"]

    def _api(self, inst: str, path: str, params: dict):
        try:
            data = self.http.get_json(f"https://{inst}/api/v3/{path}", params=params, domain=inst)
        except FetchError as err:
            if err.kind in ("client", "forbidden") and err.status in (400, 401, 403, 404):
                self.db.bump_counter("lemmy_instance_refused")      # instance restricts anonymous search: skip politely
                return None
            raise
        if not isinstance(data, dict):
            raise FetchError("bad_json", "unexpected Lemmy payload")
        return data

    @staticmethod
    def _post_raw(inst: str, post: dict) -> RawItem | None:
        ap = post.get("ap_id") or f"https://{inst}/post/{post.get('id')}"
        title = (post.get("name") or "").strip()
        body = (post.get("body") or "").strip()
        if not title or post.get("deleted") or post.get("removed"):
            return None
        return RawItem(uid=f"lemmy:{ap}", source="lemmy", kind="post", url=ap,
                       text=f"{title}\n\n{body}".strip() if body else title, date=to_iso(post.get("published")),
                       thread_uid=f"lemmy:{ap}", title=title, community=inst, score_hint=post.get("url") or "")

    @staticmethod
    def _comment_raw(inst: str, comment: dict, post: dict) -> RawItem | None:
        ap = comment.get("ap_id") or f"https://{inst}/comment/{comment.get('id')}"
        text = (comment.get("content") or "").strip()
        pap = post.get("ap_id") or f"https://{inst}/post/{post.get('id')}"
        if not text or comment.get("deleted") or comment.get("removed"):
            return None
        return RawItem(uid=f"lemmy:{ap}", source="lemmy", kind="comment", url=ap, text=text,
                       date=to_iso(comment.get("published")), thread_uid=f"lemmy:{pap}", community=inst)

    def _run_lemmy_search(self, p: dict, priority: int):
        inst, q, page = p["instance"], p["q"], int(p.get("page", 1))
        self.stats.current_task = f"Lemmy {inst}: {q} p{page}"
        data = self._api(inst, "search", {"q": q, "type_": "All", "sort": "New", "limit": 50, "page": page})
        if data is None:
            return 0, None
        posts, comments = data.get("posts") or [], data.get("comments") or []
        raws, post_ids = [], {}
        for v in posts:
            r = self._post_raw(inst, v.get("post") or {})
            if r:
                raws.append(r)
                post_ids[r.uid] = (v.get("post") or {}).get("id")
        for v in comments:
            r = self._comment_raw(inst, v.get("comment") or {}, v.get("post") or {})
            if r:
                raws.append(r)
        results = self.ingest(raws, "lemmy_search")
        hits = 0
        for raw, res in zip(raws, results):
            if res.entity_hit or res.tier != "irrelevant":
                hits += 1
            if res.status == "stored" and raw.kind == "post" and res.own_relevant and post_ids.get(raw.uid):
                self.db.add_task("lemmy_comments", {"instance": inst, "post_id": post_ids[raw.uid], "page": 1,
                                                    "thread": raw.thread_uid}, 640, "lemmy")
        dry = 0 if hits else int(p.get("dry", 0)) + 1
        if (len(posts) >= 50 or len(comments) >= 50) and dry < 2 and page < 10:
            self.db.add_task("lemmy_search", {**p, "page": page + 1, "dry": dry}, priority, "lemmy")
        return len(posts) + len(comments), None

    def _run_lemmy_comments(self, p: dict, priority: int):
        inst, page = p["instance"], int(p.get("page", 1))
        self.stats.current_task = f"Lemmy thread {inst}/{p['post_id']} p{page}"
        data = self._api(inst, "comment/list", {"post_id": p["post_id"], "type_": "All", "sort": "New", "max_depth": 12,
                                                "limit": 50, "page": page})
        if data is None:
            return 0, None
        comments = data.get("comments") or []
        raws = []
        for v in comments:
            r = self._comment_raw(inst, v.get("comment") or {}, v.get("post") or {})
            if r:
                r.thread_uid = p["thread"]
                raws.append(r)
        self.ingest(raws, "lemmy_comments")
        if len(comments) >= 50 and page < 20:
            self.db.add_task("lemmy_comments", {**p, "page": page + 1}, priority, "lemmy")
        else:
            self.db.mark_thread_expanded(p["thread"])
        return len(comments), None
