"""Shared fixtures for the analysis tests: config, scorer, and a fake Anthropic client."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from wbscraper.analysis.config import load_analysis_config
from wbscraper.analysis.sentiment import SentimentScorer
from wbscraper.terms import TermLibrary

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
_LIB = None


def lib() -> TermLibrary:
    global _LIB
    if _LIB is None:
        _LIB = TermLibrary.load(REPO_CONFIG)
    return _LIB


def cfg_for(tmp_path: Path, **overrides):
    return load_analysis_config(REPO_CONFIG, tmp_path / "data", overrides or None)


def scorer(tmp_path: Path, **kw) -> SentimentScorer:
    return SentimentScorer(cfg_for(tmp_path), lib(), **kw)


def write_input(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(["text", "date", "source_url"])
        w.writerows(rows)
    return path


def good_tag(task="Look up data, reports or records", pain="Online systems (e.g., SMARTS)", conf="high"):
    return {"task": task, "task_detail": "look up a case", "pain_point": pain, "confidence": conf, "rationale": "because"}


def response(payload: dict | None = None, *, stop_reason="end_turn", text: str | None = None):
    body = text if text is not None else json.dumps(payload or good_tag())
    usage = SimpleNamespace(input_tokens=1200, output_tokens=60, cache_creation_input_tokens=0, cache_read_input_tokens=900)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)], stop_reason=stop_reason, usage=usage)


class FakeClient:
    """Records every request; `reply` is a response, a list of responses, or a callable(request) -> response."""

    def __init__(self, reply=None, raises: Exception | None = None):
        self.reply, self.raises = reply, raises
        self.calls: list[tuple[str, dict]] = []
        self.messages = SimpleNamespace(create=lambda **kw: self._call("messages", kw))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self._call("beta", kw)))

    def _call(self, kind, kw):
        self.calls.append((kind, kw))
        if self.raises:
            raise self.raises
        r = self.reply
        if callable(r):
            return r(kw)
        if isinstance(r, list):
            return r[min(len(self.calls) - 1, len(r) - 1)]
        return r if r is not None else response()
