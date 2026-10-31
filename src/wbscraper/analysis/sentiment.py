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
import math
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..terms import TermLibrary, phrase_regex
from .config import AnalysisConfig

AGGREGATES = ("mean", "sum", "vote")
BACKENDS = ("nltk-vader-sentiment", "vaderSentiment")

_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.M)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
_URL = re.compile(r"https?://\S+")                    # bare domains stay: "go to geotracker.waterboards.ca.gov" names the system
_REDACTED = re.compile(r"\[REDACTED_[A-Z_]+\]")
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
_SPACES = re.compile(r"[ \t]+")
CONTRAST = re.compile(r"\s*;\s*|,?\s+\b(?:but|however|although|though|yet|whereas)\b,?\s+|^\s*(?:but|however|although|though)\b,?\s+", re.I)
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")          # the same splitter the relevance filter uses


def is_document_url(url: str) -> bool:
    """Agency documents and comment letters on waterboards.ca.gov: authored text about policy, not a person's experience."""
    host = (urlparse(url or "").netloc or "").lower()
    return host.endswith("waterboards.ca.gov")


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

    def __init__(self, lib: TermLibrary, focus_cfg: dict, complaint_rx: re.Pattern | None = None,
                 org_rx: re.Pattern | None = None):
        self.complaint_rx = complaint_rx
        self.org_rx = org_rx
        self.short_post = int(focus_cfg.get("short_post_sentences", 0))
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

    def find(self, text: str, sentences: list[str], *, strict: bool = False) -> list[int]:
        """`strict` (agency documents): only sentences that themselves name a system or an interface word.
        Otherwise a sentence that states an inability, limit or missing feature also counts when the post is about a
        named system or the Water Boards, even if the sentence does not repeat the name."""
        systems = self._present(self.systems, text)        # context (e.g. stormwater words for SMARTS) judged on the whole post
        orgs = self._present(self.orgs, text)
        about_site = bool(systems or orgs)
        focus: set[int] = set()
        for i, s in enumerate(sentences):
            low = s.lower()
            if any(e.match(s, low)[0] for e in systems):
                focus.add(i)
            elif not strict and any(rx.search(s) for rx in self.interface):
                focus.add(i)
            elif not strict and self.service is not None and self.service.search(s) and any(e.match(s, low)[0] for e in orgs):
                focus.add(i)
            elif not strict and about_site and self.complaint_rx is not None and self.complaint_rx.search(s):
                focus.add(i)
            elif not strict and self.org_rx is not None and self.org_rx.search(s) and any(e.match(s, low)[0] for e in orgs):
                focus.add(i)
        if not strict and about_site and 0 < len(sentences) <= self.short_post:
            focus = set(range(len(sentences)))           # a short post about a system: every sentence is about it
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
                 use_focus: bool = True, use_clean: bool = True, plain: bool = False,
                 aggregate: str | None = None, sum_alpha: float | None = None):
        s = cfg.sentiment
        self.backend_name = backend or s.get("backend", "nltk-vader-sentiment")
        self.alpha = float(s.get("alpha", 15))
        self.t_neg, self.t_pos = thresholds or cfg.thresholds
        if self.t_neg >= self.t_pos:
            raise ValueError("negative threshold must be below the positive threshold")
        self.mixed_cutoff = float(s.get("mixed_cutoff", 0.3))
        self.margin = float(s.get("review_margin", 0.10))
        self.plain = plain
        self.aggregate = aggregate or s.get("aggregate", "mean")
        if self.aggregate not in AGGREGATES:
            raise ValueError(f"unknown aggregate {self.aggregate!r}; choose one of {', '.join(AGGREGATES)}")
        self.sum_alpha = float(sum_alpha if sum_alpha is not None else s.get("sum_alpha", 1.0))
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
            sub = lex.get("subtle_words") or {}
            if sub and "subtle_words" not in off:
                extra.update({k: float(v) for k, v in (sub.get("words") or {}).items()})
            n = 0
            if "complaint_patterns" not in off:
                for cp in lex.get("complaint_patterns") or []:
                    if f"complaint.{cp['name']}" in off:
                        continue
                    token = f"wbxphrase{n}"
                    n += 1
                    extra[token] = float(cp["value"])
                    self._phrases.append((re.compile(cp["pattern"], re.I), token))
            plain_phrases: list[tuple[re.Pattern, str]] = []
            for name, group in (lex.get("phrases") or {}).items():
                if f"phrases.{name}" in off or "phrases" in off:
                    continue
                for phrase, val in (group.get("items") or {}).items():
                    token = f"wbxphrase{n}"
                    n += 1
                    extra[token] = float(val)
                    rx = phrase_regex([straighten(phrase)])
                    if rx is not None:
                        plain_phrases.append((rx, token))
            self._phrases.extend(sorted(plain_phrases, key=lambda p: -len(p[0].pattern)))
            if "polite_phrases" not in off:
                self._polite = phrase_regex([straighten(p) for p in (lex.get("polite_phrases") or {}).get("items") or []])
        self._compound = make_backend(self.backend_name, extra, self.alpha)
        complaint_rx = org_rx = None
        if "complaint_patterns" not in off and "complaint_focus" not in off:
            live = [cp for cp in lex.get("complaint_patterns") or [] if f"complaint.{cp['name']}" not in off]
            pats = [cp["pattern"] for cp in live if cp.get("expand_focus") is True]
            org_pats = [cp["pattern"] for cp in live if cp.get("expand_focus") == "org"]
            complaint_rx = re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None
            org_rx = re.compile("|".join(f"(?:{p})" for p in org_pats), re.I) if org_pats else None
        self.doc_strict = "document_strict" not in off
        self.split_clauses = "clauses" not in off
        self.focus_finder = FocusFinder(lib, lex.get("focus") or {}, complaint_rx, org_rx) if (self.use_focus and lib is not None) else None

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

    def sentences(self, text: str) -> list[str]:
        """Sentences, split further at contrast words ("but", "however", "though", ";") so a complaint is scored on its
        own instead of being halved by VADER's "but" rule or averaged away by the praise next to it."""
        out = []
        for part in SENTENCES.split(text):
            if not part or not part.strip() or not re.search(r"\w", part):
                continue
            if self.split_clauses:
                out.extend(c.strip(" ()") for c in CONTRAST.split(part) if c and re.search(r"\w", c))
            else:
                out.append(part.strip())
        return out

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
    def score(self, text: str, source_url: str = "") -> SentimentResult:
        if self.plain:
            sc = round(self._compound(text or ""), 4)
            return SentimentResult(sc, label_for(sc, self.t_neg, self.t_pos))
        clean = self.clean(text) if self.use_clean else (text or "")
        sents = self.sentences(clean)
        if self.focus_finder is not None:
            idx = self.focus_finder.find(clean, sents, strict=self.doc_strict and is_document_url(source_url))
        else:
            idx = list(range(len(sents)))
        focus = [sents[i] for i in idx]
        scores = [round(self.compound(s), 4) for s in focus]
        opinions = [x for x in scores if x != 0.0]          # a sentence that states no opinion does not dilute one that does
        if not opinions:
            sc = 0.0
        elif self.aggregate == "vote":                       # complaints are judged on their own, praise cannot average them away
            neg = [x for x in opinions if x <= -self.mixed_cutoff]
            pos = [x for x in opinions if x >= self.mixed_cutoff]
            if neg and len(neg) >= len(pos):
                sc = round(sum(neg) / len(neg), 4)
            elif pos and len(pos) > len(neg):
                sc = round(sum(pos) / len(pos), 4)
            else:
                sc = round(sum(opinions) / len(opinions), 4)
        elif self.aggregate == "sum":                        # many complaints add up; squashed back into -1..+1
            total = sum(opinions)
            sc = round(total / math.sqrt(total * total + self.sum_alpha), 4)
        else:
            sc = round(sum(opinions) / len(opinions), 4)
        res = SentimentResult(sc, label_for(sc, self.t_neg, self.t_pos), focus, scores)
        res.no_focus = not focus
        res.mixed = any(x >= self.mixed_cutoff for x in scores) and any(x <= -self.mixed_cutoff for x in scores)
        res.near_threshold = sc != 0.0 and (abs(sc - self.t_neg) < self.margin or abs(sc - self.t_pos) < self.margin)
        return res

    def label(self, score: float) -> str:
        return label_for(score, self.t_neg, self.t_pos)
