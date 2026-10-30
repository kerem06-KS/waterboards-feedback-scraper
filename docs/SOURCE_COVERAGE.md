# Source coverage matrix

Last researched: **2026-10-29**. "Verified" means a single small request or the provider's own documentation was
checked that day; "reported" means only third-party articles were found and the claim should be re-checked.
`wbscraper report` adds the real yields from your own runs. Complete coverage of the internet is not claimed.

Status legend: **Implemented** (works without credentials) · **Optional** (works with free credentials, off or
inactive until configured) · **Planned** (scheduled in a later phase) · **Restricted** (access controls or terms
prohibit automated collection; deliberately not collected) · **Future** (documented, not built).

## Priority 1: Reddit

| Method | Status | Findings | Cost / key |
|---|---|---|---|
| **PullPush** `api.pullpush.io` | **Implemented** (Phase 1) | Verified: global `q` search of posts and comments works, `before`/`after`, `link_id`, `ids`. `q` is fuzzy (quotes ignored, multi-word queries return noise) so only single distinctive tokens are sent. Reported: frequent 502/429, ~30 req/min cap, ingestion gaps. | free, no key |
| **Arctic Shift** `arctic-shift.photon-reddit.com` | **Implemented** (Phase 1) | Verified: current to Oct 2026; keyword search needs `subreddit`; `url=` prefix search works site-wide; `comments/search?link_id=` returns whole threads; heavy queries answer HTTP 422 timeout (handled by splitting the date window); OR queries time out, so single terms are used. Documentation gives no uptime guarantee. | free, no key |
| **Search-engine discovery** (`ddgs`) of Reddit threads | **Implemented** (Phase 1) | Unofficial metasearch (DuckDuckGo/Brave/Mojeek/Yahoo…); may be rate-limited, so 1 query / ~15 s. Finds threads the archives' keyword search misses; ids are then looked up and expanded. | free |
| **PRAW / official Reddit API** | **Optional** (Phase 1) | Reported: new apps need manual approval under Reddit's Responsible Builder Policy; reports say new requests close around 2026-10-31 and the Data API sunsets in 2027 (verify). Search is relevance-ranked, ~1,000 items per listing, no comment search. Used only as a fallback when `REDDIT_CLIENT_ID/SECRET` are set. | free but approval needed |
| reddit.com / old.reddit.com `.json`, HTML pages | **Restricted** | Reported: anonymous `.json` requests return 403 since mid-2026 and old.reddit moved behind login. Not used; no workaround attempted. | — |
| Reddit data dumps (Academic Torrents, Arctic Shift parquet) | **Future** | Multi-TB monthly dumps, no query layer; reported that Reddit began acting against public dumps in 2026. A local-file adapter could read per-subreddit files the user downloads. | free, large disk |
| Apify Reddit actors | **Future** | ~$5/month free credit; actors priced roughly $2–4 per 1,000 results (reported); terms-of-service caveats. | paid beyond free credit |
| THINKPOL and other commercial archives | **Future** | Exhaustive keyword search but priced per query. | paid |

## Priority 2: Public forums and Q&A

| Source | Status | Notes |
|---|---|---|
| Stack Exchange network (gis, engineering, earthscience, law, diy, …) | Planned (Phase 2) | Verified keyless API (300 requests/day; 10k with a free key). Note GeoTracker GPS-app false positives. |
| Hacker News (Algolia) | Planned (Phase 2) | Verified keyless; low yield. |
| GitHub issues (incl. the Water Boards' own `CAWaterBoardDataCenter`, 57 public repos) | Planned (Phase 2) | Keyless at low limits; optional `GITHUB_TOKEN`. |
| Professional operator/engineering forums, blogs, Q&A sites | Planned (Phase 2) | Found through search-engine discovery; robots-aware extraction of schema.org / Discourse / WordPress comment data. No dedicated California-operator public forum was found; CWEA/CASQA/AWWA communities are members-only. |

## Priority 3: Other platforms

| Platform | Status | Findings |
|---|---|---|
| Bluesky | Planned (Phase 3A) | Verified: `api.bsky.app` `searchPosts` answers 200 without login (page 1); `public.api.bsky.app` answers 403; paging reported to need an app password (optional credential). |
| Lemmy | Planned (Phase 3A) | Public `/api/v3/search` on instances; no key. |
| Mastodon | Planned (Phase 3A) | Hashtag timelines are public; full-text status search needs an account on an opted-in server (optional token). |
| YouTube comments | Planned (Phase 3A, optional) | Data API v3 needs a free key (10k units/day; search = 100 units, comments = 1). |
| Facebook / Instagram | **Restricted** | Login walls; automated collection is prohibited. Legitimate routes exist only through Meta Content Library (researcher application) or Graph API review for pages you manage. Not built. |
| LinkedIn, Nextdoor | **Restricted** | Login-gated; terms prohibit scraping; no public search API. Not built. |
| X / Twitter, Threads, TikTok, Yelp | **Restricted / paid** | X API is paid; Threads keyword search and TikTok Research API need app review/academic access; Yelp Fusion returns only three review excerpts. Not built. |

## Priority 4: Government-hosted public feedback

| Source | Status | Notes |
|---|---|---|
| waterboards.ca.gov comment-letter pages (`/public_notices/comments/`, 2007→) and PDFs | Planned (Phase 2) | robots.txt blocks only asset folders and some eWRIMS paths (verified). Many recent letters are released only by e-mail request to `commentletters@waterboards.ca.gov`. Agency summaries/responses are classified as agency documents and excluded from the CSV by default. |
| Board meeting materials, hearing submissions | Planned (Phase 2) | Same pipeline; scanned PDFs without a text layer are counted as a gap (OCR is a future enhancement). |

## Search and discovery infrastructure

| Option | Status | Notes |
|---|---|---|
| `ddgs` (DuckDuckGo/Brave/Mojeek… metasearch) | **Implemented** | Free, unofficial. |
| Google Custom Search JSON API | Not available | Closed to new customers; sunset 2027-01-01 (reported). |
| Bing Search API | Not available | Retired. |
| Brave Search API | Future | Now a $5 monthly credit and a credit card (reported). |
| SearXNG (self-hosted) | Future | Free; needs a local server. |
| Wayback Machine / Common Crawl | Future | URL discovery and dead-page retrieval only; no full-text search. |

## Not implemented on purpose

The tool never evades blocking: no CAPTCHA solving, proxy rotation, browser-fingerprint spoofing, login automation
or robots.txt circumvention. A blocked source is recorded as *restricted* in `wbscraper report`.
