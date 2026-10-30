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
thread (`comments/search?link_id=` paged by time, or PullPush `link_id`). Comments are scored with a thread-context
bonus, so a reply such as "the upload kept timing out for me too" qualifies without naming the system. If a thread
only becomes relevant midway through its own expansion, it is rescanned once. Threads surfaced by search engines are
expanded at lower priority.

## Relevance scoring

`score = anchor + digital + experience + california + thread − penalties` (weights in `config/relevance.yaml`):

| Part | Value |
|---|---|
| anchor | 3.0 strong system/org name or Water Boards domain; 2.5 ambiguous name with required context; topic phrase 1.5; 0.5 ambiguous name without context (not an anchor) |
| digital | first technical/UX/process/digital term 1.5, +0.5 each further, cap 2.5 |
| experience | complaint/praise/question wording 0.5, +0.25 each, cap 1.0 |
| california | +1.0 when an anchor exists and a California context word appears |
| thread | +2.5 for items inside a thread already judged relevant |
| penalties | −1 organisation-only mention with no service signal; look-alike patterns (−2…−6) |

No anchor and no thread context caps the score at 1.0. Tiers: ≥ 4.0 relevant (exported), ≥ 2.5 borderline (stored,
counted, exported with `--include-borderline`), otherwise irrelevant (kept only if an entity matched, so rules can be
tuned with `wbscraper rescore`).

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
