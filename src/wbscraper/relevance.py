"""Rule-based relevance scoring (configurable, no paid APIs, no ML dependency).

Score = entity/topic anchor + digital-use signal + experience signal + context bonuses - penalties.
Items with no anchor (no entity, no topic phrase, no relevant parent thread) are capped low, so generic
"my bank website is slow" posts never qualify, while a comment like "the upload kept timing out for me too"
inside a relevant thread does.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .terms import TermLibrary

DEFAULT_WEIGHTS = {
    "entity_strong": 3.0, "entity_weak_with_context": 2.5, "entity_weak_no_context": 0.5, "topic_default": 1.5,
    "digital_first": 1.5, "digital_additional": 0.5, "digital_cap": 2.5,
    "experience_first": 0.5, "experience_additional": 0.25, "experience_cap": 1.0,
    "california_context": 1.0, "thread_context": 2.5, "policy_only_penalty": -1.0, "max_penalty": -6.0,
    "no_anchor_cap": 1.0,
}
DEFAULT_THRESHOLDS = {"relevant": 4.0, "borderline": 2.5}


@dataclass
class Score:
    total: float
    tier: str
    entity_hit: bool
    entities: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def relevant(self) -> bool:
        return self.tier == "relevant"


class RelevanceScorer:
    def __init__(self, lib: TermLibrary):
        self.lib = lib
        rel = lib.relevance or {}
        self.w = {**DEFAULT_WEIGHTS, **(rel.get("weights") or {})}
        self.th = {**DEFAULT_THRESHOLDS, **(rel.get("thresholds") or {})}
        self._digital = lib.regexes.get("digital")
        self._experience = lib.regexes.get("experience")
        self._california = lib.regexes.get("california")

    def tier_for(self, total: float) -> str:
        if total >= self.th["relevant"]:
            return "relevant"
        if total >= self.th["borderline"]:
            return "borderline"
        return "irrelevant"

    @staticmethod
    def _distinct(rx, text: str) -> set[str]:
        if rx is None:
            return set()
        return {m.group(0).lower() for m in rx.finditer(text)}

    def score(self, text: str, thread_relevant: bool = False) -> Score:
        w = self.w
        text_l = text.lower()
        reasons: list[str] = []
        entities: list[str] = []
        best_entity = 0.0
        best_kind: str | None = None
        has_system = False
        topic = 0.0
        entity_hit = False

        for e in self.lib.entities:
            level, matched = e.match(text, text_l)
            if level is None:
                continue
            if level == "weak_noctx":
                if e.kind != "topic":
                    reasons.append(f"weak-no-context:{e.id}")
                continue
            entity_hit = True
            entities.append(e.id)
            if e.kind == "topic":
                val = float(e.weight if e.weight is not None else w["topic_default"])
                topic = max(topic, val)
                reasons.append(f"topic:{e.id}={val}")
                continue
            val = w["entity_strong"] if level == "strong" else w["entity_weak_with_context"]
            has_system = has_system or e.kind == "system"
            if val > best_entity:
                best_entity, best_kind = val, e.kind
            reasons.append(f"{level}:{e.id}")

        anchor = best_entity + (topic if best_entity < w["entity_strong"] else 0.0)
        has_anchor = anchor > 0 or thread_relevant

        digital_hits = self._distinct(self._digital, text)
        digital = 0.0
        if digital_hits:
            digital = min(w["digital_cap"], w["digital_first"] + w["digital_additional"] * (len(digital_hits) - 1))
            reasons.append(f"digital:{len(digital_hits)}")

        exp_hits = self._distinct(self._experience, text)
        if "?" in text:
            exp_hits.add("?")
        experience = 0.0
        if exp_hits:
            experience = min(w["experience_cap"], w["experience_first"] + w["experience_additional"] * (len(exp_hits) - 1))
            reasons.append(f"experience:{len(exp_hits)}")

        california = 0.0
        if has_anchor and anchor > 0 and self._california is not None and self._california.search(text):
            california = w["california_context"]
            reasons.append("california")

        thread = w["thread_context"] if thread_relevant else 0.0
        if thread:
            reasons.append("thread-context")

        penalty = 0.0
        if best_kind == "org" and not has_system and topic == 0 and digital == 0 and not thread_relevant:
            penalty += w["policy_only_penalty"]
            reasons.append("policy-only")
        for neg in self.lib.negatives:
            if neg.when is not None and not neg.when.search(text):
                continue
            if not neg.pattern.search(text):
                continue
            if neg.unless is not None and neg.unless.search(text):
                continue
            penalty -= neg.penalty
            reasons.append(f"negative:{neg.id}")
        penalty = max(penalty, w["max_penalty"])

        total = anchor + digital + experience + california + thread + penalty
        if not has_anchor:
            total = min(total, w["no_anchor_cap"])
        total = round(total, 3)
        return Score(total=total, tier=self.tier_for(total), entity_hit=entity_hit, entities=entities, reasons=reasons)
