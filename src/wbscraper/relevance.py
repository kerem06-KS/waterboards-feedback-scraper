"""Rule-based, two-stage relevance classification (configurable, no paid APIs, no ML dependency).

Stage 1 - connection: does the text itself establish a credible link to the California Water Boards?
    system   a named Water Boards online system (SMARTS, GeoTracker, CIWQS, CalWATRS, ...), or its domain
    org      the State/Regional Water Boards, a division, or a waterboards.ca.gov URL
    program  a Water Boards programme term (operator certification, stormwater permit jargon) that is *also*
             accompanied by a California / Water Boards qualifier
    Ambiguous names (SMARTS, WDID, OCIS, ...) count only in the right case and with their context words.

Stage 2 - usage: does it concern using a website, portal or digital process? Three kinds of evidence,
    A  specific interface vocabulary (website, portal, login, upload, e-file, ...)
    B  generic process vocabulary (submit, account, password, renewal, ...)
    C  experience wording: strong (error, confusing, how do I, easy, ...) or weak (great, how to, does anyone, ...)
    Generic words alone ("system", "error", "account", "login") never qualify: they only matter next to Stage 1.

        system connection   relevant: any C and (A or B)  |  two A                             borderline: any A, B or C
        org / program       relevant: A and strong C                                  borderline: A  |  B and C
        implicit (hint)     never relevant on its own                                 borderline: A and strong C

Locality: both stages are judged inside a window of three neighbouring sentences (short texts are one window), so
a Water Boards mention in the first paragraph of a long post cannot borrow "website" from the last one. URLs are
ignored when looking for usage vocabulary. A hint (a link post's URL, a Water Boards GitHub repository) supplies
the connection for the whole text.

Thread exception (narrow): a reply in a thread that another item already established as relevant needs no
connection of its own, but one window must show A with strong C, A with two B, or two A. Anything weaker is at most
borderline; a reply with no interface vocabulary at all is irrelevant however relevant its thread.

Look-alike patterns (negatives) demote one tier; strong ones (penalty >= 4) reject outright.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .terms import TermLibrary

DEFAULT_WEIGHTS = {
    "system": 3.0, "system_weak": 2.5, "org": 3.0, "org_weak": 2.0, "program": 1.5,
    "usability": 1.5, "usability_cap": 2, "process": 0.5, "process_cap": 2, "experience": 0.5, "experience_cap": 2,
    "california": 0.5, "thread_exception": 1.0, "hard_negative": 4.0,
}

TIERS = ("irrelevant", "borderline", "relevant")


@dataclass
class Score:
    total: float
    tier: str
    entity_hit: bool                      # Stage 1 passed on the item's own text (stored even if Stage 2 fails)
    entities: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    connection: str = "none"              # none | system | org | program
    thread_exception: bool = False

    @property
    def relevant(self) -> bool:
        return self.tier == "relevant"


def cap_tier(score: Score, cap: str | None) -> Score:
    """Never let an item rank above `cap` (e.g. Water Boards project repositories stay in review)."""
    if cap and TIERS.index(score.tier) > TIERS.index(cap):
        score.tier = cap
        score.reasons.append(f"capped:{cap}")
    return score


def _demote(tier: str) -> str:
    return TIERS[max(0, TIERS.index(tier) - 1)]


_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_SHORT_TEXT = 500                  # characters; shorter texts are judged as one window
_RANK = {"none": 0, "implicit": 1, "program": 2, "org": 3, "system": 4}


class RelevanceScorer:
    def __init__(self, lib: TermLibrary):
        self.lib = lib
        rel = lib.relevance or {}
        self.w = {**DEFAULT_WEIGHTS, **(rel.get("weights") or {})}
        self._usability = lib.regexes.get("usability")
        self._process = lib.regexes.get("process")
        self._experience = lib.regexes.get("experience")
        self._experience_weak = lib.regexes.get("experience_weak")
        self._california = lib.regexes.get("california")
        self._ca_connection = lib.regexes.get("ca_connection")

    @staticmethod
    def _distinct(rx, text: str) -> set[str]:
        if rx is None:
            return set()
        return {m.group(0).lower() for m in rx.finditer(text)}

    # -- Stage 1 ---------------------------------------------------------------------------------------------
    def _connection(self, text: str, ca_ok: bool) -> tuple[str, float, list[str], list[str]]:
        """Best connection kind, its points, entity ids and reasons. Topic phrases count only with a California /
        Water Boards qualifier (`ca_ok`, judged on the whole text)."""
        w = self.w
        text_l = text.lower()
        best_kind, best_pts = "none", 0.0
        entities: list[str] = []
        reasons: list[str] = []
        for e in self.lib.entities:
            level, _ = e.match(text, text_l)
            if level is None:
                continue
            if level == "weak_noctx":
                if e.kind != "topic":
                    reasons.append(f"weak-no-context:{e.id}")
                continue
            if e.kind == "topic":
                if not ca_ok:
                    reasons.append(f"topic-no-california:{e.id}")
                    continue
                kind, pts = "program", w["program"]
            elif e.kind == "system":
                kind, pts = "system", w["system"] if level == "strong" else w["system_weak"]
            else:
                kind, pts = "org", w["org"] if level == "strong" else w["org_weak"]
            entities.append(e.id)
            reasons.append(f"{level}:{e.id}")
            if (_RANK[kind], pts) > (_RANK[best_kind], best_pts):
                best_kind, best_pts = kind, pts
        return best_kind, best_pts, entities, reasons

    @staticmethod
    def _windows(text: str) -> list[str]:
        if len(text) <= _SHORT_TEXT:
            return [text]
        sents = [x for x in _SENTENCES.split(text) if x and x.strip()]
        if len(sents) <= 3:
            return [text]
        return [" ".join(sents[max(0, i - 1): i + 2]) for i in range(len(sents))]

    # -- Stage 2 ---------------------------------------------------------------------------------------------
    @staticmethod
    def _tier(conn: str, a: int, b: int, c: int, cs: int, thread_relevant: bool) -> tuple[str, bool]:
        """(tier, used_thread_exception). c = all experience wording, cs = the strong kind only."""
        strong_use = a >= 1 and (b >= 1 or c >= 1 or a >= 2)
        if conn == "system":
            return ("relevant" if ((c >= 1 and (a >= 1 or b >= 1)) or a >= 2) else
                    "borderline" if (a or b or c) else "irrelevant"), False
        if conn == "implicit":
            return ("borderline" if (a >= 1 and cs >= 1) else "irrelevant"), False
        if conn in ("org", "program"):
            return ("relevant" if (a >= 1 and cs >= 1) else
                    "borderline" if (a or (b and c)) else "irrelevant"), False
        if thread_relevant:
            tier = "relevant" if ((a >= 1 and cs >= 1) or (a >= 1 and b >= 2) or a >= 2) else \
                   ("borderline" if (a or (b and c)) else "irrelevant")
            return tier, tier != "irrelevant"
        return "irrelevant", False

    # -- scoring ---------------------------------------------------------------------------------------------
    def score(self, text: str, thread_relevant: bool = False, hint: str = "") -> Score:
        """`hint` is evidence that is not part of the stored text (a link post's URL, a Water Boards repository)."""
        w = self.w
        text = text.replace("\u2019", "'").replace("\u2018", "'")      # curly apostrophes: matching only, stored text is untouched
        hint = hint.replace("\u2019", "'") if hint else hint
        ca_ok = bool(self._ca_connection and self._ca_connection.search(text + " " + hint))
        reasons: list[str] = []
        entities: list[str] = []

        h_conn, h_pts, h_ents, h_reasons = self._connection(hint, ca_ok) if hint else ("none", 0.0, [], [])
        plain = _URL.sub(" ", text)
        g_conn, g_pts, g_ents, g_reasons = self._connection(plain, ca_ok)
        if g_conn == "none":                      # only a link: the page was shared, not necessarily used
            u_conn, _, u_ents, u_reasons = self._connection(text, ca_ok)
            if u_conn != "none" and h_conn == "none":
                h_conn, h_pts, h_ents, h_reasons = u_conn, 0.0, u_ents, [f"url-only:{r}" for r in u_reasons]
        if h_conn != "none":
            h_conn = "implicit"          # a hint (repository, link URL) is context, never proof
        entity_hit = h_conn != "none" or g_conn != "none"
        entities = list(dict.fromkeys(h_ents + g_ents))
        reasons.extend(f"hint:{r}" for r in h_reasons)
        reasons.extend(g_reasons)

        best = ("irrelevant", "none", 0.0, 0, 0, 0, False, (set(), set(), set()))   # tier, conn, pts, a, b, c, exc, words
        if entity_hit or thread_relevant:
            for win in self._windows(text):
                conn, pts, _, _ = self._connection(_URL.sub(" ", win), ca_ok) if g_conn != "none" else ("none", 0.0, [], [])
                if _RANK[h_conn] > _RANK[conn]:
                    conn, pts = h_conn, h_pts
                usage = _URL.sub(" ", win)
                sa, sb, sc = (self._distinct(self._usability, usage), self._distinct(self._process, usage),
                              self._distinct(self._experience, usage))
                cs = len(sc)
                sc = sc | self._distinct(self._experience_weak, usage)
                a, b, c = len(sa), len(sb), len(sc)
                tier, exc = self._tier(conn, a, b, c, cs, thread_relevant)
                key = (TIERS.index(tier), _RANK[conn], a + b + c)
                if key > (TIERS.index(best[0]), _RANK[best[1]], best[3] + best[4] + best[5]):
                    best = (tier, conn, pts, a, b, c, exc, (sa, sb, sc))
        tier, conn, pts, na, nb, nc, exception, words = best
        if conn == "none" and entity_hit:                          # connection exists but in no scored window
            conn = h_conn if _RANK[h_conn] >= _RANK[g_conn] else g_conn
            pts = h_pts if _RANK[h_conn] >= _RANK[g_conn] else g_pts
        reasons.append(f"stage1:{conn}")
        reasons.append("A=" + ",".join(sorted(words[0])))
        reasons.append("B=" + ",".join(sorted(words[1])))
        reasons.append("C=" + ",".join(sorted(words[2])))
        if exception:
            reasons.append("thread-exception")

        negative = 0.0
        for neg in self.lib.negatives:
            if neg.when is not None and not neg.when.search(text):
                continue
            if not neg.pattern.search(text):
                continue
            if neg.unless is not None and neg.unless.search(text):
                continue
            negative = max(negative, neg.penalty)
            reasons.append(f"negative:{neg.id}")
        if negative >= w["hard_negative"]:
            tier = "irrelevant"
        elif negative:
            tier = _demote(tier)

        total = pts + w["usability"] * min(na, w["usability_cap"]) + w["process"] * min(nb, w["process_cap"]) \
            + w["experience"] * min(nc, w["experience_cap"])
        if conn != "none" and self._california is not None and self._california.search(text):
            total += w["california"]
            reasons.append("california")
        if exception:
            total += w["thread_exception"]
        total -= negative
        if conn == "none" and not exception:
            total = min(total, 1.0)   # no Water Boards connection: whatever the wording, it cannot rank high
        return Score(total=round(total, 3), tier=tier, entity_hit=entity_hit, entities=entities,
                     reasons=reasons, connection=conn, thread_exception=exception)
