"""robots.txt compliance for HTML/PDF fetches. We obey it; there is no override."""
from __future__ import annotations

import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .http import FetchError, HttpClient

log = logging.getLogger("wbscraper.robots")


class RobotsCache:
    def __init__(self, http: HttpClient, user_agent_token: str = "waterboards-feedback-scraper"):
        self.http = http
        self.token = user_agent_token
        self._parsers: dict[str, RobotFileParser | None] = {}   # None = could not be determined -> disallow for now

    def _load(self, origin: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        try:
            res = self.http.get(origin + "/robots.txt", domain=urlparse(origin).hostname)
            rp.parse(res.text.splitlines())
            return rp
        except FetchError as err:
            if err.kind == "client" and err.status in (404, 410):    # no robots.txt: everything allowed
                rp.parse([])
                return rp
            log.warning("could not read robots.txt for %s (%s): skipping its pages for now", origin, err)
            return None

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._parsers or self._parsers[origin] is None:
            self._parsers[origin] = self._load(origin)
        rp = self._parsers[origin]
        return bool(rp and rp.can_fetch(self.token, url))

    def crawl_delay(self, url: str) -> float | None:
        p = urlparse(url)
        rp = self._parsers.get(f"{p.scheme}://{p.netloc}")
        return float(rp.crawl_delay(self.token) or 0) or None if rp else None
