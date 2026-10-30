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
| Stack Exchange network (gis, engineering, earthscience, law, diy, …) | **Implemented** (Phase 2) | Verified keyless API (300 requests/day; 10k with a free key). Note GeoTracker GPS-app false positives. |
| Hacker News (Algolia) | **Implemented** (Phase 2) | Verified keyless; low yield. |
| GitHub issues (incl. the Water Boards' own `CAWaterBoardDataCenter`, 57 public repos) | **Implemented** (Phase 2) | Keyless at low limits; optional `GITHUB_TOKEN`. |
| Professional operator/engineering forums, blogs, Q&A sites | **Implemented** (Phase 2) | Found through search-engine discovery; robots-aware extraction of schema.org / Discourse / WordPress comment data. No dedicated California-operator public forum was found; CWEA/CASQA/AWWA communities are members-only. |

## Priority 3: Other platforms

### Accessible with free, permitted public APIs (Phase 3A, implemented)

| Platform | Status | Findings |
|---|---|---|
| Bluesky | **Implemented**, on by default, no credentials | Verified 2026-10-29: `api.bsky.app` `searchPosts` answers anonymous requests including `since`/`until` windows; cursor paging is reported to be refused anonymously, so the adapter pages by splitting date windows instead. `getPostThread` is public. Optional `BSKY_HANDLE`/`BSKY_APP_PASSWORD` are used only as a fallback if anonymous access is refused. Post URLs contain the account's DID (opaque id), as Bluesky's own URLs do. |
| Lemmy | **Implemented**, on by default | Verified: public `/api/v3/search` works without a key on lemmy.world; instances can restrict anonymous search (handled: counted and skipped). Instances are configurable in `config/sources/forums.yaml`. |
| Mastodon | **Implemented**, on by default (hashtag timelines); **Optional** full-text search | Verified: `/api/v1/timelines/tag/<tag>` is public on mastodon.social. Full-text status search needs an account token on an opted-in server: set `MASTODON_INSTANCE` and `MASTODON_ACCESS_TOKEN`. Mastodon's own status URLs contain the account name. |
| YouTube comments | **Optional**, **disabled by default** | Official Data API v3 needs a free Google Cloud key (`YOUTUBE_API_KEY`); default quota 10,000 units/day, a search costs 100 units, a comment page 1 unit. Enable with `wbscraper run --enable youtube`. Only videos whose title/description match the system names have their comments fetched. |

### Restricted platforms (not collected; legitimate routes documented)

Researched 2026-10-29 from vendor documentation and third-party reports (verify before applying). None of these is
accessed by this tool, and nothing in it evades their access controls.

| Platform | Why it is not collected | Legitimate access route (if you want it) |
|---|---|---|
| Facebook / Instagram | Login walls; terms prohibit automated collection | **Meta Content Library + API**: free, for researchers affiliated with a qualifying academic/research institution, annual re-certification, ~500,000 records per 7-day window, runs inside a secure research environment and exports are restricted ([Meta docs](https://developers.facebook.com/docs/content-library-and-api/get-access)). **Instagram Graph API hashtag search**: needs a Business/Creator account and Meta app review; reported cap of 30 unique hashtags per 7 days and only recent posts. Neither fits an unaffiliated, general-purpose collection. |
| Threads | Keyword search is permission-gated | `threads_keyword_search` permission via Meta app review; without approval only the authenticated user's own posts are searchable ([docs](https://developers.facebook.com/documentation/threads/keyword-search)). |
| LinkedIn | Login-gated; no public post-search API | Community Management API covers organisations you manage (reads comments on your own posts) and requires application and approval ([docs](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/)). Not usable for public discussions about the Water Boards. |
| Nextdoor | Login-gated; official APIs are partner-gated | developer.nextdoor.com offers advertising, publishing and approved-partner content APIs (e.g. public agency feed); there is no general public read API. |
| X / Twitter | Paid | Pay-per-use since 2026-02-06 (reported ~$0.005 per post read; recent-search window of 7 days; no free tier for new developers). Optional paid future adapter. |
| TikTok | Research-only | Research API for independent, non-profit academic researchers with ethics approval and a project application; long review times reported ([FAQ](https://developers.tiktok.com/doc/research-api-faq)). |
| Yelp | Not a source of full reviews | Fusion API returns only a few ~160-character excerpts per business (reported); of little value here. |

## Priority 4: Government-hosted public feedback

| Source | Status | Notes |
|---|---|---|
| waterboards.ca.gov comment-letter pages (`/public_notices/comments/`, 2007→) and PDFs | **Implemented** (Phase 2) | robots.txt blocks only asset folders and some eWRIMS paths (verified). Many recent letters are released only by e-mail request to `commentletters@waterboards.ca.gov`. Agency summaries/responses are classified as agency documents and excluded from the CSV by default. |
| Board meeting materials, hearing submissions | **Implemented** (Phase 2; reached only through comment pages and search-engine discovery) | Same pipeline; scanned PDFs without a text layer are counted as a gap (OCR is a future enhancement). |

## Search and discovery infrastructure

| Option | Status | Notes |
|---|---|---|
| `ddgs` (DuckDuckGo/Brave/Mojeek… metasearch) | **Implemented** | Free, unofficial. |
| Google Custom Search JSON API | Not available | Closed to new customers; sunset 2027-01-01 (reported). |
| Bing Search API | Not available | Retired. |
| Brave Search API | Future | Now a $5 monthly credit and a credit card (reported). |
| SearXNG (self-hosted) | Future | Free; needs a local server. |
| Wayback Machine / Common Crawl | Future | URL discovery and dead-page retrieval only; no full-text search. See [FUTURE_ENHANCEMENTS.md](FUTURE_ENHANCEMENTS.md). |

## Not implemented on purpose

The tool never evades blocking: no CAPTCHA solving, proxy rotation, browser-fingerprint spoofing, login automation
or robots.txt circumvention. A blocked source is recorded as *restricted* in `wbscraper report`.
