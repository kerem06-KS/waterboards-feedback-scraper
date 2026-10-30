"""Normalisation of Reddit records (from any provider) into RawItem. Author fields are never read."""
from __future__ import annotations

import re
from typing import Any

from ..dates import to_iso
from .base import RawItem

SOURCE = "reddit"
_ID = re.compile(r"^(?:t[1-3]_)?([a-z0-9]{2,10})$", re.I)
REDDIT_URL = re.compile(
    r"reddit\.com/(?:r/(?P<sub>[A-Za-z0-9_]+)/)?comments/(?P<post>[a-z0-9]{5,10})(?:/[^/\s?#]*(?:/(?P<comment>[a-z0-9]{5,10}))?)?",
    re.I)


def bare_id(value: str | None) -> str | None:
    if not value:
        return None
    m = _ID.match(str(value).strip())
    return m.group(1).lower() if m else None


def post_url(post_id: str, subreddit: str | None) -> str:
    return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/" if subreddit else \
        f"https://www.reddit.com/comments/{post_id}/"


def comment_url(post_id: str, comment_id: str, subreddit: str | None) -> str:
    return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/comment/{comment_id}/" if subreddit else \
        f"https://www.reddit.com/comments/{post_id}/comment/{comment_id}/"


def thread_uid(post_id: str) -> str:
    return f"reddit:t3_{post_id}"


def parse_reddit_url(url: str) -> dict[str, str | None] | None:
    m = REDDIT_URL.search(url or "")
    if not m:
        return None
    return {"subreddit": m.group("sub"), "post": m.group("post").lower(),
            "comment": (m.group("comment") or "").lower() or None}


def post_from_json(d: dict[str, Any]) -> RawItem | None:
    pid = bare_id(d.get("id"))
    if not pid:
        return None
    sub = d.get("subreddit")
    title = (d.get("title") or "").strip()
    body = d.get("selftext") or ""
    if body.strip().lower() in ("[removed]", "[deleted]"):
        body = ""
    text = f"{title}\n\n{body.strip()}".strip() if body.strip() else title
    link = str(d.get("url") or "")
    hint = "" if (not link or "reddit.com" in link or "redd.it" in link) else link
    return RawItem(
        uid=f"reddit:t3_{pid}", source=SOURCE, kind="post", url=post_url(pid, sub), text=text,
        date=to_iso(d.get("created_utc")), thread_uid=thread_uid(pid), community=sub, title=title,
        score=_num(d.get("score")), score_hint=hint)


def comment_from_json(d: dict[str, Any]) -> RawItem | None:
    cid = bare_id(d.get("id"))
    pid = bare_id(d.get("link_id"))
    if not cid or not pid:
        return None
    sub = d.get("subreddit")
    parent = str(d.get("parent_id") or "")
    parent_uid = f"reddit:{parent}" if re.match(r"^t[13]_", parent) else None
    return RawItem(
        uid=f"reddit:t1_{cid}", source=SOURCE, kind="comment", url=comment_url(pid, cid, sub),
        text=d.get("body") or "", date=to_iso(d.get("created_utc")), thread_uid=thread_uid(pid),
        parent_uid=parent_uid, community=sub, score=_num(d.get("score")))


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def created_ts(d: dict[str, Any]) -> int | None:
    try:
        return int(float(d.get("created_utc")))
    except (TypeError, ValueError):
        return None
