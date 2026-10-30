"""Polite HTTP: one shared client, per-domain pacing with jitter, retries with exponential backoff + full jitter,
Retry-After handling, response-size caps and per-domain cooldowns after repeated failures.

Nothing here tries to evade blocking. A 403/429 is treated as "back off and use another route".
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger("wbscraper.http")

# Failure kinds that count towards a domain cooldown.
COOLDOWN_KINDS = {"rate_limited", "server", "timeout", "network", "forbidden", "bad_json"}


class FetchError(Exception):
    """kind: rate_limited | server | timeout | network | forbidden | client | bad_json | too_large | query_timeout"""

    def __init__(self, kind: str, message: str = "", status: int | None = None,
                 retry_after: float | None = None, body: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.body = body

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limited", "server", "timeout", "network"}


@dataclass
class Fetched:
    status: int
    headers: dict[str, str]
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise FetchError("bad_json", f"invalid JSON from {self.url}: {exc}", status=self.status,
                             body=self.text[:200]) from exc


class DomainPacer:
    """Minimum interval between requests per domain, plus cooldowns."""

    def __init__(self, rates: Callable[[str], tuple[float, float]], clock=time.time, sleep=time.sleep,
                 rng: random.Random | None = None):
        self._rates = rates
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._next_ok: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._fails: dict[str, int] = {}

    def wait_needed(self, domain: str) -> float:
        now = self.clock()
        return max(0.0, self._next_ok.get(domain, 0.0) - now, self._cooldown_until.get(domain, 0.0) - now)

    def cooldown_remaining(self, domain: str) -> float:
        return max(0.0, self._cooldown_until.get(domain, 0.0) - self.clock())

    def acquire(self, domain: str) -> None:
        wait = self.wait_needed(domain)
        if wait > 0:
            self.sleep(wait)
        interval, jitter = self._rates(domain)
        self._next_ok[domain] = self.clock() + interval * (1.0 + self.rng.uniform(0.0, jitter))

    def delay_until(self, domain: str, seconds: float) -> None:
        self._next_ok[domain] = max(self._next_ok.get(domain, 0.0), self.clock() + seconds)

    def success(self, domain: str) -> None:
        self._fails[domain] = 0

    def failure(self, domain: str, kind: str, threshold: int, cooldown: float, retry_after: float | None) -> None:
        if kind not in COOLDOWN_KINDS:
            return
        self._fails[domain] = self._fails.get(domain, 0) + 1
        if retry_after:
            self.delay_until(domain, retry_after)
        if self._fails[domain] >= threshold:
            self._cooldown_until[domain] = self.clock() + cooldown
            self._fails[domain] = 0
            log.warning("domain %s cooling down for %.0fs after repeated failures", domain, cooldown)

    def force_cooldown(self, domain: str, seconds: float) -> None:
        self._cooldown_until[domain] = max(self._cooldown_until.get(domain, 0.0), self.clock() + seconds)


class HttpClient:
    def __init__(self, settings, clock=time.time, sleep=time.sleep, rng: random.Random | None = None,
                 client: httpx.Client | None = None):
        h = settings.get("http", {}) or {}
        self.user_agent = h.get("user_agent", "waterboards-feedback-scraper/0.1")
        self.max_retries = int(h.get("max_retries", 3))
        self.backoff_base = float(h.get("backoff_base", 2.0))
        self.backoff_max = float(h.get("backoff_max", 30))
        self.max_inline_retry_after = float(h.get("max_inline_retry_after", 60))
        self.max_bytes = int(h.get("max_response_bytes", 15_000_000))
        self.failure_threshold = int(h.get("failure_threshold", 4))
        self.cooldown_seconds = float(h.get("cooldown_seconds", 600))
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.pacer = DomainPacer(settings.domain_rate, clock=clock, sleep=sleep, rng=self.rng)
        timeout = httpx.Timeout(connect=float(h.get("connect_timeout", 10)), read=float(h.get("read_timeout", 30)),
                                write=10.0, pool=10.0)
        self._client = client or httpx.Client(headers={"User-Agent": self.user_agent, "Accept": "application/json, */*;q=0.5"},
                                              timeout=timeout, follow_redirects=True)
        self.request_count = 0
        self.failed_count = 0
        self.on_request: Callable[[str, bool], None] | None = None  # (domain, ok) hook for progress counters

    def close(self) -> None:
        self._client.close()

    # -- state used by the scheduler --------------------------------------------------------
    def wait_needed(self, domain: str) -> float:
        return self.pacer.wait_needed(domain)

    def cooldown_remaining(self, domain: str) -> float:
        return self.pacer.cooldown_remaining(domain)

    # -- for non-httpx providers (e.g. ddgs, PRAW) that still want pacing + cooldown ---------
    def acquire(self, domain: str) -> None:
        self.pacer.acquire(domain)

    def record_success(self, domain: str) -> None:
        self.pacer.success(domain)
        self.request_count += 1
        if self.on_request:
            self.on_request(domain, True)

    def record_failure(self, domain: str, err: FetchError) -> None:
        self.failed_count += 1
        self.request_count += 1
        self.pacer.failure(domain, err.kind, self.failure_threshold, self.cooldown_seconds, err.retry_after)
        if self.on_request:
            self.on_request(domain, False)

    # -- core -------------------------------------------------------------------------------
    @staticmethod
    def _retry_after(headers: httpx.Headers, now: float) -> float | None:
        ra = headers.get("retry-after")
        if ra:
            try:
                return max(0.0, float(ra))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(ra).timestamp() - now)
                except (TypeError, ValueError):
                    pass
        for h in ("x-ratelimit-reset", "ratelimit-reset"):
            v = headers.get(h)
            if v:
                try:
                    f = float(v)
                    return f if f < 1e9 else max(0.0, f - now)
                except ValueError:
                    pass
        return None

    def _once(self, url: str, params: dict | None, headers: dict | None, max_bytes: int | None = None,
              method: str = "GET", json_body=None) -> Fetched:
        cap = max_bytes or self.max_bytes
        try:
            with self._client.stream(method, url, params=params, headers=headers, json=json_body) as resp:
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > cap:
                        raise FetchError("too_large", f"response exceeds {cap} bytes", status=resp.status_code)
                    chunks.append(chunk)
                content = b"".join(chunks)
                status = resp.status_code
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                retry_after = self._retry_after(resp.headers, self.clock())
                final_url = str(resp.url)
        except httpx.TimeoutException as exc:
            raise FetchError("timeout", str(exc) or "timed out") from exc
        except httpx.TransportError as exc:
            raise FetchError("network", f"{type(exc).__name__}: {exc}") from exc
        body = content[:300].decode("utf-8", errors="replace")
        if 200 <= status < 300:
            return Fetched(status, hdrs, content, final_url)
        if status == 429:
            raise FetchError("rate_limited", f"HTTP 429 from {url}", status, retry_after, body)
        if status == 403:
            raise FetchError("forbidden", f"HTTP 403 from {url}", status, retry_after, body)
        if status >= 500:
            raise FetchError("server", f"HTTP {status} from {url}", status, retry_after, body)
        raise FetchError("client", f"HTTP {status} from {url}", status, retry_after, body)

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            domain: str | None = None, max_bytes: int | None = None, method: str = "GET", json_body=None) -> Fetched:
        domain = domain or (urlparse(url).hostname or "default")
        last: FetchError | None = None
        for attempt in range(self.max_retries + 1):
            self.pacer.acquire(domain)
            try:
                result = self._once(url, params, headers, max_bytes, method, json_body)
            except FetchError as err:
                last = err
                self.record_failure(domain, err)
                if not err.retryable or attempt >= self.max_retries:
                    raise
                if err.retry_after is not None and err.retry_after > self.max_inline_retry_after:
                    # Too long to sleep inline: let the scheduler move on to other work / another provider.
                    self.pacer.force_cooldown(domain, err.retry_after)
                    raise
                delay = err.retry_after if err.retry_after is not None else \
                    self.rng.uniform(0.0, min(self.backoff_max, self.backoff_base * (2 ** attempt)))
                log.info("retry %d/%d for %s in %.1fs (%s)", attempt + 1, self.max_retries, domain, delay, err.kind)
                self.sleep(delay)
                continue
            self.record_success(domain)
            return result
        assert last is not None
        raise last

    def get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                 domain: str | None = None) -> Any:
        domain = domain or (urlparse(url).hostname or "default")
        res = self.get(url, params=params, headers=headers, domain=domain)
        try:
            return res.json()
        except FetchError as err:
            self.record_failure(domain, err)
            raise

    def post_json(self, url: str, body: dict, *, headers: dict | None = None, domain: str | None = None):
        """POST a JSON body and parse the JSON reply (used only for optional logins such as Bluesky app passwords)."""
        domain = domain or (urlparse(url).hostname or "default")
        res = self.get(url, headers=headers, domain=domain, method="POST", json_body=body)
        return res.json()
