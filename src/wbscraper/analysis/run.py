"""Orchestrates one `wbscraper analyze` run: score, tag (opt-in), apply manual overrides, write the three outputs."""
from __future__ import annotations

import csv
import hashlib
import os
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..terms import TermLibrary
from .config import LABELS, AnalysisConfig
from .review import COLUMNS, Override, post_key, read_overrides, write_review
from .reviewed import read_ai_reviews
from .sentiment import SentimentScorer
from .summary import build_summary, source_name
from .tagging import UNCLASSIFIED, Tag, TagCache, Tagger, TaggingUnavailable, cache_key, make_client

# The first seven columns are unchanged from the earlier format. `sentiment_score` is always the original VADER score and
# `vader_label` the label that score gives; `sentiment_label` is the final label (human override > AI review > VADER).
SCORED_HEADER = ["text", "date", "source_url", "sentiment_score", "sentiment_label", "task", "pain_point",
                 "vader_label", "label_source", "tag_source", "confidence", "explanation", "needs_review"]


@dataclass
class RunResult:
    n: int = 0
    paths: dict[str, Path] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    flagged: int = 0
    reviewed: int = 0
    changed: int = 0
    ai_reviewed: int = 0
    warnings: list[str] = field(default_factory=list)
    tagging: dict[str, Any] = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)


def read_input(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        missing = {"text", "date", "source_url"} - set(rd.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing column(s): {', '.join(sorted(missing))}")
        return [{"text": r["text"], "date": r["date"], "source_url": r["source_url"]} for r in rd]


def _atomic_csv(path: Path, header: list[str], rows: list[list], *, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _hand_labels(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {r["text_sha1"]: (r.get("label") or "").strip().lower() for r in csv.DictReader(fh) if r.get("text_sha1")}


def analyze(cfg: AnalysisConfig, lib: TermLibrary, *, input_path: Path | None = None, out_path: Path | None = None,
            summary_path: Path | None = None, review_path: Path | None = None, tag: bool = False, retag: bool = False,
            dry_run: bool = False, client: Any = None, scorer: SentimentScorer | None = None) -> RunResult:
    inp = Path(input_path) if input_path else cfg.path("input")
    out = Path(out_path) if out_path else cfg.path("scored")
    summ = Path(summary_path) if summary_path else cfg.path("summary")
    rev = Path(review_path) if review_path else cfg.path("review")
    rows = read_input(inp)
    res = RunResult(n=len(rows))
    scorer = scorer or SentimentScorer(cfg, lib)
    cache = TagCache(cfg.path("tag_cache"))
    tagger: Tagger | None = None
    skip_msg = ""
    if tag or dry_run:
        try:
            tagger = Tagger(cfg, client if client is not None else (None if dry_run else make_client()), cache)
        except TaggingUnavailable as e:
            skip_msg = str(e)
    if dry_run:
        tagger = tagger or Tagger(cfg, None, cache)
        est = tagger.estimate([r["text"] for r in rows])
        est["approx_cost_usd"] = est["approx_input_tokens"] * float((cfg.tagging.get("price_per_mtok") or {}).get("input", 0)) / 1e6 \
            + est["to_tag"] * 150 * float((cfg.tagging.get("price_per_mtok") or {}).get("output", 0)) / 1e6
        res.tagging = {"dry_run": True, **est}
        return res
    if tag and tagger is None:
        res.warnings.append(f"Tagging skipped: {skip_msg}. Sentiment is still written; task and pain-point stay blank "
                            f"unless cached.")

    hand = _hand_labels(cfg.path("gold_relevant"))
    systems = [e for e in lib.entities if e.kind == "system"]
    auto: list[dict] = []
    for r in rows:
        s = scorer.score(r["text"], r["source_url"])
        t = Tag(source="none", task="", pain_point="")
        if tagger is not None and tag:
            t = tagger.tag(r["text"], source_name(r["source_url"]), r["date"], retag=retag)
        else:
            hit = cache.get(cache_key(r["text"], cfg))
            if hit is not None:
                t = hit
        low = r["text"].lower()
        sysnames = [e.label for e in systems if e.match(r["text"], low)[0] in ("strong", "weak_ctx")]
        auto.append({"row": r, "s": s, "tag": t, "systems": sysnames, "key": post_key(r["source_url"], r["text"])})
    if tagger is not None and tag:
        cache.save()
        res.tagging = {"calls": tagger.usage.calls, "refusals": tagger.usage.refusals, "errors": tagger.usage.errors,
                       "input_tokens": tagger.usage.input_tokens, "output_tokens": tagger.usage.output_tokens,
                       "cache_read_tokens": tagger.usage.cache_read, "cost_usd": round(tagger.usage.cost(cfg), 3)}

    keys = {a["key"] for a in auto}
    ov_rep = read_overrides(rev, cfg, keys)
    ai_rep = read_ai_reviews(cfg.path("ai_review"), cfg, keys)
    res.warnings += ov_rep.warnings + ai_rep.warnings
    res.ai_reviewed = len(ai_rep.reviews)

    items, review_rows, scored_rows = [], [], []
    for a in auto:
        r, s, t = a["row"], a["s"], a["tag"]
        ov = ov_rep.overrides.get(a["key"], Override())
        ai = ai_rep.reviews.get(a["key"])
        has_tag = t.source != "none"

        # precedence: human override > AI review > API tag / VADER
        if ov.label:
            label, label_src = ov.label, "human_override"
        elif ai:
            label, label_src = ai.label, "ai_review"
        else:
            label, label_src = s.label, "vader"
        if ov.task:
            task, task_src = ov.task, "human_override"
        elif ai:
            task, task_src = ai.task, "ai_review"
        elif has_tag:
            task, task_src = t.task, "api_tag"
        else:
            task, task_src = "", "none"
        if ov.pain_point:
            pain, pain_src = ov.pain_point, "human_override"
        elif ai:
            pain, pain_src = (ai.pain_point if ai.has_pain else ""), "ai_review"
        elif has_tag and label == "negative":
            pain, pain_src = t.pain_point, "api_tag"
        else:
            pain, pain_src = "", "none"
        tag_src = next((x for x in ("human_override", "ai_review", "api_tag") if x in (task_src, pain_src)), "none")
        if label != s.label:
            res.changed += 1
        if not ov.empty:
            res.reviewed += 1

        confidence = ai.confidence if ai else (t.confidence if has_tag else "")
        explanation = ai.explanation if ai else (t.rationale if has_tag else "")
        reasons = list(s.review_reasons)
        h = hand.get(hashlib.sha1(r["text"].encode()).hexdigest()[:12])
        if h and h != s.label and not ai:
            reasons.append(f"hand label is {h}, scorer says {s.label}")
        if ai:
            if ai.uncertain or ai.confidence == "low":
                reasons.append("AI review is uncertain")
            if ai.label != s.label:
                reasons.append(f"VADER says {s.label}, reviewed label is {ai.label}")
        elif has_tag:
            if t.confidence == "low":
                reasons.append("low tag confidence")
            if t.task in ("Other", UNCLASSIFIED):
                reasons.append(f"task is {t.task}")
            if t.pain_point != "None" and label != "negative" and t.pain_point != "Other / not a website problem":
                reasons.append(f"tagger sees a problem ({t.pain_point}) but label is {label}")
        if not ov.empty:
            needs_review = ""                                    # a person has already looked at it
        elif ai is not None:
            needs_review = "yes" if (ai.uncertain or ai.confidence == "low") else ""
        else:
            needs_review = "yes" if reasons else ""
        src = source_name(r["source_url"])
        items.append({"source": src, "label": label, "score": s.score, "task": task, "pain_point": pain, "systems": a["systems"],
                      "text": r["text"], "date": r["date"], "source_url": r["source_url"], "label_source": label_src})
        review_rows.append({"key": a["key"], "review_reasons": "; ".join(reasons), "source": src, "date": r["date"],
                            "source_url": r["source_url"], "score": f"{s.score:+.3f}", "auto_label": s.label,
                            "focus_sentences": " | ".join(s.focus)[:1500], "task": t.task if has_tag else "",
                            "task_detail": t.task_detail, "pain_point": t.pain_point if has_tag else "",
                            "tag_confidence": t.confidence if has_tag else "", "rationale": t.rationale,
                            "ai_label": ai.label if ai else "", "ai_task": ai.task if ai else "",
                            "ai_pain_point": (ai.pain_point if ai else ""), "ai_confidence": ai.confidence if ai else "",
                            "ai_uncertain": ("yes" if ai.uncertain else "") if ai else "", "ai_explanation": ai.explanation if ai else "",
                            "final_label": label, "final_task": task, "final_pain_point": pain,
                            "label_source": label_src, "tag_source": tag_src, "text": r["text"],
                            "label_override": ov.label, "task_override": ov.task,
                            "pain_point_override": ov.pain_point, "note": ov.note})
        scored_rows.append([r["text"], r["date"], r["source_url"], f"{s.score:.4f}", label, task, pain,
                            s.label, label_src, tag_src, confidence, explanation, needs_review])

    res.flagged = sum(1 for r in review_rows if r["review_reasons"])
    res.counts = {lab: sum(1 for i in items if i["label"] == lab) for lab in LABELS}
    res.items = items
    _atomic_csv(out, SCORED_HEADER, scored_rows)
    write_review(rev, review_rows, ov_rep.stale)
    note = (f"Backend `{scorer.backend_name}`, negative <= {scorer.t_neg}, positive >= {scorer.t_pos} (VADER, kept in `sentiment_score` "
            f"and `vader_label`). Final labels follow human overrides, then the AI-assisted review, then VADER. Accuracy of the VADER "
            f"stage against hand labels is in `docs/SENTIMENT.md`; with so few negative posts it is indicative, not precise.")
    tagged = any(i["task"] for i in items)
    sources = Counter(i["label_source"] for i in items)
    summ.parent.mkdir(parents=True, exist_ok=True)
    summ.write_text(build_summary(items, accuracy_note=note, reviewed=res.reviewed, changed=res.changed, tagged=tagged,
                                  label_sources=dict(sources), date=time.strftime("%Y-%m-%d")), encoding="utf-8")
    res.paths = {"scored": out, "summary": summ, "review": rev}
    return res
