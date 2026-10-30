"""YouTube video descriptions and comments through the official Data API v3 (free key; disabled by default).

Quota: search = 100 units, comment page = 1 unit, default free quota 10,000 units/day. `youtube.max_search_calls`
caps searches per collection. Setup: create a key in Google Cloud Console (YouTube Data API v3), set YOUTUBE_API_KEY,
then run `wbscraper run --enable youtube` (or set `sources.youtube.enabled: true`).
"""
from __future__ import annotations

import os

from ..dates import to_iso
from ..http import FetchError
from .base import RawItem
from .handler_base import ApiHandler

API = "https://www.googleapis.com/youtube/v3/"


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    if not os.environ.get("YOUTUBE_API_KEY"):
        return []
    from .. import queries
    cfg = lib.forums.get("youtube", {}) or {}
    cap = int(cfg.get("max_search_calls", 40))
    qs = [a.text for a in queries.compact(lib, queries.anchors(lib, 1))] + list(cfg.get("extra_queries", []))
    return [("yt_search", {"q": q}, 495, "youtube") for q in qs[:cap]]


class YouTubeHandler(ApiHandler):
    TASK_TYPES = {"yt_search", "yt_comments"}
    PROVIDER = "youtube"
    DOMAIN = "www.googleapis.com"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.key = os.environ.get("YOUTUBE_API_KEY")
        self.max_comment_pages = int(self.cfg.get("max_comment_pages", 5))

    def next_domain(self, task_type: str, payload: dict):
        return None if not self.key else super().next_domain(task_type, payload)

    def earliest_ready(self, task_type: str, payload: dict) -> float:
        return float("inf") if not self.key else super().earliest_ready(task_type, payload)

    def _get(self, path: str, params: dict):
        try:
            return self.http.get_json(API + path, params={**params, "key": self.key}, domain=self.DOMAIN)
        except FetchError as err:
            body = err.body.lower()
            if err.kind == "forbidden" and "commentsdisabled" in body:
                return None
            if err.kind in ("forbidden", "rate_limited") and "quota" in body:
                self.http.pacer.force_cooldown(self.DOMAIN, 6 * 3600)
                raise FetchError("rate_limited", "YouTube daily quota exhausted", retry_after=6 * 3600) from err
            raise

    def _run_yt_search(self, p: dict, priority: int):
        q = p["q"]
        self.stats.current_task = f"YouTube: {q}"
        data = self._get("search", {"part": "snippet", "q": q, "type": "video", "maxResults": 25, "order": "relevance"})
        if not isinstance(data, dict) or "items" not in data:
            raise FetchError("bad_json", "unexpected YouTube payload")
        raws, ids = [], {}
        for it in data["items"]:
            vid = (it.get("id") or {}).get("videoId")
            sn = it.get("snippet") or {}
            if not vid:
                continue
            title, desc = (sn.get("title") or "").strip(), (sn.get("description") or "").strip()
            r = RawItem(uid=f"yt:v{vid}", source="youtube", kind="post", url=f"https://www.youtube.com/watch?v={vid}",
                        text=f"{title}\n\n{desc}".strip(), date=to_iso(sn.get("publishedAt")), thread_uid=f"yt:v{vid}",
                        title=title, community=None)
            raws.append(r)
            ids[r.uid] = vid
        results = self.ingest(raws, "yt_search")
        for raw, res in zip(raws, results):
            if res.status in ("stored", "duplicate") and (res.entity_hit or res.tier != "irrelevant"):
                self.db.add_task("yt_comments", {"video_id": ids[raw.uid], "page_token": None, "page": 0}, 600, "youtube")
        return len(data["items"]), None

    def _run_yt_comments(self, p: dict, priority: int):
        vid, page = p["video_id"], int(p.get("page", 0))
        self.stats.current_task = f"YouTube comments {vid} p{page + 1}"
        params = {"part": "snippet,replies", "videoId": vid, "maxResults": 100, "order": "time", "textFormat": "plainText"}
        if p.get("page_token"):
            params["pageToken"] = p["page_token"]
        data = self._get("commentThreads", params)
        if not isinstance(data, dict):
            return 0, None
        raws = []
        for th in data.get("items", []):
            top = (th.get("snippet") or {}).get("topLevelComment") or {}
            for c, kind in [(top, "comment")] + [(r, "comment") for r in (th.get("replies") or {}).get("comments", [])]:
                sn = c.get("snippet") or {}
                text = (sn.get("textDisplay") or sn.get("textOriginal") or "").strip()
                if text and c.get("id"):
                    raws.append(RawItem(uid=f"yt:c{c['id']}", source="youtube", kind=kind,
                                        url=f"https://www.youtube.com/watch?v={vid}&lc={c['id']}", text=text,
                                        date=to_iso(sn.get("publishedAt")), thread_uid=f"yt:v{vid}"))
        self.ingest(raws, "yt_comments")
        token = data.get("nextPageToken")
        if token and page + 1 < self.max_comment_pages:
            self.db.add_task("yt_comments", {**p, "page_token": token, "page": page + 1}, priority, "youtube")
        else:
            self.db.mark_thread_expanded(f"yt:v{vid}")
        return len(raws), None
