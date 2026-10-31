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
| `data/output/waterboards_feedback_scored.csv` | `text,date,source_url` (identical to the input) + `sentiment_score`, `sentiment_label`, `task`, `pain_point`. `task` is filled for every post, `pain_point` only for negative posts. |
| `data/output/sentiment_summary.md` | Shares by label, source and system; tasks; pain points; the negative posts. |
| `data/analysis/review.csv` | One row per post for manual checking (below). Opens in Excel / Google Sheets. |

## What "sentiment" means here

A post is **positive or negative only when it gives an opinion about a Water Boards website or online system itself**
("GeoTracker is an amazing site", "the CalWATRS upload doesn't work", "the websites are so confusing").
Worry about contamination, comments on regulations and neutral recommendations ("check GeoTracker near you") are **neutral**.
Plain VADER does not make that distinction: it scores `spills`, `worry` or `violation` as negative feelings and `smarts` (the SMARTS system) as praise.

Percentages in the summary are **shares of the posts collected**. The posts are self-selected, found by searching for specific systems, and mostly from Reddit and about GeoTracker. They are not a survey and say nothing about overall user satisfaction.

## How the score is made

1. **Clean** the text: drop quoted lines (`> ...`, someone else's words), URLs, markdown link targets, code and `[REDACTED_*]` placeholders.
2. **Pick the sentences that are about a site or system**: a named system (the same matcher the relevance filter uses), an interface word (website, portal, map, upload, login, form...), or the Water Boards plus a service word (called, applied, waiting...). The sentence right after one counts when it starts "It...", "This...", "The site...". Everything else is ignored. No such sentence means a score of 0.
3. **Score each with VADER** after the domain adjustments in `config/analysis/lexicon.yaml`: topic and legal words set to 0, usability complaints and praise added ("nightmare", "doesn't work", "can't find", "easy to use"), courtesy formulas removed ("thank you for the opportunity to comment").
4. **Score = mean of the non-zero sentence scores.** A sentence stating no opinion does not dilute one that does. Label by two cutoffs.

Package: **`nltk-vader-sentiment` 1.0.0** (a maintained re-implementation of VADER that fixes about 18 upstream bugs, such as the `but` handling, curly-apostrophe negation, and "no" at the end of a sentence). Against the original `vaderSentiment` 3.3.2 it gave the same results on this project's data and the same correlation with the human ratings of the original VADER paper (below). `vaderSentiment` stays installed for the comparison.

Every rule is written down before measuring and none was fitted to the labelled posts. One rule changed after the first run: step 4 originally averaged *all* chosen sentences. The change made no measurable difference (relevant macro-F1 0.55 → 0.53, tuning 0.41 → 0.39); it was kept because it fixes a case where a neutral sentence dilutes a clear complaint. The first run's report is kept in `data/analysis/eval_report_run1_mean_of_all_focus.md`.

## Measured accuracy (2026-10-31)

Hand labels (mine, from the written definition above) for two sets:

* **Evaluation set: the 71 relevant posts.** Never used for tuning. 10 negative, 56 neutral, 5 positive; 63 labelled "clear".
* **Tuning set: 449 other posts** (a seeded random sample of the borderline posts, plus the remaining non-document borderline posts). 12 negative, 420 neutral, 17 positive.

Only the two cutoffs were tuned, on a 0.05 grid, on the tuning set only. Each is the middle of the range whose cross-validated macro-F1 is within 0.01 of the best. Result: **negative ≤ −0.3, positive ≥ 0.55**.

| Method (nltk-vader-sentiment) | Relevant: accuracy | Relevant: macro-F1 (95% CI) | Relevant: negative precision / recall |
|---|---|---|---|
| Always "neutral" | 79% | 0.29 | 0% / 0% |
| Default VADER, whole text, ±0.05 | 28% | 0.26 (0.16–0.37) | 19% / 30% |
| This scorer, ±0.05 | 62% | 0.48 (0.35–0.61) | 57% / 40% |
| **This scorer, locked cutoffs** | **82%** | **0.53 (0.35–0.71)** | **100% / 30%** |

On the 71 relevant posts the scorer's confusion matrix is: of 10 negatives it finds 3 and calls 7 neutral; of 56 neutrals it gets 54; of 5 positives it finds 1.

**How to read this honestly**

* Default VADER is worse than guessing "neutral" for every post here (28% vs 79% accuracy). The focus-and-lexicon changes are what make it usable.
* The scorer is **cautious**: when it says negative it is right (3 of 3), but it misses most negatives (recall 30%). The summary's "share negative" is therefore a **floor**; my hand labels give 10/71 = 14%, the scorer 3/71 = 4%.
* Typical misses: long posts where a few positive sentences ("the staff are helpful") outweigh a long list of defects (the two CalWATRS blog posts); complaints phrased without any strongly negative word ("I'm a little confused about the proof part"); help-seeking posts.
* The numbers are noisy: 10 negatives and 5 positives. The confidence intervals above are wide, and the tuning set has only 12 negatives (its cross-validated macro-F1 is 0.37). The cutoffs are the best supported by little data, not precise.
* All labels were written by one person (me). Eight relevant posts and 36 tuning posts are marked "unclear" because reasonable readers could differ; that is what the review file is for.
* Because the aggregation change was made after the first run had shown the relevant-set results, that set is not a perfectly untouched check.

General-text check (correlation with the human ratings of Hutto & Gilbert 2014): the domain word list leaves behaviour on ordinary text essentially unchanged (tweets 0.872 / 0.875 / 0.862 for VADER 3.3.2 / nltk-vader-sentiment / with the domain list; movie reviews 0.427 / 0.427 / 0.428; Amazon 0.590 / 0.589 / 0.608; NYT editorials 0.503 / 0.504 / 0.486).

Re-run any time: `wbscraper analyze-eval --icwsm data/analysis/icwsm`. It never changes `config/analysis/settings.yaml`; cutoffs are edited by hand.

## Tasks and pain points (Claude API)

`wbscraper analyze --tag` sends each post (never usernames: the CSV has none) to `claude-opus-5-5` with a fixed list of choices (`config/analysis/tags.yaml`) and a JSON schema, so the answer is always one of the allowed values. Each call returns:

* **task**, for every post: what the person came to the site to do (or is recommending others do), plus a short free-text `task_detail`;
* **pain point**: why it went badly, from five categories (can't find information; outdated or conflicting pages; hard-to-read PDFs and forms; online systems like SMARTS; reaching staff) plus "Other / not a website problem" and "None". The scored CSV shows it **only for negative posts**.

"Outdated or conflicting pages" is defined as stale content, broken or moved links, old dates, and instructions that contradict each other across pages. Edit the definitions in `tags.yaml` if the Guidesheet's Issues column says otherwise, then raise `tagging.prompt_version` in `settings.yaml`.

* **Opt-in and cached.** Plain `analyze` never spends money. Results are cached in `data/analysis/tag_cache.json` by post text + prompt version + model, so re-runs are free and the answers stay fixed (this model has no temperature setting). `--retag` refreshes them. Refusals and API errors are not cached and are retried.
* **Cost:** about $0.30 of input for the 71 posts at the configured price plus a few cents of output; each run prints the actual token use. If no key is set, tagging is skipped with a message and everything else still runs.
* Refusal fallback (`fallbacks: default`) is on; set `tagging.fallbacks: off` to disable.

## Manual review

Open `data/analysis/review.csv`. Rows with something worth a look come first, with the reason in `review_reasons`: score within 0.10 of a cutoff, mixed opinions, no sentence about a site/system, hand label differs from the scorer (the 71 relevant posts), low tag confidence, task "Other"/"Unclassified", or a problem the tagger sees in a post not labelled negative. The `focus_sentences` column shows exactly which sentences produced the score.

Fill any of `label_override` (negative / neutral / positive), `task_override`, `pain_point_override` (exact names from `tags.yaml`, case-insensitive), `note`, then run `wbscraper analyze` again. Overrides are applied to the scored CSV and the summary, survive later runs (the previous file is kept as `review.csv.bak`), are validated (bad values are ignored with a warning), and are matched by URL + text, so an override for a post that is no longer in the input is kept and marked STALE but never applied. The summary reports how many posts were reviewed and how many labels changed.
