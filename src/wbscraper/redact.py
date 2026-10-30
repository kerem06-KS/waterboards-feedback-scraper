"""Redaction of incidental personal identifiers inside post text."""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_PHONE = re.compile(
    r"(?<![\w/])(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s*|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?!\d)"
)
_PROFILE_URL = re.compile(
    r"https?://(?:www\.|old\.|new\.|np\.|m\.)?(?:reddit\.com/(?:u|user)/[\w\-]+"
    r"|(?:twitter|x)\.com/[\w]{1,15}(?![\w/])"
    r"|(?:linkedin\.com/in/|facebook\.com/(?!groups/|pages/|watch)|instagram\.com/|tiktok\.com/@|bsky\.app/profile/|github\.com/(?!orgs/))[\w.\-%]+)\S*",
    re.IGNORECASE,
)
_REDDIT_USER = re.compile(r"(?<![\w/])/?u/[A-Za-z0-9_\-]{3,20}\b")
_HANDLE = re.compile(r"(?<![\w.@/])@[A-Za-z0-9_]{2,30}(?:@[A-Za-z0-9.\-]+\.[a-z]{2,})?")
_STREET = re.compile(
    r"\b\d{1,5}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-z]+\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Circle|Cir)\b\.?"
)
# Public agency addresses are not personal data.
_AGENCY_ADDR = re.compile(r"1001\s+I\s+Street|1416\s+(?:9th|Ninth)\s+Street|1219\s+K\s+Street", re.IGNORECASE)


def _trailing(s: str) -> str:
    """Trailing sentence punctuation that a greedy match swallowed."""
    m = re.search(r"[.,;:!?)\]]+$", s)
    return m.group(0) if m else ""


def redact(text: str, *, emails=True, phones=True, handles=True, profile_urls=True, street_addresses=True) -> str:
    if not text:
        return text
    if profile_urls:
        text = _PROFILE_URL.sub(lambda m: "[REDACTED_PROFILE_URL]" + _trailing(m.group(0)), text)
    if emails:
        text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    if phones:
        text = _PHONE.sub("[REDACTED_PHONE]", text)
    if handles:
        text = _REDDIT_USER.sub("[REDACTED_USER]", text)
        text = _HANDLE.sub("[REDACTED_HANDLE]", text)
    if street_addresses:
        def _addr(m: re.Match) -> str:
            return m.group(0) if _AGENCY_ADDR.search(m.group(0)) else "[REDACTED_ADDRESS]" + _trailing(m.group(0))

        text = _STREET.sub(_addr, text)
    return text


def redactor_from_settings(settings) -> "callable":
    cfg = settings.get("redaction", {}) or {}
    opts = dict(
        emails=cfg.get("emails", True),
        phones=cfg.get("phones", True),
        handles=cfg.get("handles", True),
        profile_urls=cfg.get("profile_urls", True),
        street_addresses=cfg.get("street_addresses", True),
    )
    return lambda t: redact(t, **opts)
