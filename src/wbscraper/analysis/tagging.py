"""Task and pain-point tags from the Claude API (opt-in: `wbscraper analyze --tag`).

Every post gets a task (what the person came to do or is advising others to do). The same call also names the main
problem described, if any; the scored CSV shows it only for negative posts, so turning a post negative during manual
review needs no second call. Results are cached by text hash + prompt version + model, so re-runs are free and repeatable.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import AnalysisConfig

UNCLASSIFIED = "Unclassified"
MAX_POST_CHARS = 6000


@dataclass
class Tag:
    task: str = UNCLASSIFIED
    task_detail: str = ""
    pain_point: str = "None"
    confidence: str = "low"
    rationale: str = ""
    source: str = "api"           # api | cache | none


class TaggingUnavailable(RuntimeError):
    """No API key / SDK; sentiment output is still written and the tag columns stay blank."""


def system_prompt(cfg: AnalysisConfig) -> str:
    tasks = "\n".join(f"- {t['name']}: {t['description']}" for t in cfg.tasks)
    pains = "\n".join(f"- {p['name']}: {p['description']}" for p in cfg.pain_points)
    return f"""You tag public posts that mention California Water Boards websites and online systems (SMARTS, GeoTracker, CIWQS, OCIS, CalWATRS/eWRIMS, Drinking Water Watch, eAR, FAAST, CEDEN, GAMA, ELAP and similar).

For each post return:
1. task: what the person came to the site or system to do. If they are advising someone else ("check GeoTracker before you buy"), use the task they recommend. Choose exactly one from:
{tasks}
2. task_detail: a short phrase (under 12 words) naming the specific action, for example "renew wastewater operator certificate" or "look up cleanup cases near a house".
3. pain_point: why it went badly, from the post's own words. Choose exactly one from:
{pains}
   Use "None" when the post describes no problem. Use "Other / not a website problem" when the problem is the regulation, a fee, or the contamination itself rather than a website, system or service.
4. confidence: high, medium or low, for the pair of tags.
5. rationale: one sentence quoting or paraphrasing the evidence.

Judge only from the post text. Do not guess at details the post does not give. Posts may be long documents or comment letters: tag the task the author is concerned with."""


def response_schema(cfg: AnalysisConfig) -> dict:
    return {
        "type": "object",
        "properties": {
            "task": {"type": "string", "enum": cfg.task_names},
            "task_detail": {"type": "string"},
            "pain_point": {"type": "string", "enum": cfg.pain_point_names},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "rationale": {"type": "string"},
        },
        "required": ["task", "task_detail", "pain_point", "confidence", "rationale"],
        "additionalProperties": False,
    }


def cache_key(text: str, cfg: AnalysisConfig) -> str:
    t = cfg.tagging
    raw = f"{t.get('prompt_version', 1)}|{t.get('model')}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class TagCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.data = {}

    def get(self, key: str) -> Tag | None:
        d = self.data.get(key)
        if not d:
            return None
        return Tag(**{**d, "source": "cache"})

    def put(self, key: str, tag: Tag) -> None:
        self.data[key] = asdict(tag)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    refusals: int = 0
    errors: int = 0

    def add(self, u: Any) -> None:
        self.input_tokens += int(getattr(u, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(u, "output_tokens", 0) or 0)
        self.cache_write += int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.cache_read += int(getattr(u, "cache_read_input_tokens", 0) or 0)

    def cost(self, cfg: AnalysisConfig) -> float:
        p = cfg.tagging.get("price_per_mtok") or {}
        return (self.input_tokens * p.get("input", 0) + self.output_tokens * p.get("output", 0)
                + self.cache_write * p.get("cache_write", 0) + self.cache_read * p.get("cache_read", 0)) / 1e6


def make_client():
    """The real Anthropic client. Credentials come from ANTHROPIC_API_KEY (or an `ant auth login` profile)."""
    try:
        import anthropic
    except ImportError as e:
        raise TaggingUnavailable("the 'anthropic' package is not installed (pip install -e '.[analysis]')") from e
    client = anthropic.Anthropic()
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        # `ant auth login` profiles are resolved by the SDK itself; only fail early when nothing at all is configured.
        profile_dir = Path.home() / ".config" / "anthropic"
        if not profile_dir.exists():
            raise TaggingUnavailable("no ANTHROPIC_API_KEY set (add it to .env or your environment)")
    return client


class Tagger:
    def __init__(self, cfg: AnalysisConfig, client: Any, cache: TagCache):
        self.cfg, self.client, self.cache = cfg, client, cache
        self.usage = Usage()
        self._system = system_prompt(cfg)
        self._schema = response_schema(cfg)

    def request(self, text: str, source: str = "", date: str = "") -> dict:
        t = self.cfg.tagging
        body = text if len(text) <= MAX_POST_CHARS else text[:MAX_POST_CHARS] + "\n[... truncated for tagging]"
        user = f"Source: {source or 'unknown'}\nDate: {date or 'unknown'}\n\nPost:\n{body}"
        req: dict = {
            "model": t.get("model", "claude-opus-5-5"),
            "max_tokens": int(t.get("max_tokens", 4000)),
            "system": [{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": t.get("effort", "medium"),
                              "format": {"type": "json_schema", "schema": self._schema}},
        }
        return req

    def _create(self, req: dict):
        fb = str(self.cfg.tagging.get("fallbacks", "default")).lower()
        if fb in ("off", "false", "none", ""):
            return self.client.messages.create(**req)
        return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **req)

    def _parse(self, resp) -> Tag:
        if getattr(resp, "stop_reason", None) == "refusal":
            self.usage.refusals += 1
            return Tag(UNCLASSIFIED, "", "None", "low", "The model declined to tag this post.", "api")
        block = next((b for b in resp.content if getattr(b, "type", "") == "text"), None)
        if block is None:
            raise ValueError("no text block in response")
        d = json.loads(block.text)
        task = d.get("task") if d.get("task") in self.cfg.task_names else UNCLASSIFIED
        pain = d.get("pain_point") if d.get("pain_point") in self.cfg.pain_point_names else "None"
        return Tag(task, str(d.get("task_detail", ""))[:200], pain,
                   d.get("confidence") if d.get("confidence") in ("high", "medium", "low") else "low",
                   str(d.get("rationale", ""))[:400], "api")

    def tag(self, text: str, source: str = "", date: str = "", *, retag: bool = False) -> Tag:
        key = cache_key(text, self.cfg)
        if not retag:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        try:
            resp = self._create(self.request(text, source, date))
            self.usage.calls += 1
            self.usage.add(getattr(resp, "usage", None))
            tag = self._parse(resp)
        except Exception as e:  # one bad post must not stop the run; it is retried on the next `--tag`
            self.usage.errors += 1
            return Tag(UNCLASSIFIED, "", "None", "low", f"tagging failed: {type(e).__name__}", "error")
        if tag.task != UNCLASSIFIED:          # refusals are not cached, so the next `--tag` run tries again
            self.cache.put(key, tag)
        return tag

    def estimate(self, texts: list[str]) -> dict:
        """Posts that would be sent, from the cache state alone (no network)."""
        pending = [t for t in texts if self.cache.get(cache_key(t, self.cfg)) is None]
        return {"total": len(texts), "cached": len(texts) - len(pending), "to_tag": len(pending),
                "approx_input_tokens": sum(min(len(t), MAX_POST_CHARS) // 4 + 60 for t in pending) + len(self._system) // 4}
