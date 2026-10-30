# Architecture

```
config/                    all tunables and the search-term library (YAML, no code changes needed)
src/wbscraper/
  cli.py app.py            commands and wiring
  config.py terms.py       settings and term library loading, compiled matchers
  queries.py               turns the term library into prioritised tasks (idempotent seeding)
  db.py                    SQLite (WAL): tasks, items, threads, provider health, statistics
  http.py                  shared client: pacing, jittered backoff, Retry-After, size caps, cooldowns
  runner.py                single-threaded loop: pick -> execute -> commit; stop conditions; Ctrl+C
  pipeline.py              redact -> score -> dedupe -> store, one place for all filtering
  relevance.py redact.py dates.py textutil.py htmltext.py robots.py export.py report.py rescore.py progress.py stats.py
                           (relevance.py: two-stage classifier; rescore.py: reclassify + prune stale work)
  sources/
    base.py                RawItem, Page, exceptions
    reddit_provider.py     provider interface + page helpers
    reddit_pullpush.py reddit_arcticshift.py reddit_search_discovery.py reddit_praw.py
    reddit_tasks.py        fallback chains, paging, window splitting, thread expansion
    handler_base.py        ApiHandler: scheduler hooks, stats/health bookkeeping for single-provider sources
    stackexchange.py hackernews.py github.py waterboards_docs.py web_pages.py   Phase 2 handlers (+ plan())
    bluesky.py lemmy.py mastodon.py youtube.py                                  Phase 3A handlers (YouTube off by default)
    registry.py            list of extra sources (name, role, credentials, module) used by app/CLI
tests/                     mocked network only (sockets are blocked by a fixture)
```

## Control flow

1. `queries.seed()` inserts tasks (`reddit_search`, `reddit_urls`, `reddit_discover`, …). A task's key is the hash of
   its type and payload, so re-seeding on resume never duplicates work, and newly added terms are simply new tasks.
2. `Runner` picks the highest-priority task whose first usable provider's domain is ready (per-domain pacing means
   PullPush, Arctic Shift and the search engine interleave while each site sees only slow serial traffic).
3. A handler executes it through a **provider chain**. Provider failures are recorded and the next provider is
   tried. If every provider fails, the task is deferred with exponential cooldown (10 min × 2ⁿ, up to 4 failed
   attempts, then `failed`). An exception inside an adapter fails that task only.
4. Results flow through `Pipeline.ingest`, which stores relevant/borderline items, then the handler enqueues
   follow-ups: next page, split windows, thread expansion, parent post lookup, newly seen subreddits.
5. Everything is committed after each task, so a crash loses at most the request in flight.

### Paging and history

Searches are sorted newest-first and paged by moving the `before` cursor to the oldest timestamp seen (+1 second,
so a boundary second is re-read and duplicates are dropped by id). A full page inside one second steps past it so
paging always terminates (a warning is logged). Search paging stops after `max_dry_pages` consecutive pages without
any entity hit, which stops fuzzy archives from paging through junk. A provider timeout splits the window in half
(bounded by `max_split_depth` and `min_window_days`) — for searches and for thread reads.

### Thread expansion

A post that is relevant on its own, or a comment that is, marks its thread relevant and queues a full read of the
thread (`comments/search?link_id=` paged by time, or PullPush `link_id`). Replies are then judged with the *narrow
thread exception* (below): a reply such as "the upload kept timing out for me too" qualifies without naming the
system, but "thanks, that worked" or an off-topic aside does not, however relevant the thread is. Only items that are
relevant on their own merits can flag a thread. If a thread only becomes relevant midway through its own expansion,
it is rescanned once. Threads surfaced by search engines are expanded at lower priority.

## Relevance classification

Two stages, decided by rules rather than a score threshold (`relevance.py`; vocabulary in `config/terms/`):

**Stage 1, connection** - what the text itself says about the Water Boards:

| Connection | Meaning |
|---|---|
| `system` | a named online system (SMARTS, GeoTracker, CIWQS, CalWATRS, …) or its domain |
| `org` | State/Regional Water Boards, a division, a `waterboards.ca.gov` address |
| `program` | a programme term (operator certification, stormwater jargon) *and* a California / Water Boards qualifier |
| `implicit` | evidence outside the text (a link post's URL, a Water Boards GitHub repository, a URL-only mention) |

Ambiguous acronyms are case-sensitive and need context words (`config/terms/entities.yaml`).

**Stage 2, usage** - evidence inside the same or neighbouring sentences (URLs ignored), from `signals.yaml`:
**A** interface vocabulary (website, portal, login, upload, e-file, dropdown…), **B** process vocabulary (submit,
renewal, account…), **C** experience wording, strong (error, confusing, "can't log in", how do I, easy…) or weak
(great, how to, does anyone…).

| Connection | relevant | borderline |
|---|---|---|
| system | any C with A or B, or two A | any A, B or C |
| org / program | A and strong C | A, or B and C |
| implicit | never | A and strong C |
| none, thread already relevant | A with strong C, A with two B, or two A | A, or B and C |
| none | never | never |

Look-alike patterns (`negatives.yaml`) demote one tier; strong ones (penalty ≥ 4) reject outright. Items from the
Water Boards' own GitHub repositories are capped at borderline. `score` in the database is only a sort key for review.
Items with a connection but no usage evidence are kept (`entity_hit`) so edited rules can resurrect them with
`wbscraper rescore`; items with no connection and no thread context are not stored at all.

`wbscraper rescore` recomputes every stored item, rebuilds the thread flags, forgets auto-discovered subreddits that
produced nothing relevant, and cancels *pending* tasks that existed only because of misclassified items (searches
in those subreddits, thread reads, post look-ups, retired queries). Raw rows and finished tasks are never touched.

## Data handling

* Only redacted text is stored; Reddit fields are mapped explicitly, and the author is never read. Arctic Shift
  requests ask only for text fields, so author data does not even arrive.
* Duplicates: platform id, canonical URL, and a normalised-text hash for texts of 80+ characters (short replies such
  as "same problem here" in different threads are distinct). Which providers found each item is recorded for the
  coverage report ("found only by this method").
* Dates: ISO 8601 UTC; implausible (pre-1995 or future) or missing dates become empty cells.
* Export is atomic (temp file + rename), `csv` module, `QUOTE_ALL`, UTF-8, exactly `text,date,source_url`.

## Documents and pages (Phase 2)

* **Comment letters.** `wb_page` tasks crawl the yearly "Documents for Public Comment" indexes, following only links
  whose text or URL mention comments/letters (depth ≤ 3, robots.txt obeyed, 3 s between requests). `wb_pdf` tasks
  download a PDF (≤ 25 MB), extract text with pypdf and classify it from its first page: *letter* (salutation,
  "on behalf of", "respectfully"…), *agency* (staff report, response/summary of comments, agenda, minutes, notice…) or
  *unknown*. Only letters are processed: the signature block is cut, text is grouped into ≤ 1,500-character
  passages that never cross a page, and each passage is scored with an implicit "State Water Board" anchor so it must
  show a digital/service signal (website, portal, login, upload, database…) to qualify. Each passage keeps the
  letter's date and a `#page=N` URL, and passages have no thread context, so policy-only text is never promoted.
* **Web pages.** Search-engine results go through `queue_discovered_url`, which skips login-walled domains and routes
  waterboards.ca.gov URLs to the document crawler. `web_page` tasks check robots.txt, then try JSON-LD, Discourse
  topic JSON, the WordPress comments API and finally the main article text. Authors are never read.

## Lightweight platforms (Phase 3A)

* **Bluesky** pages by *date window*, not cursor: `searchPosts` answers anonymous requests with `since`/`until`, and a
  window that returns a full page of 100 is halved (down to one day). Optional app-password login is only a fallback.
* **Lemmy / Mastodon** use keyless public endpoints per configured instance (each instance is its own rate-limited
  domain); refused instances are counted and skipped. Mastodon hashtag paging stops after three consecutive pages
  without any entity hit.
* **YouTube** is excluded unless `YOUTUBE_API_KEY` is set *and* the source is enabled; searches are capped by quota
  budget and comments are fetched only for videos whose title/description already match the system names.
* Optional/credential sources follow one rule: keyless modes are on by default, credential-only sources are off by
  default and enabled with `--enable <name>` or `sources.<name>.enabled: true`.

## Adding a source

Subclass `ApiHandler` (see `sources/hackernews.py` for a compact example): set `TASK_TYPES`, `PROVIDER`, `DOMAIN` and
write `_run_<task_type>(payload, priority)` methods that call `self.http`, build `RawItem`s and call `self.ingest()`.
Add a `plan(lib, settings, since)` function that returns its seed tasks, register it in `sources/registry.py`, add
settings/term-library entries and a mocked test. The core (HTTP, queue, pipeline, export, reporting) is unchanged.
