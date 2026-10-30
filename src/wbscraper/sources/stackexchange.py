"""Stack Exchange network (gis, engineering, earthscience, law, …) via the free public API (no key required).

Flow: `se_search` (search/excerpts finds questions *and* answers that mention a term) -> `se_questions`
(question bodies, all answers, all comments for the discovered questions). The API reports its own quota and
`backoff`; both are honoured.
"""
from __future__ import annotations

import os
from typing import Any

from ..htmltext import html_to_text
from ..http import FetchError
from ..dates import to_iso
from .base import RawItem
from .handler_base import ApiHandler

BASE = "https://api.stackexchange.com/2.3/"
SITE_DOMAINS = {
    "gis": "gis.stackexchange.com", "engineering": "engineering.stackexchange.com",
    "earthscience": "earthscience.stackexchange.com", "law": "law.stackexchange.com",
    "diy": "diy.stackexchange.com", "opendata": "opendata.stackexchange.com",
    "sustainability": "sustainability.stackexchange.com", "superuser": "superuser.com",
    "stackoverflow": "stackoverflow.com",
}


def site_domain(site: str) -> str:
    return SITE_DOMAINS.get(site, f"{site}.stackexchange.com")


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    cfg = lib.forums.get("stackexchange", {})
    sites = cfg.get("sites", [])
    qs = queries.pullpush_tokens(lib) + list(cfg.get("extra_queries", []))
    tasks = []
    for si, site in enumerate(sites):
        for qi, q in enumerate(qs):
            tasks.append(("se_search", {"site": site, "q": q, "page": 1}, 560 - 5 * si - (1 if qi > 20 else 0),
                          "stackexchange"))
    return tasks


class StackExchangeHandler(ApiHandler):
    TASK_TYPES = {"se_search", "se_questions"}
    PROVIDER = "stackexchange"
    DOMAIN = "api.stackexchange.com"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pagesize = int(self.cfg.get("pagesize", 50))
        self.max_pages = int(self.cfg.get("max_pages", 2))
        self.reserve = int(self.cfg.get("quota_reserve", 15))
        self.batch = int(self.cfg.get("question_batch", 25))
        self.key = os.environ.get("STACKEXCHANGE_KEY")

    # -- API access -----------------------------------------------------------------------
    def _api(self, path: str, site: str, **params) -> dict[str, Any]:
        params = {"site": site, **params}
        if self.key:
            params["key"] = self.key
        try:
            data = self.http.get_json(BASE + path, params=params, domain=self.DOMAIN)
        except FetchError as err:
            if err.kind == "client" and ("502" in err.body or "throttle" in err.body.lower()):
                self.http.pacer.force_cooldown(self.DOMAIN, 600)
                raise FetchError("rate_limited", "Stack Exchange throttle violation", retry_after=600) from err
            raise
        if not isinstance(data, dict) or "items" not in data:
            raise FetchError("bad_json", f"unexpected Stack Exchange payload for {path}")
        if data.get("backoff"):
            self.http.pacer.delay_until(self.DOMAIN, float(data["backoff"]))
        remaining = data.get("quota_remaining")
        if remaining is not None and remaining <= self.reserve:
            self.http.pacer.force_cooldown(self.DOMAIN, 6 * 3600)   # daily quota nearly used: resume later
            self.stats.last_error = f"Stack Exchange daily quota nearly exhausted ({remaining} left); pausing"
        return data

    # -- tasks --------------------------------------------------------------------------------
    def _run_se_search(self, p: dict, priority: int):
        site, q, page = p["site"], p["q"], int(p.get("page", 1))
        self.stats.current_task = f"{site}: {q} (p{page})"
        data = self._api("search/excerpts", site, q=q, order="desc", sort="relevance", page=page, pagesize=self.pagesize)
        items = data["items"]
        qids = []
        for it in items:
            qid = it.get("question_id")
            if qid and qid not in qids:
                qids.append(qid)
        fresh = [q_ for q_ in qids if not self.db.thread_row(f"se:{site}:q{q_}")]
        for i in range(0, len(fresh), self.batch):
            self.db.add_task("se_questions", {"site": site, "ids": sorted(fresh[i:i + self.batch])}, 700, "stackexchange")
        for q_ in fresh:
            self.db.upsert_thread(f"se:{site}:q{q_}", "stackexchange", discovered=True)
        self.stats.urls_discovered += len(fresh)
        if data.get("has_more") and page < self.max_pages and items:
            self.db.add_task("se_search", {**p, "page": page + 1}, priority, "stackexchange")
        newest = max((it.get("creation_date") or 0 for it in items), default=None)
        return len(items), newest or None

    def _fetch_all(self, path: str, site: str, **params) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 4):
            data = self._api(path, site, filter="withbody", pagesize=100, page=page, order="asc", sort="creation", **params)
            out.extend(data["items"])
            if not data.get("has_more"):
                break
        return out

    def _run_se_questions(self, p: dict, priority: int):
        site, ids = p["site"], list(p["ids"])
        dom = site_domain(site)
        self.stats.current_task = f"{site}: {len(ids)} question(s)"
        joined = ";".join(str(i) for i in ids)
        questions = self._fetch_all(f"questions/{joined}", site)
        answers = self._fetch_all(f"questions/{joined}/answers", site)
        q_comments = self._fetch_all(f"questions/{joined}/comments", site)
        a_comments: list[dict] = []
        aids = [a["answer_id"] for a in answers if a.get("answer_id")]
        for i in range(0, len(aids), 50):
            a_comments.extend(self._fetch_all(f"answers/{';'.join(str(x) for x in aids[i:i + 50])}/comments", site))

        post_to_q = {a["answer_id"]: a["question_id"] for a in answers}
        raws: list[RawItem] = []
        for q in questions:
            qid = q["question_id"]
            title = html_to_text(q.get("title"))
            raws.append(RawItem(uid=f"se:{site}:q{qid}", source="stackexchange", kind="post",
                                url=q.get("link") or f"https://{dom}/q/{qid}",
                                text=f"{title}\n\n{html_to_text(q.get('body'))}".strip(), date=to_iso(q.get("creation_date")),
                                thread_uid=f"se:{site}:q{qid}", community=site, title=title))
        for a in answers:
            qid, aid = a["question_id"], a["answer_id"]
            raws.append(RawItem(uid=f"se:{site}:a{aid}", source="stackexchange", kind="comment",
                                url=a.get("link") or f"https://{dom}/a/{aid}", text=html_to_text(a.get("body")),
                                date=to_iso(a.get("creation_date")), thread_uid=f"se:{site}:q{qid}",
                                parent_uid=f"se:{site}:q{qid}", community=site))
        for c in q_comments + a_comments:
            post_id = c.get("post_id")
            qid = post_to_q.get(post_id, post_id)
            cid = c["comment_id"]
            raws.append(RawItem(uid=f"se:{site}:c{cid}", source="stackexchange", kind="comment",
                                url=f"https://{dom}/q/{qid}#comment{cid}_{post_id}", text=html_to_text(c.get("body")),
                                date=to_iso(c.get("creation_date")), thread_uid=f"se:{site}:q{qid}", community=site))
        self.ingest(raws, "se_questions")
        for q in questions:
            self.db.mark_thread_expanded(f"se:{site}:q{q['question_id']}")
        return len(raws), None
