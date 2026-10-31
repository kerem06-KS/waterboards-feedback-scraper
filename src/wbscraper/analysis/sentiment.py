"""Experience-focused sentiment: VADER applied only to the sentences that are about a Water Boards site or system.

A post counts as positive or negative only when it gives an opinion of the website or system itself. Talk about
contamination, regulations or property is ignored, and a neutral recommendation ("check GeoTracker") stays neutral.

Steps (all fixed in config/analysis/lexicon.yaml, none fitted to labelled data):
  1. clean: drop quoted lines (someone else's words), URLs, markdown link targets, code and [REDACTED_*] placeholders
  2. split into sentences; keep the *focus* sentences (a named system, an interface word, or the Water Boards plus a
     service word) and the sentence after one when it starts "It ..." / "This ..." / "The site ..."
  3. score each focus sentence with VADER after the domain word list and phrase rules are applied
  4. score = mean of the focus sentences' non-zero compound scores (0.0 when there are none); label by the two thresholds
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..terms import TermLibrary, phrase_regex
from .config import AnalysisConfig

BACKENDS = ("nltk-vader-sentiment", "vaderSentiment")

_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.M)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
_URL = re.compile(r"https?://\S+")                    # bare domains stay: "go to geotracker.waterboards.ca.gov" names the system
_REDACTED = re.compile(r"\[REDACTED_[A-Z_]+\]")
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
_SPACES = re.compile(r"[ \t]+")
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")          # the same splitter the relevance filter uses


def straighten(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def make_backend(name: str, extra_lexicon: dict[str, float] | None = None, alpha: float = 15.0) -> Callable[[str], float]:
    """Returns text -> VADER compound score for the chosen package, with `extra_lexicon` merged into its word list."""
    extra = {k.lower(): float(v) for k, v in (extra_lexicon or {}).items()}
    if name == "nltk-vader-sentiment":
        from vader import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer(extra_lexicon=extra or None, alpha=alpha)
        return lambda text: float(sia.polarity_scores(text)["compound"])
    if name == "vaderSentiment":
        if alpha != 15:
            raise ValueError("vaderSentiment 3.3.2 has a fixed alpha of 15")
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer()
        sia.lexicon.update(extra)
        return lambda text: float(sia.polarity_scores(text)["compound"])
    raise ValueError(f"unknown sentiment backend {name!r}; choose one of {', '.join(BACKENDS)}")


def label_for(score: float, t_neg: float, t_pos: float) -> str:
    return "negative" if score <= t_neg else "positive" if score >= t_pos else "neutral"


@dataclass
class SentimentResult:
    score: float
    label: str
    focus: list[str] = field(default_factory=list)          # the sentences that were scored
    sentence_scores: list[float] = field(default_factory=list)
    mixed: bool = False                                      # one clearly positive and one clearly negative sentence
    near_threshold: bool = False
    no_focus: bool = False                                   # nothing about a site or system: neutral by rule

    @property
    def review_reasons(self) -> list[str]:
        out = []
        if self.near_threshold:
            out.append("score near a threshold")
        if self.mixed:
            out.append("mixed opinions")
        if self.no_focus:
            out.append("no sentence about a site or system")
        return out


class FocusFinder:
    """Picks the sentences that are about a Water Boards website, online system or service channel."""

    def __init__(self, lib: TermLibrary, focus_cfg: dict):
        self.systems = [e for e in lib.entities if e.kind == "system"]
        self.orgs = [e for e in lib.entities if e.kind == "org"]
        self.interface = [rx for rx in (lib.regexes.get("usability"),
                                        phrase_regex(focus_cfg.get("extra_interface_words") or [])) if rx is not None]
        self.service = phrase_regex(focus_cfg.get("service_words") or [], inflect=True)
        starts = sorted({str(s).strip() for s in focus_cfg.get("follow_on_starts") or [] if str(s).strip()},
                        key=len, reverse=True)
        self.follow = re.compile(r"^\W*(?:" + "|".join(re.escape(s) for s in starts) + r")(?!\w)", re.I) if starts else None

    @staticmethod
    def _present(entities, text: str):
        low = text.lower()
        return [e for e in entities if e.match(text, low)[0] in ("strong", "weak_ctx")]

    def find(self, text: str, sentences: list[str]) -> list[int]:
        systems = self._present(self.systems, text)        # context (e.g. stormwater words for SMARTS) judged on the whole post
        orgs = self._present(self.orgs, text)
        focus: set[int] = set()
        for i, s in enumerate(sentences):
            low = s.lower()
            if any(e.match(s, low)[0] for e in systems) or any(rx.search(s) for rx in self.interface):
                focus.add(i)
            elif self.service is not None and self.service.search(s) and any(e.match(s, low)[0] for e in orgs):
                focus.add(i)
        if self.follow is not None:
            for i in sorted(focus):
                j = i + 1
                if j < len(sentences) and j not in focus and self.follow.match(sentences[j]):
                    focus.add(j)
        return sorted(focus)


class SentimentScorer:
    """`groups_off` switches lexicon groups off by name (for ablation); `plain=True` is unmodified VADER on the whole
    text (the baseline)."""

    def __init__(self, cfg: AnalysisConfig, lib: TermLibrary | None = None, *, backend: str | None = None,
                 thresholds: tuple[float, float] | None = None, groups_off: Iterable[str] = (),
                 use_focus: bool = True, use_clean: bool = True, plain: bool = False):
        s = cfg.sentiment
        self.backend_name = backend or s.get("backend", "nltk-vader-sentiment")
        self.alpha = float(s.get("alpha", 15))
        self.t_neg, self.t_pos = thresholds or cfg.thresholds
        if self.t_neg >= self.t_pos:
            raise ValueError("negative threshold must be below the positive threshold")
        self.mixed_cutoff = float(s.get("mixed_cutoff", 0.3))
        self.margin = float(s.get("review_margin", 0.10))
        self.plain = plain
        self.use_focus = use_focus and not plain
        self.use_clean = use_clean and not plain
        off = set(groups_off)
        lex = cfg.lexicon or {}
        extra: dict[str, float] = {}
        self._phrases: list[tuple[re.Pattern, str]] = []
        self._polite = None
        if not plain:
            for name, group in (lex.get("neutral_words") or {}).items():
                if name not in off:
                    extra.update({w: 0.0 for w in group.get("words") or []})
            for name, group in (lex.get("added_words") or {}).items():
                if name not in off:
                    extra.update({k: float(v) for k, v in (group.get("words") or {}).items()})
            n = 0
            for name, group in (lex.get("phrases") or {}).items():
                if f"phrases.{name}" in off or "phrases" in off:
                    continue
                for phrase, val in (group.get("items") or {}).items():
                    token = f"wbxphrase{n}"
                    n += 1
                    extra[token] = float(val)
                    rx = phrase_regex([straighten(phrase)])
                    if rx is not None:
                        self._phrases.append((rx, token))
            self._phrases.sort(key=lambda p: -len(p[0].pattern))
            if "polite_phrases" not in off:
                self._polite = phrase_regex([straighten(p) for p in (lex.get("polite_phrases") or {}).get("items") or []])
        self._compound = make_backend(self.backend_name, extra, self.alpha)
        self.focus_finder = FocusFinder(lib, lex.get("focus") or {}) if (self.use_focus and lib is not None) else None

    # -- text handling ----------------------------------------------------------------------------------
    @staticmethod
    def clean(text: str) -> str:
        t = html.unescape(text or "")
        t = _ZERO_WIDTH.sub("", straighten(t))
        t = _QUOTE_LINE.sub("", t)
        t = _CODE_BLOCK.sub(" ", t)
        t = _INLINE_CODE.sub(" ", t)
        t = _MD_LINK.sub(r"\1", t)
        t = _URL.sub(" ", t)
        t = _REDACTED.sub(" ", t)
        return _SPACES.sub(" ", t).strip()

    @staticmethod
    def sentences(text: str) -> list[str]:
        return [s.strip() for s in SENTENCES.split(text) if s and s.strip() and re.search(r"\w", s)]

    def rewrite(self, sentence: str) -> str:
        """Courtesy formulas out, multi-word complaints/praise in as single scored tokens."""
        s = straighten(sentence)
        if self._polite is not None:
            s = self._polite.sub(" ", s)
        for rx, token in self._phrases:
            s = rx.sub(f" {token} ", s)
        return _SPACES.sub(" ", s).strip()

    def compound(self, text: str) -> float:
        return self._compound(self.rewrite(text) if not self.plain else text)

    # -- scoring ----------------------------------------------------------------------------------------
    def score(self, text: str) -> SentimentResult:
        if self.plain:
            sc = round(self._compound(text or ""), 4)
            return SentimentResult(sc, label_for(sc, self.t_neg, self.t_pos))
        clean = self.clean(text) if self.use_clean else (text or "")
        sents = self.sentences(clean)
        if self.focus_finder is not None:
            idx = self.focus_finder.find(clean, sents)
        else:
            idx = list(range(len(sents)))
        focus = [sents[i] for i in idx]
        scores = [round(self.compound(s), 4) for s in focus]
        opinions = [x for x in scores if x != 0.0]          # a sentence that states no opinion does not dilute one that does
        sc = round(sum(opinions) / len(opinions), 4) if opinions else 0.0
        res = SentimentResult(sc, label_for(sc, self.t_neg, self.t_pos), focus, scores)
        res.no_focus = not focus
        res.mixed = any(x >= self.mixed_cutoff for x in scores) and any(x <= -self.mixed_cutoff for x in scores)
        res.near_threshold = sc != 0.0 and (abs(sc - self.t_neg) < self.margin or abs(sc - self.t_pos) < self.margin)
        return res

    def label(self, score: float) -> str:
        return label_for(score, self.t_neg, self.t_pos)
