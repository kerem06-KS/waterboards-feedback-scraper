"""AI-assisted review of individual posts (`data/analysis/ai_review.csv`).

A reviewer (Claude, working inside a Claude Code session; no paid API) read each post against the project definitions and
recorded a label, task, pain point, confidence and a one-line explanation. This file is read on every `analyze` run, never
rewritten by it, so the classifications persist. Precedence when `analyze` builds the final columns:

    human override (review.csv)  >  AI review (this file)  >  API tag cache / VADER

Rows are keyed by source URL + text hash (the same key review.csv uses); a row whose post is no longer in the input is kept
in the file but not applied."""
from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import LABELS, AnalysisConfig

COLUMNS = ["key", "source_url", "ai_label", "ai_task", "ai_pain_point", "ai_confidence", "ai_uncertain",
           "ai_explanation", "reviewed_by", "reviewed_on"]
CONFIDENCE = ("high", "medium", "low")
NO_PAIN = ("", "none")


@dataclass
class AIReview:
    label: str
    task: str
    pain_point: str          # "" when no genuine website or system complaint
    confidence: str
    uncertain: bool
    explanation: str

    @property
    def has_pain(self) -> bool:
        return self.pain_point.strip().lower() not in NO_PAIN


@dataclass
class AIReviewReport:
    reviews: dict[str, AIReview] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _match(value: str, allowed: list[str]) -> str | None:
    v = (value or "").strip()
    for a in allowed:
        if a.lower() == v.lower():
            return a
    return None


def read_ai_reviews(path: Path, cfg: AnalysisConfig, current_keys: set[str]) -> AIReviewReport:
    rep = AIReviewReport()
    path = Path(path)
    if not path.exists():
        return rep
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            key = (row.get("key") or "").strip()
            label = _match(row.get("ai_label", ""), list(LABELS))
            task = _match(row.get("ai_task", ""), cfg.task_names)
            pain_raw = (row.get("ai_pain_point") or "").strip()
            pain = "" if pain_raw.lower() in NO_PAIN else _match(pain_raw, cfg.pain_point_names)
            conf = _match(row.get("ai_confidence", ""), list(CONFIDENCE))
            problems = []
            if label is None:
                problems.append(f"ai_label {row.get('ai_label')!r} is not one of {', '.join(LABELS)}")
            if task is None:
                problems.append(f"ai_task {row.get('ai_task')!r} is not in config/analysis/tags.yaml")
            if pain is None:
                problems.append(f"ai_pain_point {pain_raw!r} is not in config/analysis/tags.yaml")
            if conf is None:
                problems.append(f"ai_confidence {row.get('ai_confidence')!r} is not high/medium/low")
            if problems:
                rep.warnings.append(f"ai_review row {n}: " + "; ".join(problems) + " (row ignored)")
                continue
            if key not in current_keys:
                rep.warnings.append(f"ai_review row {n}: the post is no longer in the input; kept in the file, not applied")
                continue
            rep.reviews[key] = AIReview(label, task, pain, conf, (row.get("ai_uncertain") or "").strip().lower() in ("yes", "true", "1"),
                                        (row.get("ai_explanation") or "").strip())
    return rep


def write_ai_reviews(path: Path, rows: list[dict]) -> Path:
    """Used once by the review session that creates the file (and by tests); `analyze` itself never calls it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in COLUMNS})
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
