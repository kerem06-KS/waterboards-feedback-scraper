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

> **Status:** Reddit (Phase 1) and Stack Exchange, Hacker News, GitHub, Water Boards comment letters and forum/blog
> pages (Phase 2) are implemented. Every platform investigated, including restricted ones, is in
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
| `wbscraper rescore` | Re-apply edited relevance rules to everything already collected (no network). |
| `wbscraper check` | One small request per provider to confirm connectivity. |
| `pytest` | Run the test suite (fully mocked; never touches the network). |

Useful `run` options: `--min-minutes 120`, `--max-minutes 480`, `--sources reddit_arcticshift,stackexchange,github`
(see `wbscraper sources` for all names; default is every enabled source),
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
  | Forums, blogs, Q&A | pages found by search engines: schema.org JSON-LD, Discourse topics, WordPress comments, article text | robots.txt obeyed; Facebook, Instagram, LinkedIn, X, TikTok, Nextdoor, YouTube pages are never fetched |

* **Ctrl+C** once: finish the current task, save, export, print the summary. Press it twice to leave immediately —
  state is committed after every task, so nothing is lost.
* **Rate limits** (all in `config/settings.yaml`): PullPush 1 request / 4 s, Arctic Shift 1 / 1.5 s, search engines
  1 / 15 s, others 1 / 3 s, plus random jitter, 10 s connect / 30 s read timeouts, up to 3 retries with exponential
  backoff + full jitter, `Retry-After` honoured, and a 10-minute cooldown after repeated failures.

---

## Relevance filtering (no paid AI)

Scoring is rule-based and fully configurable (`config/relevance.yaml`, `config/terms/*.yaml`):

* an **anchor** — a system/organisation name, a Water Boards domain, or a topic phrase such as "operator
  certification renewal" — is required, *or* the item must sit inside a thread that already contains a relevant item;
* ambiguous names (`SMARTS`, `GeoTracker`, `OCIS`, `FAAST`, `ELAP`, "water board") only count when supporting
  context is present, and look-alikes are penalised (GPS/SEO "GeoTracker" apps, the SMARTS solar model, waterboarding,
  other countries' water boards);
* technical, usability and process vocabulary (login, upload failed, confusing, processing time, accessibility…) and
  complaint/praise/question wording add to the score, so positive and neutral feedback is kept too;
* **relevant** items are exported; **borderline** ones are stored and counted in every summary
  (`wbscraper export --include-borderline` includes them) so nothing is silently discarded.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Query library: [docs/SEARCH_TERMS.md](docs/SEARCH_TERMS.md).

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

Other optional keys (all only raise limits): `GITHUB_TOKEN`, `STACKEXCHANGE_KEY`.

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
* Search-engine discovery (`ddgs`) is unofficial and may be throttled; it is deliberately slow.
* Many recent Water Boards comment letters are released only on request by e-mail, scanned letters need OCR (not
  implemented), and compiled response documents are skipped by design.
* Facebook groups, LinkedIn, Nextdoor and members-only professional forums are not collectible with permitted
  methods; see [docs/SOURCE_COVERAGE.md](docs/SOURCE_COVERAGE.md). The tool never claims complete coverage of the
  internet — use `wbscraper report` to see what was actually reached.
* Expect hundreds to low thousands of relevant items, not millions: these are niche systems.

## Troubleshooting

* **`wbscraper: command not found` / `No module named wbscraper`** — activate the virtualenv first. On macOS with
  Python 3.13+, if the project folder is flagged "hidden" by the OS, the editable install can be ignored; run
  `chflags nohidden .venv/lib/python*/site-packages/*.pth` or install non-editable with `pip install .`.
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
