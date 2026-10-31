"""Accuracy checks and threshold tuning for the sentiment scorer (`wbscraper analyze-eval`).

Kept deliberately small because the labelled sets are small:
  * only the two label cutoffs are tuned, on a coarse 0.05 grid, on the tuning set only
  * each cutoff comes from the middle of the range whose cross-validated macro-F1 is within 0.01 of the best,
    and VADER's default (+/-0.05) is kept when it is inside that range
  * the 71 relevant posts are never used for tuning; they are scored once with the locked settings
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..terms import TermLibrary
from .config import LABELS, AnalysisConfig
from .sentiment import SentimentScorer, label_for

GRID_NEG = [round(-0.05 * i, 2) for i in range(1, 13)]      # -0.05 .. -0.60
GRID_POS = [round(0.05 * i, 2) for i in range(1, 15)]       # 0.05 .. 0.70
DEFAULT = (-0.05, 0.05)
TOLERANCE = 0.01


@dataclass
class GoldItem:
    id: str
    text: str
    label: str
    clear: bool
    source_url: str = ""


def load_gold(path: Path) -> list[GoldItem]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        lab = (r.get("label") or "").strip().lower()
        if lab not in LABELS:
            continue
        out.append(GoldItem(r.get("id", ""), r["text"], lab, (r.get("clarity") or "clear").strip() == "clear",
                            r.get("source_url", "")))
    return out


# -- metrics -------------------------------------------------------------------------------------------------
def metrics(gold: list[str], pred: list[str]) -> dict:
    n = len(gold)
    per = {}
    for c in LABELS:
        tp = sum(1 for g, p in zip(gold, pred) if g == c and p == c)
        fp = sum(1 for g, p in zip(gold, pred) if g != c and p == c)
        fn = sum(1 for g, p in zip(gold, pred) if g == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[c] = {"precision": prec, "recall": rec, "f1": f1, "support": tp + fn, "predicted": tp + fp}
    present = [c for c in LABELS if per[c]["support"] or per[c]["predicted"]]
    macro = statistics.fmean(per[c]["f1"] for c in present) if present else 0.0
    conf = {g: {p: sum(1 for a, b in zip(gold, pred) if a == g and b == p) for p in LABELS} for g in LABELS}
    acc = sum(1 for g, p in zip(gold, pred) if g == p) / n if n else 0.0
    return {"n": n, "accuracy": acc, "macro_f1": macro, "per_class": per, "confusion": conf}


def macro_f1(gold: list[str], pred: list[str]) -> float:
    return metrics(gold, pred)["macro_f1"]


def bootstrap_ci(gold: list[str], pred: list[str], fn: Callable[[list[str], list[str]], float],
                 n: int = 2000, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    idx = range(len(gold))
    vals = []
    for _ in range(n):
        sample = [rng.choice(idx) for _ in idx]
        vals.append(fn([gold[i] for i in sample], [pred[i] for i in sample]))
    vals.sort()
    return vals[int(0.025 * n)], vals[min(n - 1, int(0.975 * n))]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


# -- thresholds --------------------------------------------------------------------------------------------
def predict(scores: list[float], t: tuple[float, float]) -> list[str]:
    return [label_for(s, t[0], t[1]) for s in scores]


def threshold_table(scores: list[float], gold: list[str]) -> dict[tuple[float, float], float]:
    return {(tn, tp): macro_f1(gold, predict(scores, (tn, tp))) for tn in GRID_NEG for tp in GRID_POS}


def choose_thresholds(scores: list[float], gold: list[str]) -> tuple[tuple[float, float], dict]:
    """Middle of the near-best plateau; VADER's default when it is on the plateau."""
    table = threshold_table(scores, gold)
    best = max(table.values())
    plateau = [k for k, v in table.items() if v >= best - TOLERANCE]
    if DEFAULT in plateau:
        return DEFAULT, {"best": best, "plateau": plateau, "rule": "default kept (inside plateau)"}
    neg_vals = sorted({k[0] for k in plateau})
    pos_vals = sorted({k[1] for k in plateau})
    centre = (statistics.median(neg_vals), statistics.median(pos_vals))
    pick = min(plateau, key=lambda k: (abs(k[0] - centre[0]) + abs(k[1] - centre[1]), -table[k]))
    return pick, {"best": best, "plateau": plateau, "rule": "middle of plateau"}


def stratified_folds(labels: list[str], k: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    folds: list[list[int]] = [[] for _ in range(k)]
    for c in LABELS:
        idx = [i for i, lab in enumerate(labels) if lab == c]
        rng.shuffle(idx)
        for j, i in enumerate(idx):
            folds[j % k].append(i)
    return folds


def cross_validate(scores: list[float], gold: list[str], k: int = 5, repeats: int = 5) -> dict:
    """Out-of-fold macro-F1 when the cutoffs are chosen on the other folds (the tuning procedure's own accuracy)."""
    per_repeat, picks = [], []
    for r in range(repeats):
        pred = [""] * len(gold)
        for fold in stratified_folds(gold, k, seed=100 + r):
            held = set(fold)
            tr = [i for i in range(len(gold)) if i not in held]
            t, _ = choose_thresholds([scores[i] for i in tr], [gold[i] for i in tr])
            picks.append(t)
            for i in fold:
                pred[i] = label_for(scores[i], *t)
        per_repeat.append(metrics(gold, pred))
    return {"macro_f1": statistics.fmean(m["macro_f1"] for m in per_repeat),
            "accuracy": statistics.fmean(m["accuracy"] for m in per_repeat),
            "macro_f1_range": (min(m["macro_f1"] for m in per_repeat), max(m["macro_f1"] for m in per_repeat)),
            "threshold_picks": picks}


# -- the evaluation run ------------------------------------------------------------------------------------
def _scores(scorer: SentimentScorer, items: Iterable[GoldItem]) -> list[float]:
    return [scorer.score(it.text).score for it in items]


def evaluate(cfg: AnalysisConfig, lib: TermLibrary, relevant: list[GoldItem], tuning: list[GoldItem],
             *, backends: tuple[str, ...] = ("nltk-vader-sentiment", "vaderSentiment"),
             ablate: bool = True) -> dict:
    gold_t = [g.label for g in tuning]
    gold_r = [g.label for g in relevant]
    clear_r = [i for i, g in enumerate(relevant) if g.clear]
    main_backend = cfg.sentiment.get("backend", "nltk-vader-sentiment")
    rows = []

    def row(name: str, pred_t: list[str], pred_r: list[str], cv: dict | None = None, thresholds=None):
        mr = metrics(gold_r, pred_r)
        mc = metrics([gold_r[i] for i in clear_r], [pred_r[i] for i in clear_r])
        rows.append({
            "method": name, "thresholds": thresholds,
            "tuning": metrics(gold_t, pred_t), "tuning_cv": cv,
            "relevant": mr, "relevant_clear": mc,
            "relevant_macro_f1_ci": bootstrap_ci(gold_r, pred_r, macro_f1),
            "relevant_accuracy_ci": bootstrap_ci(gold_r, pred_r, lambda g, p: sum(a == b for a, b in zip(g, p)) / len(g)),
        })

    row("Always neutral (baseline)", ["neutral"] * len(tuning), ["neutral"] * len(relevant))
    locked = None
    for be in backends:
        plain = SentimentScorer(cfg, lib, backend=be, thresholds=DEFAULT, plain=True)
        row(f"Default VADER, whole text, ±0.05 [{be}]", predict(_scores(plain, tuning), DEFAULT),
            predict(_scores(plain, relevant), DEFAULT), thresholds=DEFAULT)
        ours = SentimentScorer(cfg, lib, backend=be, thresholds=DEFAULT)
        st, sr = _scores(ours, tuning), _scores(ours, relevant)
        row(f"This scorer, ±0.05 [{be}]", predict(st, DEFAULT), predict(sr, DEFAULT), thresholds=DEFAULT)
        t, info = choose_thresholds(st, gold_t)
        cv = cross_validate(st, gold_t)
        row(f"This scorer, tuned cutoffs [{be}]", predict(st, t), predict(sr, t), cv=cv, thresholds=t)
        if be == main_backend:
            locked = {"backend": be, "thresholds": t, "rule": info["rule"], "best_tuning_f1": info["best"],
                      "plateau_size": len(info["plateau"]), "cv": cv,
                      "tuning_scores": st, "relevant_scores": sr}

    ablation = []
    if ablate and locked:
        t = locked["thresholds"]
        variants = [("all rules (locked)", {}), ("no cleaning", {"use_clean": False}), ("no focus (whole post)", {"use_focus": False})]
        for g in list((cfg.lexicon.get("neutral_words") or {})) + list((cfg.lexicon.get("added_words") or {})):
            variants.append((f"without {g}", {"groups_off": [g]}))
        variants += [("without phrase rules", {"groups_off": ["phrases"]}), ("without polite-phrase removal", {"groups_off": ["polite_phrases"]})]
        for name, kw in variants:
            sc = SentimentScorer(cfg, lib, backend=main_backend, thresholds=t, **kw)
            m = metrics(gold_t, predict(_scores(sc, tuning), t))
            ablation.append({"variant": name, "macro_f1": m["macro_f1"], "negative_f1": m["per_class"]["negative"]["f1"],
                             "positive_f1": m["per_class"]["positive"]["f1"]})
    return {"rows": rows, "locked": locked, "ablation": ablation,
            "sizes": {"relevant": len(relevant), "relevant_clear": len(clear_r), "tuning": len(tuning),
                      "relevant_labels": {c: gold_r.count(c) for c in LABELS},
                      "tuning_labels": {c: gold_t.count(c) for c in LABELS}}}


def icwsm_check(cfg: AnalysisConfig, lib: TermLibrary, folder: Path) -> list[dict]:
    """Correlation with the human ratings published with VADER (Hutto & Gilbert 2014), whole-text scoring.
    Files: tweets_GroundTruth.txt etc. (id <tab> mean rating <tab> text)."""
    out = []
    scorers = {
        "vaderSentiment 3.3.2 (default)": SentimentScorer(cfg, lib, backend="vaderSentiment", thresholds=DEFAULT, plain=True),
        "nltk-vader-sentiment (default)": SentimentScorer(cfg, lib, backend="nltk-vader-sentiment", thresholds=DEFAULT, plain=True),
        "nltk-vader-sentiment + domain word list": SentimentScorer(cfg, None, backend="nltk-vader-sentiment",
                                                                  thresholds=DEFAULT, use_focus=False, use_clean=False),
    }
    for f in sorted(Path(folder).glob("*_GroundTruth.txt")):
        human, texts = [], []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    human.append(float(parts[1]))
                except ValueError:
                    continue
                texts.append("\t".join(parts[2:]))
        rec = {"dataset": f.name.replace("_GroundTruth.txt", ""), "n": len(texts)}
        for name, sc in scorers.items():
            vals = [sc.compound(t) if not sc.plain else sc.score(t).score for t in texts]
            rec[name] = statistics.correlation(human, vals) if len(texts) > 2 else float("nan")
        out.append(rec)
    return out


# -- report ------------------------------------------------------------------------------------------------
def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def render_report(res: dict, icwsm: list[dict] | None = None, date: str = "") -> str:
    s = res["sizes"]
    L = res["locked"]
    lines = [f"# Sentiment accuracy report{(' (' + date + ')') if date else ''}", "",
             f"Hand-labelled sets: {s['relevant']} relevant posts (evaluation only; {s['relevant_clear']} with a clear label) "
             f"and {s['tuning']} tuning posts. Labels: relevant {s['relevant_labels']}, tuning {s['tuning_labels']}.", ""]
    if L:
        t = L["thresholds"]
        lines += [f"**Locked settings:** backend `{L['backend']}`, negative ≤ {t[0]}, positive ≥ {t[1]} "
                  f"({L['rule']}; best tuning macro-F1 {L['best_tuning_f1']:.2f}, {L['plateau_size']} grid points within 0.01). "
                  f"Cross-validated macro-F1 of the tuning procedure on the tuning set: {L['cv']['macro_f1']:.2f} "
                  f"(range {L['cv']['macro_f1_range'][0]:.2f}–{L['cv']['macro_f1_range'][1]:.2f}).", ""]
    lines += ["## Comparison", "",
              "| Method | Cutoffs | Relevant: accuracy | Relevant: macro-F1 (95% CI) | Relevant: negative P / R | Relevant clear-only: macro-F1 | Tuning: macro-F1 |",
              "|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        rel, ci = r["relevant"], r["relevant_macro_f1_ci"]
        neg = rel["per_class"]["negative"]
        th = f"{r['thresholds'][0]} / {r['thresholds'][1]}" if r["thresholds"] else "—"
        tun = r["tuning"]["macro_f1"]
        cv = f" (CV {r['tuning_cv']['macro_f1']:.2f})" if r.get("tuning_cv") else ""
        lines.append(f"| {r['method']} | {th} | {_pct(rel['accuracy'])} | {rel['macro_f1']:.2f} ({ci[0]:.2f}–{ci[1]:.2f}) | "
                     f"{_pct(neg['precision'])} / {_pct(neg['recall'])} | {r['relevant_clear']['macro_f1']:.2f} | {tun:.2f}{cv} |")
    best = res["rows"][-1] if res["rows"] else None
    for r in res["rows"]:
        if L and r["thresholds"] == L["thresholds"] and L["backend"] in r["method"] and "tuned" in r["method"]:
            best = r
    if best:
        c = best["relevant"]["confusion"]
        lines += ["", f"## Confusion matrix on the relevant posts ({best['method']})", "",
                  "| Hand label \\ Scorer | negative | neutral | positive |", "|---|---|---|---|"]
        for g in LABELS:
            lines.append(f"| {g} | {c[g]['negative']} | {c[g]['neutral']} | {c[g]['positive']} |")
        lines += ["", "| Class | Precision | Recall | F1 | Hand-labelled |", "|---|---|---|---|---|"]
        for g in LABELS:
            p = best["relevant"]["per_class"][g]
            lines.append(f"| {g} | {_pct(p['precision'])} | {_pct(p['recall'])} | {p['f1']:.2f} | {p['support']} |")
    if res.get("ablation"):
        lines += ["", "## What each rule adds (tuning set, locked cutoffs; information only, nothing is selected here)", "",
                  "| Variant | Macro-F1 | Negative F1 | Positive F1 |", "|---|---|---|---|"]
        for a in res["ablation"]:
            lines.append(f"| {a['variant']} | {a['macro_f1']:.2f} | {a['negative_f1']:.2f} | {a['positive_f1']:.2f} |")
    if icwsm:
        names = [k for k in icwsm[0] if k not in ("dataset", "n")]
        lines += ["", "## General-text check: correlation with human ratings (Hutto & Gilbert 2014)", "",
                  "| Dataset | n | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
        for rec in icwsm:
            lines.append(f"| {rec['dataset']} | {rec['n']} | " + " | ".join(f"{rec[k]:.3f}" for k in names) + " |")
    return "\n".join(lines) + "\n"


def save_results(res: dict, path: Path) -> None:
    slim = {k: v for k, v in res.items() if k != "locked"}
    if res.get("locked"):
        slim["locked"] = {k: v for k, v in res["locked"].items() if not k.endswith("_scores")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
