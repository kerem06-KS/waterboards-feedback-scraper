"""What kind of site a web page comes from. Used to cap relevance, never to widen it.

official    government, university and mailing-list-bulletin sites (*.gov, *.edu, *.state.xx.us, govdelivery.com).
            Their pages are announcements, instructions and reference material, not public feedback, so an item from
            one can be at most "borderline" (kept for review). Comment letters hosted on waterboards.ca.gov are a
            separate source (`waterboards`) and are not affected.
reference   site checkers, technology profilers, dictionaries and vendor manuals ("is X down", "reviews of X",
            "what is X"). They talk about a site, not to or about people using it: never relevant, never fetched.
"""
from __future__ import annotations

import re

DEFAULT_REFERENCE = (
    "builtwith.com", "scamadviser.com", "sur.ly", "downornot.net", "hypestat.com", "isitdownrightnow.com",
    "urlscan.io", "similarweb.com", "siteprice.org", "websiteoutlook.com", "who.is", "whois.com", "wikipedia.org",
    "thefreedictionary.com", "acronymfinder.com", "ibm.com", "learn.microsoft.com", "support.microsoft.com",
)
_OFFICIAL = re.compile(r"(\.gov|\.mil|\.edu)$|(^|\.)state\.[a-z]{2}\.us$|(^|\.)govdelivery\.com$", re.I)


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def domain_class(host: str | None, reference=DEFAULT_REFERENCE, official_extra=()) -> str:
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    if _matches(host, reference):
        return "reference"
    if _OFFICIAL.search(host) or _matches(host, official_extra):
        return "official"
    return ""


def cap_for(host: str | None, reference=DEFAULT_REFERENCE, official_extra=()) -> str:
    return {"reference": "irrelevant", "official": "borderline"}.get(domain_class(host, reference, official_extra), "")
