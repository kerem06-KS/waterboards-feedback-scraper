# Sentiment, tasks and pain points (analysis step)

A separate step that reads the exported CSV. It never touches the scraper, its database or `waterboards_feedback.csv`, so it can be re-run at any time without scraping.

```
wbscraper analyze                  # score + label, review file, summary (offline, free)
wbscraper analyze --tag --dry-run  # how many posts would go to the API, rough cost; no calls
wbscraper analyze --tag            # also task + pain-point tags from the Claude API (needs ANTHROPIC_API_KEY)
wbscraper analyze-eval             # accuracy against the hand-labelled posts
```

Install the extra first: `pip install -e ".[analysis]"`.

## Outputs

| File | What it is |
|---|---|
| `data/output/waterboards_feedback_scored.csv` | The three original columns, then `sentiment_score` (always the original VADER score) and `sentiment_label` (the **final** label), `task` (every post), `pain_point` (wherever there is a genuine website or system complaint), then `vader_label` (what the VADER score alone gives), `label_source` and `tag_source` (`human_override`, `ai_review`, `vader` or `api_tag`), `confidence`, `explanation` and `needs_review`. |
| `data/output/sentiment_summary.md` | Shares by label, source and system; tasks; pain points; the posts with a complaint. |
| `data/analysis/review.csv` | One row per post for manual checking (below), including the AI review and the final values. Opens in Excel / Google Sheets. |
| `data/analysis/ai_review.csv` | The AI-assisted review of individual posts (below). Read on every run, never rewritten by it. |

## What "sentiment" means here

A post is **positive or negative only when it gives an opinion about a Water Boards website or online system itself**
("GeoTracker is an amazing site", "the CalWATRS upload doesn't work", "the websites are so confusing").
Worry about contamination, comments on regulations and neutral recommendations ("check GeoTracker near you") are **neutral**.
Plain VADER does not make that distinction: it scores `spills`, `worry` or `violation` as negative feelings and `smarts` (the SMARTS system) as praise.

Percentages in the summary are **shares of the posts collected**. The posts are self-selected, found by searching for specific systems, and mostly from Reddit and about GeoTracker. They are not a survey and say nothing about overall user satisfaction.

## How the score is made

1. **Clean** the text: drop quoted lines (`> ...`, someone else's words), URLs, markdown link targets, code and `[REDACTED_*]` placeholders.
2. **Split into sentences, then into clauses** at "but", "however", "although", "though", "yet", ";". VADER's own "but" rule halves everything before the "but", so "Unfortunately it's not up to date, but the staff were friendly" used to lose its complaint; each clause is now scored on its own.
3. **Keep the clauses that are about a site or system** (everything else is ignored; no such clause means a score of 0). A clause counts when it:
   * names a system (the same matcher the relevance filter uses), or an interface word (website, portal, map, dashboard, upload, login...); or
   * states an **inability, limit, missing feature, stale content or missing data** ("can't find", "only one X can be entered", "no way to export", "not up to date", "shows nothing") in a post that is about a named system or the Water Boards, even if the clause does not repeat the name; or
   * is a **service complaint** ("completely backlogged", "waiting six weeks", "everything is so scattered") that names the Board, an office or a system; or
   * starts "It...", "This...", "The site..." right after one of the above.

   **Agency documents and comment letters** (waterboards.ca.gov) are scored strictly: only a clause that names a system counts, because letters and regulations rarely give an opinion about a website.
4. **Score each clause with VADER** after the adjustments in `config/analysis/lexicon.yaml`: topic and legal words set to 0; usability and subtle-frustration words and phrases added; the complaint patterns above scored as units; courtesy formulas removed.
5. **Post score ("vote").** A clause at or below −0.3 is a *complaint*, at or above +0.3 *praise*. If there is at least one complaint and complaints are at least as many as praise, the post score is the mean of the complaint clauses; if praise outnumbers complaints, the mean of the praise clauses; otherwise the mean of the non-zero clauses. Praise can no longer average away genuine complaints, and a tie goes to the complaint (those posts are also flagged "mixed opinions" for review).
6. **Label** by the two cutoffs in `config/analysis/settings.yaml`: negative ≤ −0.2, positive ≥ 0.55, otherwise neutral.

Package: **`nltk-vader-sentiment` 1.0.0** (a maintained re-implementation of VADER that fixes about 18 upstream bugs, such as the `but` handling, curly-apostrophe negation, and "no" at the end of a sentence). Against the original `vaderSentiment` 3.3.2 it gives the same results on this project's data and the same correlation with the human ratings of the original VADER paper (below). `vaderSentiment` stays installed for the comparison.

## Measured accuracy

All labels are mine, written from the definition above before any scoring. Three labelled sets exist (all local; they hold post text):

| Set | Posts | Negative / neutral / positive | Used for |
|---|---|---|---|
| Relevant posts (the deliverable CSV) | 71 | 10 / 56 / 5 | Reported separately. Not used for choosing rules or cutoffs. |
| Tuning sample (borderline posts) | 449 | 12 / 420 / 17 | Development |
| Extra posts from the database that name a system | 227 | 6 / 215 / 6 | Development (added in Round 2) |

The development set (676 posts, **18 negatives**) is where every rule and cutoff was chosen. Precision there looks low because only 2.7% of those posts are negative; among the 71 relevant posts 14% are.

### Round 2 (this version) against Round 1

| Method | Dev: negatives found (of 18) | Dev: precision | Dev: AUC | Dev: found at 5% false positives | Relevant: negatives found (of 10) | Relevant: precision | Relevant: macro-F1 |
|---|---|---|---|---|---|---|---|
| Default VADER, whole text, ±0.05 | 33% | 5% | 0.58 | 0% | 3 (30%) | 19% | 0.26 |
| **Round 1** (mean of sentences, cutoffs −0.3 / 0.55) | 17% | 9% | 0.50 | 17% | 3 (30%) | 100% | 0.52 |
| **Round 2** (clauses, complaint patterns, vote, strict documents; cutoffs −0.2 / 0.55) | **50%** | **15%** | **0.66** | **39%** | **6 (60%)** | **60%** | **0.58** |

("AUC" is the chance that a hand-labelled negative scores lower than another post; 0.5 is chance. "Found at 5% false positives" fixes how many other posts are wrongly flagged, so it does not depend on the cutoffs.) Overall accuracy on the 71 is 82% in both rounds (79% for always saying "neutral"); the confusion matrix for Round 2 is 6/4/0 for the ten hand-labelled negatives (negative/neutral/positive), 3/51/2 for the neutrals and 1/3/1 for the positives.

Round 1 found the negatives it was sure about and missed most of the rest. Round 2 trades some precision for recall: among the 71 it flags 10 posts as negative, 6 of them correctly. The 4 wrong ones (by my labels) are two agency documents (a grant instruction, "allow time for FAAST staff assistance, should any submittal problems occur", which is hypothetical wording the scorer does not recognise, and a comment letter about reporting burden that names CEDEN), one neutral Reddit post and one positive one. The 4 negatives still missed are mild or implied complaints: "learning GeoTracker is hard", "what trips people up", and two help-seeking posts that never say the site is bad.

### What each change adds (development set, locked cutoffs)

| Variant | Macro-F1 | Dev negatives found | AUC |
|---|---|---|---|
| All Round 2 rules | 0.44 | 50% | 0.66 |
| Without complaint patterns | 0.42 | 33% | 0.58 |
| Mean instead of vote | 0.44 | 39% | 0.62 |
| Without clause splitting | 0.42 | 44% | 0.65 |
| Without subtle-frustration words | 0.44 | 50% | 0.64 |
| Documents scored like posts | 0.41 | 50% | 0.66 |
| Whole post, no clause selection | 0.34 | 61% (precision 7%) | 0.68 |

The complaint patterns matter most, then the vote and clause splitting. Selecting only the clauses about a site is what keeps precision up: scoring the whole post finds slightly more negatives but flags far more neutral posts.

### What was tried and did not work

* **Summing sentence scores** (so many small complaints add up) was no better than the vote on the development set.
* **Treating every sentence of a short post as about the system** hurt precision (relevant-post precision fell to 42%) and was dropped.
* A first set of complaint patterns and interface words was **too loose**. Words such as "page", "form", "link", "app" and "account" matched "Page 3 of 12", "form of", Reddit accounts; "can't work", "cannot be filtered" and "is not edited" matched job rules, water filters and pull-request text. The development set exposed this (precision 7–8%), and the words and patterns were tightened before the final comparison.

### How overfitting was limited, and what is still not clean

* The first round's results had already shown me *kinds* of misses on the 71 (long posts where praise outweighs a list of defects; complaints phrased without a negative word). Round 2 was built to address those kinds of misses, so **the 71 are no longer an untouched test**; treat their numbers as an illustration, not a score.
* So that the rules would not be fitted to those 71 posts, the development data were enlarged (449 → 676 posts, 12 → 18 negatives, a different mix of sources), the new rules were written from general ways people describe software problems (inability, limits, missing features, stale content, waiting), and every comparison above was decided on the development set. The 71 were not used to choose between variants; only the two cutoffs were tuned (on the development set, from the middle of the plateau where recall and false positives stay flat, so they are not a knife-edge choice).
* Still small: **18 development negatives and 10 relevant negatives**. A single post moves recall by 6 points (dev) or 10 points (relevant). The confidence intervals are wide (relevant macro-F1 0.58, 95% interval 0.41–0.75).
* One labeller (me). 36 development posts and 8 relevant posts are marked "unclear".
* Remaining misses: complaints with no named system or site word in the clause ("Anyone else stuck in limbo here?"); sarcasm; complaints phrased as questions; mild comparatives. The review file lists the clauses behind each score, so these are quick to correct by hand.

General-text check (correlation with the human ratings of Hutto & Gilbert 2014): the domain word list leaves behaviour on ordinary text essentially unchanged (tweets 0.872 / 0.875 / 0.861 for VADER 3.3.2 / nltk-vader-sentiment / with the domain list; movie reviews 0.427 / 0.427 / 0.428; Amazon 0.590 / 0.589 / 0.610; NYT editorials 0.503 / 0.504 / 0.487).

Re-run any time: `wbscraper analyze-eval --icwsm data/analysis/icwsm`. It never changes `config/analysis/settings.yaml`; cutoffs are edited by hand.

## Tasks and pain points (Claude API)

`wbscraper analyze --tag` sends each post (never usernames: the CSV has none) to `claude-opus-5-5` with a fixed list of choices (`config/analysis/tags.yaml`) and a JSON schema, so the answer is always one of the allowed values. Each call returns:

* **task**, for every post: what the person came to the site to do (or is recommending others do), plus a short free-text `task_detail`;
* **pain point**: why it went badly, from five categories (can't find information; outdated or conflicting pages; hard-to-read PDFs and forms; online systems like SMARTS; reaching staff) plus "Other / not a website problem" and "None". The scored CSV shows it **only for negative posts**.

"Outdated or conflicting pages" is defined as stale content, broken or moved links, old dates, and instructions that contradict each other across pages. Edit the definitions in `tags.yaml` if the Guidesheet's Issues column says otherwise, then raise `tagging.prompt_version` in `settings.yaml`.

* **Opt-in and cached.** Plain `analyze` never spends money. Results are cached in `data/analysis/tag_cache.json` by post text + prompt version + model, so re-runs are free and the answers stay fixed (this model has no temperature setting). `--retag` refreshes them. Refusals and API errors are not cached and are retried.
* **Cost:** about $0.30 of input for the 71 posts at the configured price plus a few cents of output; each run prints the actual token use. If no key is set, tagging is skipped with a message and everything else still runs.
* Refusal fallback (`fallbacks: default`) is on; set `tagging.fallbacks: off` to disable.

## AI-assisted review of the 71 posts

On 2026-10-31 every one of the 71 posts was re-read against the project definitions (sentiment only for opinions about a Water Boards website or system; the task list and pain-point categories in `config/analysis/tags.yaml`) and given a label, task, pain point (only where a genuine website or system complaint exists), a confidence level (high / medium / low), a one-line explanation and an "uncertain" flag. The reviewer was Claude working inside a Claude Code session; no paid API was used. It started from my earlier hand tags but re-checked each one against the text. It is a second pass by the same reviewer, not an independent person, so treat it as careful but not as ground truth.

Result: 67 of the 71 hand tags were confirmed. Four changed: r47 (a helper's reassurance that learning GeoTracker is hard) went from negative to neutral with no pain point; the task of r38 became "look up data", of r44 "reach staff or request records", and of r01 "log in or manage an account". Compared with VADER, 12 final labels differ (VADER: 10 negative, 58 neutral, 3 positive; reviewed: 9 negative, 57 neutral, 5 positive). 55 classifications are high confidence, 10 medium, 6 low; 9 are flagged `needs_review`.

How it is stored, and how it survives later runs:

* The reviewed values live in `data/analysis/ai_review.csv`, keyed by source URL + text hash (the same key as `review.csv`). `analyze` only reads it, so re-running, re-tagging with `--tag`, or changing VADER settings cannot lose it. A row whose post has left the input is kept but not applied; a row with a value outside the allowed lists is ignored with a warning.
* Precedence for each final value: **human override** (`label_override`, `task_override`, `pain_point_override` in `review.csv`) → **AI review** → API tag (task and pain point) / VADER (label). Human overrides are never overwritten; `review.csv` is rewritten each run with your override columns carried over, and the previous copy is kept as `review.csv.bak`.
* The original CSV and the VADER score are untouched: `sentiment_score` is the VADER score, `vader_label` the label it gives, and `waterboards_feedback.csv` is never written.
* Posts added by a later scrape have no AI review yet, so they get VADER's label, the API tag if `--tag` is used, and `needs_review = yes` when something looks off.

## Manual review

Open `data/analysis/review.csv`. Rows with something worth a look come first, with the reason in `review_reasons`: score within 0.10 of a cutoff, mixed opinions, no sentence about a site/system, hand label differs from the scorer (the 71 relevant posts), low tag confidence, task "Other"/"Unclassified", or a problem the tagger sees in a post not labelled negative. The `focus_sentences` column shows exactly which sentences produced the score.

The review file also shows the AI review (`ai_*` columns) and the final values (`final_*`, `label_source`, `tag_source`). Fill any of `label_override` (negative / neutral / positive), `task_override`, `pain_point_override` (exact names from `tags.yaml`, case-insensitive), `note`, then run `wbscraper analyze` again. Overrides are applied to the scored CSV and the summary, survive later runs (the previous file is kept as `review.csv.bak`), are validated (bad values are ignored with a warning), and are matched by URL + text, so an override for a post that is no longer in the input is kept and marked STALE but never applied. The summary reports how many posts were reviewed and how many labels changed.
