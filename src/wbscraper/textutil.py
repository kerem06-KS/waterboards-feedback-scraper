"""Small text helpers shared by the pipeline."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")
_DELETED = {"[deleted]", "[removed]", "deleted", "removed", "[deleted by user]"}
_BOT_PATTERNS = (
    re.compile(r"i am a bot, and this action was performed automatically", re.I),
    re.compile(r"^\*\*?this is an automated (message|reminder)", re.I),
    re.compile(r"^your (post|submission|comment) has been removed", re.I),
    re.compile(r"^hello! (your|this) (post|submission)", re.I),
    re.compile(r"i am a bot whose sole purpose", re.I),
    re.compile(r"^this (submission|post) has been removed from /?r/", re.I),
    re.compile(r"^\*\*?(please )?read the rules", re.I),
)


def clean_text(text: str | None) -> str:
    """Normalise newlines and drop control characters; keeps wording untouched."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    return text.strip()


def is_deleted(text: str) -> bool:
    return text.strip().lower() in _DELETED


def looks_like_bot(text: str) -> bool:
    head = text.strip()[:300]
    return any(p.search(head) or p.search(text[-300:]) for p in _BOT_PATTERNS)


def normalise_for_hash(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return _WS.sub(" ", t).strip()


def text_hash(text: str) -> str:
    return hashlib.sha1(normalise_for_hash(text).encode("utf-8")).hexdigest()
