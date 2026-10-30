"""Turns raw items into stored, scored, deduplicated rows. One place for all filtering decisions."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .db import Database
from .relevance import RelevanceScorer, Score, cap_tier
from .sources.base import RawItem
from .stats import RunStats
from .textutil import clean_text, is_deleted, looks_like_bot, text_hash

DEDUP_MIN_CHARS = 80   # near-identical short replies ("same problem here") in different threads are distinct


@dataclass
class IngestResult:
    status: str                       # stored | duplicate | skipped | below_threshold
    tier: str = "irrelevant"
    own_relevant: bool = False
    entity_hit: bool = False
    new_relevant_thread: bool = False


class Pipeline:
    def __init__(self, db: Database, scorer: RelevanceScorer, redactor, stats: RunStats, min_chars: int = 12):
        self.db = db
        self.scorer = scorer
        self.redactor = redactor
        self.stats = stats
        self.min_chars = min_chars

    def ingest(self, raw: RawItem, provider: str, method: str) -> IngestResult:
        text = clean_text(raw.text)
        if not text or is_deleted(text) or len(text) < self.min_chars or looks_like_bot(text):
            self.stats.skipped_empty += 1
            return IngestResult("skipped")

        existing = self.db.item_row(raw.uid)
        if existing is not None:
            self.db.add_method(raw.uid, provider, method)
            self.stats.duplicates += 1
            return IngestResult("duplicate", existing["tier"], bool(existing["own_relevant"]), bool(existing["entity_hit"]))

        text = self.redactor(text)
        thread_flag = self.db.thread_relevant(raw.thread_uid)
        own = cap_tier(self.scorer.score(text, thread_relevant=False, hint=raw.score_hint, kind=raw.kind), raw.tier_cap)
        final: Score = cap_tier(self.scorer.score(text, thread_relevant=True, hint=raw.score_hint, kind=raw.kind), raw.tier_cap) \
            if thread_flag else own
        self.stats.candidates += 1

        if final.tier == "irrelevant" and not final.entity_hit:
            self.stats.below_threshold += 1
            return IngestResult("below_threshold", "irrelevant", False, False)

        h = text_hash(text) if len(text) >= DEDUP_MIN_CHARS else None
        if h and self.db.hash_owner(h):
            self.stats.duplicates += 1
            return IngestResult("duplicate", final.tier, own.relevant, final.entity_hit)

        inserted = self.db.insert_item(
            uid=raw.uid, source=raw.source, kind=raw.kind, url=raw.url, thread_uid=raw.thread_uid,
            parent_uid=raw.parent_uid, community=raw.community, text=text, date=raw.date, tier=final.tier,
            own_relevant=int(own.relevant), score=final.total, entity_hit=int(final.entity_hit),
            reasons=json.dumps(final.reasons), text_hash=h, flags=None)
        if not inserted:  # same URL under a different uid
            self.stats.duplicates += 1
            return IngestResult("duplicate", final.tier, own.relevant, final.entity_hit)
        self.db.add_method(raw.uid, provider, method)

        new_thread = False
        if raw.thread_uid:
            new_thread = self.db.upsert_thread(
                raw.thread_uid, raw.source, community=raw.community,
                url=raw.url if raw.kind in ("post", "article") else None, title=raw.title, relevant=own.relevant)
        if final.tier == "relevant":
            self.stats.relevant += 1
        elif final.tier == "borderline":
            self.stats.borderline += 1
        else:
            self.stats.below_threshold += 1
        return IngestResult("stored", final.tier, own.relevant, final.entity_hit, new_thread)

    def ingest_many(self, raws: list[RawItem], provider: str, method: str) -> list[IngestResult]:
        """Ingest a batch (e.g. a question with its answers/comments). Items dropped before their thread was known
        to be relevant are scored again once it is, so context-qualified replies are not lost."""
        results = [self.ingest(r, provider, method) for r in raws]
        for i, (r, res) in enumerate(zip(raws, results)):
            if res.status == "below_threshold" and r.thread_uid and self.db.thread_relevant(r.thread_uid):
                self.stats.candidates -= 1
                self.stats.below_threshold -= 1
                results[i] = self.ingest(r, provider, method)
        return results
