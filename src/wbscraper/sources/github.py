"""GitHub issues and comments through the public REST API.

* the Water Boards' own data-team organisation (issues/comments written by non-members are public feedback on
  their data tools; OWNER/MEMBER-authored items are skipped as staff chatter - the association label is read
  transiently, never stored);
* global issue search for the distinctive system names (relevance filter removes GPS-app look-alikes).

Unauthenticated limits are tight (search 10/min, core 60/hour); `GITHUB_TOKEN` raises them (`gh auth token`).
"""
from __future__ import annotations

import os

from ..dates import to_iso
from ..http import FetchError
from .base import RawItem
from .handler_base import ApiHandler

API = "https://api.github.com"


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    from .. import queries
    cfg = lib.forums.get("github", {})
    tasks = [("gh_org_repos", {"org": org, "page": 1}, 640, "github") for org in cfg.get("orgs", [])]
    toks = queries.pullpush_tokens(lib)[: int(cfg.get("search_queries_limit", 40))]
    for q in toks:
        tasks.append(("gh_search", {"q": q, "page": 1}, 520, "github"))
    return tasks


class GitHubHandler(ApiHandler):
    TASK_TYPES = {"gh_org_repos", "gh_repo_issues", "gh_repo_comments", "gh_search", "gh_issue_comments"}
    PROVIDER = "github"
    DOMAIN = "api.github.com"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.token = os.environ.get("GITHUB_TOKEN")
        self.skip_assoc = {s.upper() for s in self.cfg.get("skip_associations", ["OWNER", "MEMBER"])}
        self.max_pages = int(self.cfg.get("max_pages", 3))

    def _get(self, url: str, params: dict | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            return self.http.get_json(url, params=params, headers=headers, domain=self.DOMAIN)
        except FetchError as err:
            if err.kind in ("forbidden", "rate_limited") and (err.retry_after or "rate limit" in err.body.lower()):
                wait = err.retry_after or 3600
                self.http.pacer.force_cooldown(self.DOMAIN, wait)
                raise FetchError("rate_limited", "GitHub rate limit reached", retry_after=wait) from err
            raise

    # -- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _repo_of(html_url: str) -> str:
        parts = html_url.split("github.com/", 1)[-1].split("/")
        return "/".join(parts[:2])

    def _issue_raw(self, it: dict, *, implicit: str = "") -> RawItem | None:
        if (it.get("author_association") or "").upper() in self.skip_assoc and implicit:
            return None
        url = it.get("html_url") or ""
        repo = self._repo_of(url)
        title = (it.get("title") or "").strip()
        body = (it.get("body") or "").strip()
        return RawItem(uid=f"gh:{repo}#{it['number']}", source="github", kind="post", url=url,
                       text=f"{title}\n\n{body}".strip() if body else title, date=to_iso(it.get("created_at")),
                       thread_uid=f"gh:{repo}#{it['number']}", community=repo, title=title, score_hint=implicit,
                       tier_cap="borderline" if implicit else "")

    def _comment_raw(self, c: dict, *, implicit: str = "") -> RawItem | None:
        if (c.get("author_association") or "").upper() in self.skip_assoc and implicit:
            return None
        url = c.get("html_url") or ""
        issue_url = c.get("issue_url") or ""
        number = issue_url.rstrip("/").rsplit("/", 1)[-1]
        repo = self._repo_of(url)
        return RawItem(uid=f"gh:{repo}#c{c['id']}", source="github", kind="comment", url=url,
                       text=(c.get("body") or "").strip(), date=to_iso(c.get("created_at")),
                       thread_uid=f"gh:{repo}#{number}", parent_uid=f"gh:{repo}#{number}", community=repo,
                       score_hint=implicit, tier_cap="borderline" if implicit else "")

    # -- tasks -----------------------------------------------------------------------------------
    def _run_gh_org_repos(self, p: dict, priority: int):
        org, page = p["org"], int(p.get("page", 1))
        self.stats.current_task = f"GitHub org {org}"
        repos = self._get(f"{API}/orgs/{org}/repos", {"per_page": 100, "page": page, "type": "public"})
        if not isinstance(repos, list):
            raise FetchError("bad_json", "unexpected GitHub repos payload")
        for r in repos:
            if r.get("archived") and not r.get("open_issues_count"):
                continue
            full = r["full_name"]
            self.db.add_task("gh_repo_issues", {"repo": full, "page": 1, "org": org}, 630, "github")
            self.db.add_task("gh_repo_comments", {"repo": full, "page": 1, "org": org}, 625, "github")
        if len(repos) >= 100:
            self.db.add_task("gh_org_repos", {**p, "page": page + 1}, priority, "github")
        return len(repos), None

    def _implicit(self, p: dict) -> str:
        return f"waterboards.ca.gov California Water Boards {p['repo']}" if p.get("org") else ""

    def _run_gh_repo_issues(self, p: dict, priority: int):
        repo, page = p["repo"], int(p.get("page", 1))
        self.stats.current_task = f"GitHub issues {repo} p{page}"
        items = self._get(f"{API}/repos/{repo}/issues",
                          {"state": "all", "per_page": 100, "page": page, "sort": "created", "direction": "desc"})
        if not isinstance(items, list):
            raise FetchError("bad_json", "unexpected GitHub issues payload")
        raws = [r for r in (self._issue_raw(i, implicit=self._implicit(p)) for i in items) if r]
        self.ingest(raws, "gh_repo_issues")
        if len(items) >= 100 and page < self.max_pages * 3:
            self.db.add_task("gh_repo_issues", {**p, "page": page + 1}, priority, "github")
        return len(items), None

    def _run_gh_repo_comments(self, p: dict, priority: int):
        repo, page = p["repo"], int(p.get("page", 1))
        self.stats.current_task = f"GitHub comments {repo} p{page}"
        items = self._get(f"{API}/repos/{repo}/issues/comments",
                          {"per_page": 100, "page": page, "sort": "created", "direction": "desc"})
        if not isinstance(items, list):
            raise FetchError("bad_json", "unexpected GitHub comments payload")
        raws = [r for r in (self._comment_raw(c, implicit=self._implicit(p)) for c in items) if r]
        self.ingest(raws, "gh_repo_comments")
        if len(items) >= 100 and page < self.max_pages * 3:
            self.db.add_task("gh_repo_comments", {**p, "page": page + 1}, priority, "github")
        return len(items), None

    def _run_gh_search(self, p: dict, priority: int):
        q, page = p["q"], int(p.get("page", 1))
        self.stats.current_task = f"GitHub search: {q} p{page}"
        data = self._get(f"{API}/search/issues", {"q": f'"{q}" in:title,body,comments', "per_page": 100, "page": page,
                                                  "sort": "created", "order": "desc"})
        if not isinstance(data, dict) or "items" not in data:
            raise FetchError("bad_json", "unexpected GitHub search payload")
        items = data["items"]
        raws, meta = [], {}
        for it in items:
            r = self._issue_raw(it)
            if r:
                raws.append(r)
                meta[r.uid] = it
        results = self.ingest(raws, "gh_search")
        for raw, res in zip(raws, results):
            it = meta[raw.uid]
            if res.status == "stored" and res.own_relevant and it.get("comments") and it.get("comments_url"):
                self.db.add_task("gh_issue_comments", {"url": it["comments_url"], "issue": raw.uid, "page": 1}, 600, "github")
        if len(items) >= 100 and page < self.max_pages:
            self.db.add_task("gh_search", {**p, "page": page + 1}, priority, "github")
        return len(items), None

    def _run_gh_issue_comments(self, p: dict, priority: int):
        page = int(p.get("page", 1))
        self.stats.current_task = f"GitHub issue comments p{page}"
        items = self._get(p["url"], {"per_page": 100, "page": page})
        if not isinstance(items, list):
            raise FetchError("bad_json", "unexpected GitHub comments payload")
        raws = [r for r in (self._comment_raw(c) for c in items) if r]
        self.ingest(raws, "gh_issue_comments")
        if len(items) >= 100 and page < self.max_pages:
            self.db.add_task("gh_issue_comments", {**p, "page": page + 1}, priority, "github")
        return len(items), None
