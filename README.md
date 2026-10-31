# Water Boards Feedback Scraper

Collects **publicly available posts and comments about the usability of California Water Boards websites and
online systems** (SMARTS, GeoTracker, CIWQS, CalWATRS/eWRIMS, operator certification (OCIS), Drinking Water Watch,
eAR, FAAST, CEDEN, ELAP, the main waterboards.ca.gov site and more) and writes them to **one CSV** with exactly
three columns:

| column | meaning |
|---|---|
| `text` | full text of the public post or comment (contact details redacted, wording untouched) |
| `date` | original publication date, ISO 8601 UTC (`2024-03-05T14:22:10Z`); empty if unknown |
| `source_url` | direct public URL of the post or comment (the platform is visible from the URL) |

No usernames, author IDs, profile links, handles, e-mail addresses, phone numbers, sentiment labels or scores are
exported — and author data is never even stored in the local database.

The tool runs **locally** and is polite by design: per-domain rate limits, jittered exponential backoff, honest
`User-Agent`, checkpointing after every task, and graceful Ctrl+C. It does **not** bypass robots.txt, CAPTCHAs,
logins or other access controls; when a source blocks access the tool records it as *restricted* and moves on.

> **Status:** Reddit (Phase 1); Stack Exchange, Hacker News, GitHub, Water Boards comment letters and forum/blog
> pages (Phase 2); and Bluesky, Lemmy, Mastodon and YouTube (Phase 3A) are implemented. Every platform investigated, including restricted ones, is in
> [docs/SOURCE_COVERAGE.md](docs/SOURCE_COVERAGE.md).

---

## Quick start

Requires **Python 3.10 or newer** (tested on 3.14) and git. No paid services or API keys are needed.

### macOS / Linux

```bash
git clone https://github.com/kerem06-KS/waterboards-feedback-scraper.git
cd waterboards-feedback-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

wbscraper check       # optional: one tiny request per source to confirm connectivity
wbscraper run         # start collecting (Ctrl+C stops safely; see below)
```

To stop macOS from sleeping during a multi-hour run, prefix the command: `caffeinate -i wbscraper run`.

### Windows (PowerShell)

```powershell
git clone https://github.com/kerem06-KS/waterboards-feedback-scraper.git
cd waterboards-feedback-scraper
py -3 -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
pip install -e ".[dev]"

wbscraper check
wbscraper run
```

(Command Prompt: `.venv\Scripts\activate.bat`.) Disable sleep in *Settings → System → Power* for long runs.

### Everyday commands

| Command | What it does |
|---|---|
| `wbscraper run` | Start or continue a collection. Safe to repeat: finished work is never redone. |
| `wbscraper resume` | Same as `run`, for continuing after Ctrl+C, a crash or a lost connection. |
| `wbscraper status` | Progress from the database (use a second terminal while `run` is going). |
| `wbscraper export` | Rewrite the CSV now. `--include-borderline` adds uncertain items; `--excel` adds a UTF-8 BOM. |
| `wbscraper report` | Coverage report: which providers worked, failed, were restricted, unique threads per method. |
| `wbscraper sources` | List the source adapters and whether each is active. |
| `wbscraper terms` | Preview the generated search queries offline (`--dump plan.tsv` writes them all). |
| `wbscraper tidy-queue` | Maintenance for a queue built by an older version: re-ranks window-split Reddit searches breadth-first (so deep windows no longer outrank searches that never ran). `--cancel-stale` also cancels *pending* searches/thread reads that exist only because of items that are no longer relevant (what `rescore` does, without re-scoring). `--dry-run` reports only. Never touches finished tasks or collected data. |
| `wbscraper requeue-docs` | Queue second-pass tasks for Water Boards pages that an older version marked done without fetching (page budget). Additive and idempotent; `--dry-run` only counts them. `--unclassified` also re-queues PDFs from comment-letter pages that the first crawl could not classify (the link-context hint now classifies them). |
| `wbscraper rescore` | Re-apply the relevance rules to everything already collected (no network): reclassifies, cancels pending work that only existed because of earlier misclassifications, and regenerates both CSVs. Never deletes collected rows or resets finished tasks. `--keep-pending` also leaves every pending task alone. |
| `wbscraper check` | One small request per provider to confirm connectivity. |
| `pytest` | Run the test suite (fully mocked; never touches the network). |

Useful `run` options: `--min-minutes 120`, `--max-minutes 480`, `--sources reddit_arcticshift,stackexchange,github`
(only these), `--enable youtube` (these *in addition* to the defaults; see `wbscraper sources` for all names),
`--since 2020-01-01`, `--until 2025-12-31`, `--refresh` (incremental: only content newer than the last run),
`--fresh` (archive old state and start over), `--no-dashboard` (plain status lines, good for logs).

Output (all under `data/`, which is git-ignored):

```
data/output/waterboards_feedback.csv   <- the deliverable (text,date,source_url)
data/output/run_manifest.json          <- settings, term-file hashes, counts (reproducibility; kept out of the CSV)
data/output/coverage_report.md         <- from `wbscraper report`
data/state/collection.sqlite3          <- checkpoint database (resume, dedup, statistics)
data/logs/run-*.log                    <- detailed logs and errors (the terminal stays quiet)
```

---

## How a run behaves

* **Runtime.** `--min-minutes` (default 120) is *not* a sleep timer. It is the window in which work that failed or
  was deferred (for example a volunteer-run archive that is temporarily down) keeps being retried on a cooldown
  schedule. If everything is finished earlier, the run stops and says *exhausted*. `--max-minutes` (default 480) is
  a hard stop. A run also ends after the minimum if nothing new has been found for `stale_stop_minutes` (60) or only
  far-future retries remain.
* **Coverage first.** The dashboard's key number is *unique threads with relevant items*. Searches page backwards
  through all available history (since June 2005 by default); when an archive times out, the date window is split in
  half and retried; every relevant post — and the thread around every relevant comment — is expanded so comments
  buried deep in a thread are collected, including ones that never name the system.
* **Resilience.** Every Reddit capability has a fallback chain, so one dead service never stops the run:

  | Job | Primary | Fallbacks |
  |---|---|---|
  | Keyword search across all Reddit | PullPush | official API (if credentials), search-engine discovery |
  | Search inside one subreddit | Arctic Shift | PullPush, official API |
  | Links to `*.waterboards.ca.gov` | Arctic Shift (`url=` prefix) | PullPush |
  | Look up posts by id | Arctic Shift | PullPush, official API |
  | Read a whole thread | Arctic Shift | PullPush, official API |
  | Discover threads via search engines | `ddgs` metasearch | — |

* **Other sources** (all free, no key needed):

  | Source | What is collected | Notes |
  |---|---|---|
  | Stack Exchange | questions, answers and comments on gis, engineering, earthscience, law, diy, opendata, sustainability, superuser, stackoverflow | keyless quota is 300 requests/day; the adapter pauses when it runs low. A free `STACKEXCHANGE_KEY` raises it to 10,000 |
  | Hacker News | stories, and the full comment tree of relevant stories | Algolia API |
  | GitHub | issues/comments in the Water Boards' own data-team repos (non-staff authors only) and global issue search | unauthenticated limits are tight; set `GITHUB_TOKEN` (`export GITHUB_TOKEN=$(gh auth token)`) to speed up |
  | waterboards.ca.gov | **public comment letters** (PDF) found from the "Documents for Public Comment" pages, split into verbatim page-anchored passages (`…pdf#page=3`) | robots.txt obeyed. Agency staff reports, response-to-comments, comment summaries, agendas and minutes are **skipped** so one summary is never counted as many independent posts; skipped, unclassified and scanned PDFs are counted in `wbscraper status` / `report` |
  | Bluesky | public posts (searched by date window, so no login is needed) and reply threads | optional `BSKY_HANDLE` / `BSKY_APP_PASSWORD` are only a fallback |
  | Lemmy | posts, comments and threads from configurable instances (lemmy.world, lemmy.ml, …) | instances that refuse anonymous search are skipped |
  | Mastodon | public hashtag timelines (#waterboards, #swrcb, #stormwater …) and reply threads | `MASTODON_INSTANCE` + `MASTODON_ACCESS_TOKEN` add full-text search |
  | YouTube | video descriptions and comments for videos matching the system names | **off by default**: needs a free `YOUTUBE_API_KEY`; `wbscraper run --enable youtube` |
  | Forums, blogs, Q&A | pages found by search engines: schema.org JSON-LD, Discourse topics, WordPress comments, article text | robots.txt obeyed; Facebook, Instagram, LinkedIn, X, TikTok, Nextdoor, YouTube pages are never fetched |

* **Ctrl+C** once: finish the current task, save, export, print the summary. Press it twice to leave immediately —
  state is committed after every task, so nothing is lost.
* **Rate limits** (all in `config/settings.yaml`): PullPush 1 request / 4 s, Arctic Shift 1 / 1.5 s, search engines
  1 / 15 s, others 1 / 3 s, plus random jitter, 10 s connect / 30 s read timeouts, up to 3 retries with exponential
  backoff + full jitter, `Retry-After` honoured, and a 10-minute cooldown after repeated failures.

---

**Fair scheduling.** Reddit, the Water Boards PDFs, GitHub, Stack Exchange and the other sources each form a lane.
Priority only orders work inside a lane; lanes take turns, so a deep Reddit backlog cannot starve the PDF queue (see
`scheduler.weights` in `config/settings.yaml`). `wbscraper status` shows pending/done per task type, which is the quickest
way to confirm every source is moving.

## Relevance filtering (no paid AI)

Classification is rule-based, two-stage and fully configurable (`config/relevance.yaml`, `config/terms/*.yaml`).
An item reaches the CSV only if **both** stages pass on its own text:

1. **Connection to the California Water Boards.** A named Water Boards system (SMARTS, GeoTracker, CIWQS, CalWATRS…),
   the State/Regional Boards or a division, a `waterboards.ca.gov` address, or a programme term (operator
   certification, stormwater permit jargon) together with a California / Water Boards qualifier. Ambiguous names
   (`SMARTS`, `WDID`, `eSMR`, `GeoTracker`, `OCIS`, `FAAST`, `ELAP`, "water board") count only in the right case and
   with their supporting words, and look-alikes are rejected (GPS/SEO "GeoTracker" apps, the SMARTS solar model,
   `WDID` = "what do I do", ESMR music, waterboarding, other states' and countries' water boards).
2. **Use of a website, portal or digital process.** Interface vocabulary (website, portal, login, upload, e-file,
   dropdown…) next to experience wording (confusing, can't log in, how do I, easy…). Generic words such as "water",
   "system", "error", "account" or "login" never qualify on their own. Both stages are judged inside the same or
   neighbouring sentences, so a Water Boards mention cannot borrow "website" from an unrelated paragraph, and URL
   text is ignored.

A reply inside a thread that is *already* established as relevant needs no connection of its own, but must itself
talk about the interface ("the upload kept timing out for me too"). "Thanks, that worked!" does not qualify. Interface
words that are also ordinary words (filter, button, menu, layers, drop down, outage, Chrome ...; group
`usability_ambiguous` in `signals.yaml`) count next to a Water Boards connection but never carry this exception, so a
thread about refrigerator water filters cannot qualify on "the filter fails".
Context that is only implied (a link to the site, a Water Boards GitHub repository) can never make an item
relevant by itself.

**What kind of item it is matters.** A person's post or comment is judged on its wording (above). Authored documents
(web articles and comment-letter PDF passages) are full of generic process words, so they need a genuine complaint
(praise, "how do I" headings and "denied" do not count), and are never promoted by a shortcut. Beyond that:

* **A person using a named system counts.** "Go to the GeoTracker map and click on your site", "I can log in to the
  regulator portal for GeoTracker", "What trips people up about GeoTracker is…" are discussions of using a Water Boards
  system even without a complaint. The rule is deliberately narrow: a system must be named (an ambiguous name such as
  GeoTracker needs its cleanup/Water Boards context), and a use or interface word (*use the, look up, search, go to,
  check out, can be found, upload, portal…*) must be in the **same sentence**, outside the system's own name. It
  does not apply to authored documents, to GitHub project notes, or to a system that is merely cited.
* A passing mention is not usage: "…the website. Groundwater moves extremely slow" does not qualify. A complaint must
  sit in the same sentence as the interface word it describes (or right across a sentence boundary, with a specific
  element such as *upload* or *login* rather than the bare word *website*). Weak wording (*great, confirm, helpful,
  question*) never makes an item relevant. Requests for help (*how to, does anyone know*) and short walkthroughs
  (*go to the map, click on…*) count when a named system is involved.
* Pages from agency or institutional sites (`*.gov`, `*.edu`, `govdelivery.com`) are announcements, not feedback: at most
  borderline. Site checkers, technology profilers, dictionaries and vendor manuals are never relevant and not fetched
  (`web_pages.reference_domains` / `official_domains` in `config/sources/forums.yaml`).
* Vendor marketing ("request a demo", "cloud-based solution") and code-change titles (`feat(data): …`) are rejected.
* A PDF is a public comment letter only if it has a salutation or sign-off and does not start with the agency's own
  name; orders, complaints, minutes and the Board's own transmittals are agency documents.

* **relevant** items are exported to `waterboards_feedback.csv` (exactly `text,date,source_url`);
* **borderline** items (a connection but thin or neutral usage evidence) are stored and written to a separate
  `borderline_review.csv` for manual review (it adds a `review_note` column, so it is *not* the deliverable);
  `wbscraper export --include-borderline` adds them to the main CSV if you decide to;
* everything else stays in the SQLite database (`items.tier = irrelevant`) so rules can be tuned later without
  re-collecting.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Query library: [docs/SEARCH_TERMS.md](docs/SEARCH_TERMS.md).

## Analysis step: sentiment, tasks and pain points

A separate step that reads the exported CSV (the scraper and `waterboards_feedback.csv` are never touched):

```bash
pip install -e ".[analysis]"
wbscraper analyze                 # sentiment score + label, review file, summary (offline, free)
wbscraper analyze --tag --dry-run # how many posts would be sent to the Claude API, rough cost
wbscraper analyze --tag           # also task (every post) and pain point (negative posts); needs ANTHROPIC_API_KEY
```

It writes `data/output/waterboards_feedback_scored.csv` (the three original columns plus score, label, task, pain point),
`data/output/sentiment_summary.md`, and `data/analysis/review.csv` for manual corrections. Sentiment counts only opinions
about a Water Boards website or system itself, and percentages are shares of the collected posts, not user satisfaction.
Method, measured accuracy and limits: [docs/SENTIMENT.md](docs/SENTIMENT.md).

## Extending the search terms (no code changes)

Edit the YAML files under `config/`, then run `wbscraper terms` to preview and `wbscraper run` to collect — new
searches are queued automatically and finished ones are not repeated. `wbscraper rescore` re-applies edited
relevance rules to stored items.

* `config/terms/entities.yaml` — organisations, systems, aliases, ambiguous acronyms and their required context
* `config/terms/modifiers.yaml` — technical/UX/process/experience vocabulary and query modifiers
* `config/terms/questions.yaml` — natural-language question templates
* `config/terms/negatives.yaml` — look-alike penalties
* `config/sources/subreddits.yaml` — curated subreddits (more are discovered automatically)

## Optional credentials

Nothing is required. To use the official Reddit API as an extra fallback (new apps now need Reddit's manual
approval), install the extra and set environment variables (or copy `.env.example` to `.env`):

```bash
pip install -e ".[reddit]"
export REDDIT_CLIENT_ID=...  REDDIT_CLIENT_SECRET=...  REDDIT_USER_AGENT="waterboards-feedback-scraper by u/yourname"
wbscraper sources      # praw should now show as active
```

Windows PowerShell: `$env:REDDIT_CLIENT_ID="..."`.

Other optional keys: `GITHUB_TOKEN` and `STACKEXCHANGE_KEY` only raise rate limits; `MASTODON_*` adds search;
`BSKY_*` is a fallback. **YouTube** needs a key and is off by default:

```bash
# 1. Google Cloud Console -> create a project -> enable "YouTube Data API v3" -> Credentials -> API key (free)
export YOUTUBE_API_KEY=...
wbscraper run --enable youtube      # in addition to the default sources
```

(The free quota is 10,000 units/day; `youtube.max_search_calls` in `config/sources/forums.yaml` caps searches.)

## Privacy and responsible use

* Only public content is collected, through documented/public interfaces, at conservative request rates.
* Contact details inside text (e-mail, phone, `u/name`, `@handle`, profile URLs, street addresses) are redacted;
  author names/IDs are never read into storage. Reddit's usernames are therefore absent from the database too.
* Use the data in line with the terms of the sources, and consider the context of what people wrote; exported text
  is verbatim public speech. If a cell starts with `=`, `+`, `-` or `@`, a spreadsheet may treat it as a formula —
  open the CSV as text/import it rather than double-clicking if that matters to you.

## Limits you should know about

* PullPush and Arctic Shift are volunteer-run archives with no uptime guarantee; their coverage of recent content
  can lag, and deleted/removed posts are gone. `wbscraper report` flags a provider whose newest record looks stale.
* Arctic Shift's comment text search is slow and times out on wide date ranges (measured 2026-10-29), so
  subreddit comment searches use pre-split 3-year windows and a small anchor budget (`reddit.anchors_per_subreddit`,
  `reddit.comment_window_years` in `config/settings.yaml`); comments inside relevant threads are collected by thread
  expansion regardless, which is fast.
* **Arctic Shift "query timed out" is random, not a sign of a wide window.** Measured on a real run: about two thirds of
  scoped searches time out at every window width and age, and sibling windows fail independently. A timed-out window is
  therefore **retried** (`reddit.timeout_retries`, default 2) before it is split, and each split level runs at a lower
  priority (`reddit.split_priority_decay`). Splitting on the first timeout (the old behaviour) cost about 50 requests per
  query/subreddit instead of about 5 and starved the searches that had not run yet. Scoped subreddit search also
  turned out to add almost nothing that the global PullPush search and thread expansion had not already found, so a
  newly discovered subreddit gets the same budget as a tier-1 curated one (`reddit.discovered_subreddit_anchors`).
* Search-engine discovery (`ddgs`) is unofficial and may be throttled; it is deliberately slow.
* Many recent Water Boards comment letters are released only on request by e-mail, scanned letters need OCR (not
  implemented), and compiled response documents are skipped by design.
* Facebook groups, LinkedIn, Nextdoor and members-only professional forums are not collectible with permitted
  methods; see [docs/SOURCE_COVERAGE.md](docs/SOURCE_COVERAGE.md) for the legitimate (usually research-only) routes.
  Bluesky and Mastodon post URLs necessarily contain a DID / account name, because that is how those platforms
  address posts; no other account information is stored. The tool never claims complete coverage of the
  internet — use `wbscraper report` to see what was actually reached.
* **Expect tens, not thousands.** Measured on a real collection (15,700 stored items from 11 sources), only about 340 items
  named a specific Water Boards system and about 15 were genuine usability feedback once agency pages, vendor
  marketing, passing mentions and agency documents were excluded. These are niche systems used mostly by professionals
  (operators, consultants, dischargers) who discuss them in members-only venues. `wbscraper report` has a *Funnel*
  table showing, per source, where volume turns into (or fails to become) usable feedback. More yield comes from
  finishing the queue, the Water Boards comment-letter crawl (`wbscraper requeue-docs`), and optional credentials
  (YouTube key, `GITHUB_TOKEN`), not from loosening the filter.

## Troubleshooting

* **`ModuleNotFoundError: No module named 'wbscraper'` / `command not found`** — activate the virtualenv first. If it
  still fails on macOS, the OS has probably marked the editable-install file as *hidden* (Python 3.13+ ignores hidden
  `.pth` files; this happens in iCloud-synced folders such as Desktop and Documents). Quick fix:
  `chflags nohidden .venv/lib/python*/site-packages/*.pth`. Lasting fix: keep the virtualenv outside synced folders,
  e.g. `python3 -m venv ~/.venvs/wbscraper && source ~/.venvs/wbscraper/bin/activate && pip install -e ".[dev]"`.
  Last resort without any install: `PYTHONPATH=src python -m wbscraper run`.
* **Lots of `deferred` tasks / `restricted`** — a provider is down or throttling. This is normal; work is retried and
  fallbacks are used. Check `data/logs/` and `wbscraper report`.
* **Nothing found from one provider** — run `wbscraper check`; the archives sometimes answer 502/429 for hours.
* **Start completely over** — `wbscraper run --fresh` (the old database is archived, not deleted).

## Development

```bash
pip install -e ".[dev]"
pytest                    # mocked network only; real sockets are blocked by the test fixtures
```

Project layout: see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Source adapters live in
`src/wbscraper/sources/`; adding a platform means one adapter file plus term-library entries.

## License

MIT — see [LICENSE](LICENSE).
