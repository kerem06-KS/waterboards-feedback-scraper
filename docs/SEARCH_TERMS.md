# Search term library

Everything lives in YAML under `config/`; nothing is hard-coded. Preview with `wbscraper terms` or write every
planned query to a file with `wbscraper terms --dump plan.tsv`.

## Categories

| Category | File | Contents |
|---|---|---|
| A. Organisations and websites | `terms/entities.yaml` | State Water Resources Control Board and its variants (SWRCB, "State Water Board", "California Water Boards", common misnames), all nine regional boards by name and abbreviation (NCRWQCB … SDRWQCB), divisions (Drinking Water, Water Quality, Water Rights, Financial Assistance), domains (waterboards.ca.gov and its subdomains, swrcb.ca.gov, mywaterquality.ca.gov) |
| B. Online systems | `terms/entities.yaml` | SMARTS, GeoTracker (ESI/EDF/GEO_XY), CIWQS (eSMR, SSO), OCIS and the drinking-water/wastewater operator certification programs, CalWATRS/eWRIMS, Drinking Water Watch/SDWIS, eAR, ELAP, FAAST, CEDEN, GAMA, My Water Quality, Water Conservation Portal/SAFER dashboard, UST Cleanup Fund — each with aliases, jargon, tasks and, for ambiguous acronyms, the context required to count |
| C. Technical issues | `terms/modifiers.yaml` (`technical_issue`) | error, bug, crash, broken, login/password/authentication, timeout, slow, upload failed, submission failed, website down, payment issue, renewal problem, can't access/submit, help desk, no response … |
| D. User experience | `terms/modifiers.yaml` (`ux`, `process_delay`) | confusing, unclear, hard to navigate, can't find, outdated, clunky, mobile, accessibility/screen reader, PDF only, duplicate data entry, processing time, backlog … |
| E. Natural-language questions | `terms/questions.yaml` | "has anyone used {system}", "anyone else having trouble with {system}", "is {system} down", "how do I {task} in {system}", plus generic questions such as "how do I renew my water operator certification" |
| Relevance signals | `terms/signals.yaml` | Stage-2 vocabulary: interface terms (A), process terms (B), strong and weak experience wording (C). Not used to build queries |
| Look-alikes | `terms/negatives.yaml` | waterboarding, GPS/SEO/Houdini "GeoTracker", the SMARTS solar model, other countries' and states' water boards, promotional boilerplate |
| Subreddits | `sources/subreddits.yaml` | ~100 curated communities in three tiers (water/environmental professionals, California communities, adjacent trades/legal/property/agriculture/government); more are discovered at runtime |

## How terms become queries

* **Broad discovery vs. precision.** Tier-1 anchors are distinctive names (`CIWQS`, `CalWATRS`, `SWRCB`); tier 2–3
  add system-specific phrases (`GeoTracker upload`, `SMARTS annual report`). Subsumed phrases are dropped for
  subreddit searches (searching `GeoTracker` already returns `GeoTracker upload`), except ambiguous acronyms.
* **PullPush** receives only single distinctive tokens because its text search is fuzzy.
* **Arctic Shift** gets anchors per subreddit, capped by subreddit tier and kind (`reddit.anchors_per_subreddit`). Its
  post search is cheap, so posts get many anchors over one wide window; its comment text search is slow (it times out
  on wide ranges), so comments get few anchors, only in tier-1 subreddits, in pre-split 3-year windows, newest first.
* **Search engines** get quoted anchors, anchor × modifier combinations (`"CIWQS" login problem`) and the
  natural-language questions, each prefixed with `site:reddit.com` for Reddit discovery.
* **Topic phrases are not anchors on their own.** "Operator certification renewal", "Construction General Permit" and
  similar count only together with a California / Water Boards word, and still need interface evidence
  (see [ARCHITECTURE.md](ARCHITECTURE.md#relevance-classification)).
* **Ambiguous terms stay out of global searches.** PullPush's text search is fuzzy and case-insensitive, so it only
  receives distinctive tokens. Measured on the first collection: `WDID` (reddit slang for "what do I do") and `eSMR`
  (matches ESMR, a music crew and a cardiac therapy) pulled in relationship-advice, music and medical communities and
  were removed from every global/search-engine query. `WDID` now matches only in capitals with stormwater-permit
  wording; `eSMR` only in that exact case with water-quality wording. A regression test fails if an ambiguous
  acronym (`WDID eSMR SMARTS OCIS ELAP GAMA FAAST UPWARD DDW DWQ DFA eAR`) is ever sent as a bare global query.
  `retired_queries` in `config/relevance.yaml` lists queries whose pending searches `wbscraper rescore` cancels.
* **Date ranges.** All history by default (`date_range.since: 2005-06-01`); override with `--since/--until`.
