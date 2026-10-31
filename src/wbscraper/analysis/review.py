"""Manual review file: auto results for every post, flagged rows first, plus blank override columns for the user.

Overrides are keyed by source URL + text hash. Re-running `analyze` refreshes the automatic columns and keeps the
override columns; rows whose post no longer exists are kept at the bottom marked STALE and are never applied."""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import LABELS, AnalysisConfig

AUTO_COLUMNS = ["key", "review_reasons", "source", "date", "source_url", "score", "auto_label", "focus_sentences",
                "task", "task_detail", "pain_point", "tag_confidence", "rationale", "text"]
OVERRIDE_COLUMNS = ["label_override", "task_override", "pain_point_override", "note"]
COLUMNS = AUTO_COLUMNS + OVERRIDE_COLUMNS
STALE = "STALE: no matching post"


def post_key(url: str, text: str) -> str:
    return hashlib.sha1(f"{url}\n{text}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Override:
    label: str = ""
    task: str = ""
    pain_point: str = ""
    note: str = ""

    @property
    def empty(self) -> bool:
        return not (self.label or self.task or self.pain_point or self.note)


@dataclass
class OverrideReport:
    overrides: dict[str, Override] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stale: list[dict] = field(default_factory=list)       # whole old rows, kept in the rewritten file


def _norm(value: str, allowed: list[str]) -> str | None:
    v = (value or "").strip()
    if not v:
        return ""
    for a in allowed:
        if a.lower() == v.lower():
            return a
    return None


def read_overrides(path: Path, cfg: AnalysisConfig, current_keys: set[str]) -> OverrideReport:
    rep = OverrideReport()
    path = Path(path)
    if not path.exists():
        return rep
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            ov = Override()
            problems = []
            lab = _norm(row.get("label_override", ""), list(LABELS))
            task = _norm(row.get("task_override", ""), cfg.task_names)
            pain = _norm(row.get("pain_point_override", ""), cfg.pain_point_names)
            if lab is None:
                problems.append(f"label_override {row.get('label_override')!r} is not one of {', '.join(LABELS)}")
            if task is None:
                problems.append(f"task_override {row.get('task_override')!r} is not in config/analysis/tags.yaml")
            if pain is None:
                problems.append(f"pain_point_override {row.get('pain_point_override')!r} is not in config/analysis/tags.yaml")
            ov.label, ov.task, ov.pain_point = lab or "", task or "", pain or ""
            ov.note = (row.get("note") or "").strip()
            key = (row.get("key") or "").strip()
            for p in problems:
                rep.warnings.append(f"review row {n}: {p} (ignored)")
            if ov.empty:
                continue
            if key in current_keys:
                rep.overrides[key] = ov
            else:
                rep.warnings.append(f"review row {n}: the post for this override is no longer in the input; kept, not applied")
                rep.stale.append({c: row.get(c, "") for c in COLUMNS})
    return rep


def write_review(path: Path, rows: list[dict], stale: list[dict]) -> Path:
    """Flagged rows first (most reasons first), then the rest by date. A .bak copy of the previous file is kept."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".csv.bak"))
    ordered = sorted(rows, key=lambda r: (-len([x for x in r["review_reasons"].split("; ") if x]), r["date"] or "9999", r["source_url"]))
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:       # BOM: opens cleanly in Excel
            w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writeheader()
            for r in ordered:
                w.writerow({c: r.get(c, "") for c in COLUMNS})
            for r in stale:
                r = dict(r)
                r["review_reasons"] = STALE
                w.writerow({c: r.get(c, "") for c in COLUMNS})
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
