"""Bluesky public posts through the AT Protocol search endpoint.

Works without credentials: `searchPosts` on api.bsky.app answers anonymous requests when no cursor is used, so
this adapter pages by date window instead (a window that returns a full page of 100 is split in half until it is
a day wide). Optional app-password credentials (BSKY_HANDLE / BSKY_APP_PASSWORD) are used only as a fallback if the
anonymous endpoint starts refusing requests. Post URLs use the account's DID, an opaque identifier.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from ..dates import to_iso
from ..http import FetchError
from .base import RawItem
from .handler_base import ApiHandler

PUBLIC = "https://api.bsky.app/xrpc/"
AUTHED = "https://bsky.social/xrpc/"
BSKY_START = 1672531200            # 2023-01-01: nothing public exists before this
PAGE = 100


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    terms = [a.text for a in queries.compact(lib, queries.anchors(lib, 1))] + queries.pullpush_tokens(lib) + \
        list((lib.forums.get("bluesky", {}) or {}).get("extra_queries", []))
    start = max(int(since if since is not None else settings.since_ts), BSKY_START)
    seen, tasks = set(), []
    for q in terms:
        if q.lower() not in seen:
            seen.add(q.lower())
            tasks.append(("bsky_search", {"q": q, "since": start, "until": None, "depth": 0}, 510, "bluesky"))
    return tasks


def web_url(uri: str) -> str | None:
    # at://did:plc:xxx/app.bsky.feed.post/rkey
    if not uri.startswith("at://"):
        return None
    parts = uri[5:].split("/")
    return f"https://bsky.app/profile/{parts[0]}/post/{parts[-1]}" if len(parts) >= 3 else None


def post_raw(post: dict, root_uri: str | None = None) -> RawItem | None:
    uri = post.get("uri") or ""
    url = web_url(uri)
    rec = post.get("record") or {}
    text = (rec.get("text") or "").strip()
    if not url or not text:
        return None
    reply = rec.get("reply") or {}
    root = root_uri or (reply.get("root") or {}).get("uri") or uri
    uid = "bsky:" + uri[5:].replace("/app.bsky.feed.post/", "/")
    return RawItem(uid=uid, source="bluesky", kind="comment" if reply else "post", url=url, text=text,
                   date=to_iso(rec.get("createdAt") or post.get("indexedAt")), thread_uid="bsky:" + root[5:].replace("/app.bsky.feed.post/", "/"),
                   parent_uid=("bsky:" + reply["parent"]["uri"][5:].replace("/app.bsky.feed.post/", "/")) if reply.get("parent") else None)


class BlueskyHandler(ApiHandler):
    TASK_TYPES = {"bsky_search", "bsky_thread"}
    PROVIDER = "bluesky"
    DOMAIN = "api.bsky.app"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.handle = os.environ.get("BSKY_HANDLE")
        self.password = os.environ.get("BSKY_APP_PASSWORD")
        self._jwt: str | None = None

    def _login(self) -> str | None:
        if not (self.handle and self.password):
            return None
        if self._jwt is None:
            data = self.http.post_json(AUTHED + "com.atproto.server.createSession",
                                       {"identifier": self.handle, "password": self.password}, domain="bsky.social")
            self._jwt = data.get("accessJwt")
        return self._jwt

    def _get(self, method: str, params: dict):
        try:
            return self.http.get_json(PUBLIC + method, params=params, domain=self.DOMAIN)
        except FetchError as err:
            if err.kind in ("forbidden", "rate_limited") and self.handle and self.password:
                jwt = self._login()
                if jwt:                                  # optional fallback: authenticated (higher limits)
                    return self.http.get_json(AUTHED + method, params=params, headers={"Authorization": f"Bearer {jwt}"},
                                              domain="bsky.social")
            raise

    def _run_bsky_search(self, p: dict, priority: int):
        q, since = p["q"], int(p["since"])
        until = int(p["until"]) if p.get("until") else int(self.clock()) + 86400
        self.stats.current_task = f"Bluesky: {q} ({_iso(since)[:10]}..{_iso(until)[:10]})"
        data = self._get("app.bsky.feed.searchPosts", {"q": q, "limit": PAGE, "sort": "latest",
                                                      "since": _iso(since), "until": _iso(until)})
        if not isinstance(data, dict) or "posts" not in data:
            raise FetchError("bad_json", "unexpected Bluesky payload")
        posts = data["posts"]
        raws, meta = [], {}
        for post in posts:
            r = post_raw(post)
            if r:
                raws.append(r)
                meta[r.uid] = post
        results = self.ingest(raws, "bsky_search")
        for raw, res in zip(raws, results):
            if res.status == "stored" and raw.kind == "post" and res.own_relevant and meta[raw.uid].get("replyCount"):
                self.db.add_task("bsky_thread", {"uri": meta[raw.uid]["uri"]}, 640, "bluesky")
        if len(posts) >= PAGE and until - since > 86400:          # a full page: there may be more, so halve the window
            mid = (since + until) // 2
            for lo, hi in ((since, mid + 1), (mid - 1, until)):
                self.db.add_task("bsky_search", {**p, "since": lo, "until": hi, "depth": int(p.get("depth", 0)) + 1},
                                 priority, "bluesky")
        newest = max((int(datetime.fromisoformat(r.date.replace("Z", "+00:00")).timestamp()) for r in raws if r.date), default=None)
        return len(posts), newest

    def _run_bsky_thread(self, p: dict, priority: int):
        self.stats.current_task = "Bluesky thread"
        data = self._get("app.bsky.feed.getPostThread", {"uri": p["uri"], "depth": 20, "parentHeight": 0})
        node = (data or {}).get("thread") or {}
        root_uri = (node.get("post") or {}).get("uri") or p["uri"]
        raws, stack = [], list(node.get("replies") or [])
        while stack:
            n = stack.pop()
            if not isinstance(n, dict) or "post" not in n:
                continue                                       # blocked / not found placeholders
            stack.extend(n.get("replies") or [])
            r = post_raw(n["post"], root_uri=root_uri)
            if r:
                raws.append(r)
        self.ingest(raws, "bsky_thread")
        self.db.mark_thread_expanded("bsky:" + root_uri[5:].replace("/app.bsky.feed.post/", "/"))
        return len(raws), None
