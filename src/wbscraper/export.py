"""CSV export: exactly three columns - text, date, source_url. Metadata goes to a separate manifest file."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__

HEADER = ["text", "date", "source_url"]


@dataclass
class ExportResult:
    path: Path
    rows: int
    included_tiers: tuple[str, ...]


def export_csv(db, path: str | Path, *, include_borderline: bool = False, min_chars: int = 12,
               excel: bool = False) -> ExportResult:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tiers = ("relevant", "borderline") if include_borderline else ("relevant",)
    marks = ",".join("?" * len(tiers))
    rows = db.conn.execute(
        f"SELECT text, date, url FROM items WHERE tier IN ({marks}) AND length(text) >= ? "
        f"AND COALESCE(flags,'') NOT LIKE '%agency_doc%' ORDER BY COALESCE(date,'9999'), url",
        (*tiers, min_chars)).fetchall()
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig" if excel else "utf-8", newline="") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(HEADER)
            for r in rows:
                w.writerow([r["text"], r["date"] or "", r["url"]])
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return ExportResult(path, len(rows), tiers)


REVIEW_HEADER = ["text", "date", "source_url", "review_note"]


def export_review_csv(db, path: str | Path, *, min_chars: int = 12) -> int:
    """Borderline items for manual review, kept apart from the deliverable CSV. This is a local aid: it adds a
    `review_note` column (which rule fired), so it is never the file to hand over. Returns the number of rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = db.conn.execute(
        "SELECT text, date, url, reasons FROM items WHERE tier='borderline' AND length(text) >= ? "
        "AND COALESCE(flags,'') NOT LIKE '%agency_doc%' ORDER BY COALESCE(date,'9999'), url", (min_chars,)).fetchall()
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(REVIEW_HEADER)
            for r in rows:
                try:
                    note = "; ".join(json.loads(r["reasons"] or "[]"))
                except ValueError:
                    note = ""
                w.writerow([r["text"], r["date"] or "", r["url"], note])
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return len(rows)


def write_manifest(db, settings, csv_result: ExportResult, term_files: list[Path]) -> Path:
    """Reproducible record of collection settings; kept apart from the CSV so the CSV stays three columns."""
    hashes = {}
    for f in term_files:
        if f.exists():
            hashes[str(f.name)] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    sessions = [dict(r) for r in db.conn.execute(
        "SELECT id, started_at, ended_at, runtime_seconds, stop_reason FROM sessions ORDER BY id")]
    manifest = {
        "tool_version": __version__,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": platform.python_version(),
        "csv": {"path": str(csv_result.path), "rows": csv_result.rows, "tiers": list(csv_result.included_tiers),
                "columns": HEADER},
        "item_counts_by_tier": db.tier_counts(),
        "settings": settings.raw,
        "term_file_hashes": hashes,
        "sessions": sessions,
    }
    out = csv_result.path.parent / "run_manifest.json"
    out.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return out
