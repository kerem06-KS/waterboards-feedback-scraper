"""Loads the editable term library (config/terms/*.yaml) and compiles matchers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_yaml

_INFLECT = r"(?:s|es|ed|d|ing|ting)?"


def phrase_regex(phrases: list[str], *, case_sensitive: bool = False, inflect: bool = False) -> re.Pattern | None:
    parts: list[str] = []
    for p in sorted({str(x).strip() for x in phrases if x is not None and str(x).strip()}, key=len, reverse=True):
        toks = p.split()
        body = r"\s+".join(re.escape(t) for t in toks)
        pre = r"(?<!\w)" if re.match(r"\w", p[0]) else ""
        post = (_INFLECT if inflect else "") + (r"(?!\w)" if re.match(r"\w", p[-1]) else "")
        parts.append(f"{pre}{body}{post}")
    if not parts:
        return None
    return re.compile("(?:" + "|".join(parts) + ")", 0 if case_sensitive else re.IGNORECASE)


@dataclass
class WeakTerm:
    term: str
    case_sensitive: bool
    context: list[str]
    regex: re.Pattern
    context_regex: re.Pattern | None


@dataclass
class Entity:
    id: str
    label: str
    kind: str
    short: str
    strong: list[str]
    weak: list[WeakTerm]
    domains: list[str]
    weight: float | None
    anchors: dict[int, list[str]]
    pullpush: list[str]
    web_extra: list[str]
    tasks: list[str]
    strong_re: re.Pattern | None = None

    def match(self, text: str, text_lower: str) -> tuple[str | None, str | None]:
        """Returns (level, matched) where level is 'strong' | 'weak_ctx' | 'weak_noctx' | None."""
        if self.strong_re is not None:
            m = self.strong_re.search(text)
            if m:
                return "strong", m.group(0)
        for d in self.domains:
            if d.lower() in text_lower:
                return "strong", d
        weak_noctx = None
        for w in self.weak:
            m = w.regex.search(text)
            if not m:
                continue
            if w.context_regex is None or w.context_regex.search(text):
                return "weak_ctx", m.group(0)
            weak_noctx = weak_noctx or m.group(0)
        return ("weak_noctx", weak_noctx) if weak_noctx else (None, None)


@dataclass
class Negative:
    id: str
    pattern: re.Pattern
    penalty: float
    when: re.Pattern | None
    unless: re.Pattern | None


@dataclass
class TermLibrary:
    entities: list[Entity]
    groups: dict[str, list[str]]
    modifiers: dict[str, list[str]]
    questions: dict[str, list[str]]
    negatives: list[Negative]
    subreddits: dict[str, list[str]]
    relevance: dict
    forums: dict = field(default_factory=dict)
    signals: dict[str, list[str]] = field(default_factory=dict)
    regexes: dict[str, re.Pattern | None] = field(default_factory=dict)

    # -- context helpers ---------------------------------------------------------------
    def expand_context(self, items: list[str]) -> list[str]:
        out: list[str] = []
        for it in items or []:
            if isinstance(it, str) and it.startswith("@"):
                out.extend(self.groups.get(it[1:], []))
            else:
                out.append(it)
        return out

    def context_regex(self, items: list[str]) -> re.Pattern | None:
        return phrase_regex(self.expand_context(items))

    # -- lookups -------------------------------------------------------------------------
    def entity(self, entity_id: str) -> Entity:
        return next(e for e in self.entities if e.id == entity_id)

    def all_words(self, group: str) -> list[str]:
        return list(self.modifiers.get(group, []))

    @classmethod
    def load(cls, config_dir: Path) -> "TermLibrary":
        terms_dir = Path(config_dir) / "terms"
        groups = (load_yaml(terms_dir / "context.yaml") or {}).get("groups", {})
        modifiers = load_yaml(terms_dir / "modifiers.yaml") or {}
        questions = load_yaml(terms_dir / "questions.yaml") or {}
        neg_raw = (load_yaml(terms_dir / "negatives.yaml") or {}).get("negatives", [])
        ent_raw = (load_yaml(terms_dir / "entities.yaml") or {}).get("entities", [])
        sub_path = Path(config_dir) / "sources" / "subreddits.yaml"
        subreddits = load_yaml(sub_path) if sub_path.exists() else {}
        rel_path = Path(config_dir) / "relevance.yaml"
        relevance = load_yaml(rel_path) if rel_path.exists() else {}

        forums_path = Path(config_dir) / "sources" / "forums.yaml"
        forums = load_yaml(forums_path) if forums_path.exists() else {}
        lib = cls(entities=[], groups=groups, modifiers=modifiers, questions=questions, negatives=[],
                  subreddits=subreddits, relevance=relevance, forums=forums)

        for raw in ent_raw:
            weak: list[WeakTerm] = []
            for w in raw.get("weak", []) or []:
                cs = bool(w.get("case_sensitive", False))
                rx = phrase_regex([w["term"]], case_sensitive=cs)
                weak.append(WeakTerm(w["term"], cs, list(w.get("context", [])), rx,
                                     lib.context_regex(w.get("context", []))))
            anchors = {int(k): list(v or []) for k, v in (raw.get("anchors") or {}).items()}
            ent = Entity(
                id=raw["id"], label=raw.get("label", raw["id"]), kind=raw.get("kind", "system"),
                short=raw.get("short", raw.get("label", raw["id"])), strong=list(raw.get("strong", []) or []),
                weak=weak, domains=list(raw.get("domains", []) or []), weight=raw.get("weight"),
                anchors=anchors, pullpush=list(raw.get("pullpush", []) or []),
                web_extra=list(raw.get("web_extra", []) or []), tasks=list(raw.get("tasks", []) or []),
            )
            ent.strong_re = phrase_regex(ent.strong)
            lib.entities.append(ent)

        for n in neg_raw:
            lib.negatives.append(Negative(
                id=n["id"], pattern=re.compile(n["pattern"], re.IGNORECASE), penalty=float(n["penalty"]),
                when=re.compile(n["when"], re.IGNORECASE) if n.get("when") else None,
                unless=lib.context_regex(n.get("unless_context", [])) if n.get("unless_context") else None))

        # Stage-2 vocabulary (config/terms/signals.yaml). The query modifiers above are for building searches only.
        signals = load_yaml(terms_dir / "signals.yaml") or {}
        lib.signals = {k: list(v or []) for k, v in signals.items()}
        lib.regexes["usability_generic"] = phrase_regex(lib.signals.get("usability_generic", []), inflect=True)
        lib.regexes["usability_ambiguous"] = phrase_regex(lib.signals.get("usability_ambiguous", []), inflect=True)
        lib.regexes["usability"] = phrase_regex(
            lib.signals.get("usability", []) + lib.signals.get("usability_ambiguous", []), inflect=True)
        for name in ("process", "experience", "experience_soft", "instruction_steps", "usage_words", "experience_help", "experience_weak"):
            lib.regexes[name] = phrase_regex(lib.signals.get(name, []), inflect=True)
        lib.regexes["california"] = lib.context_regex(["@california"])
        lib.regexes["ca_connection"] = lib.context_regex(["@california", "@waterboard_names"])
        return lib
