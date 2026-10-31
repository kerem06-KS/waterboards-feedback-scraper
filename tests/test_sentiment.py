"""Experience-focused sentiment: only opinions about a Water Boards site or system move the score."""
import pytest

from analysis_helpers import cfg_for, lib, scorer
from wbscraper.analysis import tune
from wbscraper.analysis.sentiment import SentimentScorer, label_for, make_backend


def test_default_vader_misreads_topic_words_and_the_smarts_name(tmp_path):
    plain = scorer(tmp_path, plain=True)
    assert plain.score("There are spills and pollution and you should worry.").score < -0.3
    assert plain.score("SMARTS stormwater annual report NOI submitted.").score > 0.3     # 'smarts' = praise in VADER


def test_environmental_worry_and_a_neutral_recommendation_stay_neutral(tmp_path):
    s = scorer(tmp_path)
    r = s.score("You can use GeoTracker website to see if there are any documented spills or cleanup cases near your house. "
                "Kind of cheaper way to see if you have anything to worry about.")
    assert r.label == "neutral" and r.score == 0.0
    r = s.score("The contamination there is terrible and the polluters got away with it. Check GeoTracker for the case.")
    assert r.label == "neutral"


def test_smarts_system_name_is_not_praise(tmp_path):
    r = scorer(tmp_path).score("SMARTS stormwater annual report NOI submitted.")
    assert r.score == 0.0 and r.label == "neutral"


def test_a_complaint_about_the_system_is_negative(tmp_path):
    s = scorer(tmp_path)
    assert s.score("I tried to upload my annual report in SMARTS and the upload doesn't work.").label == "negative"
    assert s.score("The CalWATRS Initial Statement submittal process is mostly not working.").label == "negative"


def test_the_sentence_after_a_system_mention_counts_when_it_refers_back(tmp_path):
    r = scorer(tmp_path).score("I tried SMARTS for my stormwater permit last week. It's a nightmare and the login keeps timing out.")
    assert r.label == "negative" and len(r.focus) == 2


def test_praise_for_the_site_is_positive(tmp_path):
    r = scorer(tmp_path).score("Geotracker is an amazing site for contamination and cleanup cases, really easy to use.")
    assert r.label == "positive"


def test_sentences_about_other_things_are_ignored_even_when_strongly_worded(tmp_path):
    r = scorer(tmp_path).score("What a horrible disaster, I hate this awful company. GeoTracker lists the open cleanup cases.")
    assert r.label == "neutral" and r.focus == ["GeoTracker lists the open cleanup cases."]


def test_no_system_or_interface_word_means_neutral_by_rule(tmp_path):
    r = scorer(tmp_path).score("I love my new apartment and the neighbours are great.")
    assert r.label == "neutral" and r.no_focus and "no sentence about a site or system" in r.review_reasons


def test_courtesy_formulas_are_not_praise(tmp_path):
    r = scorer(tmp_path).score("Thank you for the opportunity to comment on the GeoTracker upload requirements.")
    assert r.score == 0.0


def test_quoted_lines_urls_and_placeholders_are_dropped(tmp_path):
    s = scorer(tmp_path)
    assert "terrible" not in s.clean("> the portal is terrible\nreal text https://x.org/terrible [REDACTED_EMAIL] `code`")
    r = s.score("> The SMARTS portal is a horrible nightmare.\nI just filed my NOI in SMARTS today.")
    assert r.label == "neutral"


def test_mixed_opinions_are_flagged_for_review(tmp_path):
    r = scorer(tmp_path).score("The GeoTracker map is excellent and easy to use. But the GeoTracker upload doesn't work and is a nightmare.")
    assert r.mixed and "mixed opinions" in r.review_reasons


def test_scores_near_a_threshold_are_flagged(tmp_path):
    s = scorer(tmp_path, thresholds=(-0.3, 0.55))
    r = s.score("The GeoTracker search is clunky.")
    assert r.near_threshold == (r.score != 0.0 and (abs(r.score + 0.3) < 0.10 or abs(r.score - 0.55) < 0.10))


def test_labels_follow_the_thresholds_inclusively():
    assert label_for(-0.3, -0.3, 0.55) == "negative"
    assert label_for(-0.2999, -0.3, 0.55) == "neutral"
    assert label_for(0.55, -0.3, 0.55) == "positive"
    assert label_for(0.5499, -0.3, 0.55) == "neutral"


def test_bad_thresholds_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        scorer(tmp_path, thresholds=(0.2, -0.2))


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError):
        make_backend("textblob")


def test_both_vader_packages_agree_closely_on_plain_text():
    a, b = make_backend("nltk-vader-sentiment"), make_backend("vaderSentiment")
    for t in ("This is great!", "This is not good at all.", "The form is okay, but the upload is terrible."):
        assert abs(a(t) - b(t)) < 0.1


def test_scores_stay_between_minus_one_and_plus_one(tmp_path):
    s = scorer(tmp_path)
    for t in ("GeoTracker " + "is a nightmare, broken, useless, terrible. " * 50, "SMARTS " + "amazing great best " * 50):
        assert -1.0 <= s.score(t).score <= 1.0


def test_ablating_a_group_changes_nothing_else(tmp_path):
    full = scorer(tmp_path)
    no_phr = scorer(tmp_path, groups_off=["phrases"])
    t = "The SMARTS upload doesn't work."
    assert full.score(t).score < no_phr.score(t).score


# -- tuning helpers -----------------------------------------------------------------------------------------
def test_metrics_and_confusion_matrix():
    gold = ["negative", "neutral", "neutral", "positive"]
    pred = ["negative", "neutral", "positive", "positive"]
    m = tune.metrics(gold, pred)
    assert m["accuracy"] == 0.75 and m["confusion"]["neutral"]["positive"] == 1
    assert m["per_class"]["negative"]["precision"] == 1.0


def test_default_thresholds_are_kept_when_they_sit_on_the_plateau():
    scores = [-0.8, -0.6, 0.0, 0.0, 0.0, 0.6, 0.8]
    gold = ["negative", "negative", "neutral", "neutral", "neutral", "positive", "positive"]
    t, info = tune.choose_thresholds(scores, gold)
    assert t == tune.DEFAULT and "default kept" in info["rule"]


def test_threshold_moves_off_the_default_only_when_the_data_demand_it():
    scores = [-0.1, -0.1, -0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.1]
    gold = ["neutral"] * 3 + ["neutral"] * 5 + ["neutral"] * 2
    t, _ = tune.choose_thresholds(scores, gold)
    assert t[0] < -0.1 and t[1] > 0.1


def test_wilson_interval_brackets_the_share():
    lo, hi = tune.wilson(3, 71)
    assert lo < 3 / 71 < hi and 0 <= lo and hi <= 1
    assert tune.wilson(0, 0) == (0.0, 0.0)


def test_bootstrap_ci_is_ordered_and_reproducible():
    gold = ["neutral"] * 8 + ["negative"] * 2
    pred = ["neutral"] * 7 + ["negative"] * 3
    a = tune.bootstrap_ci(gold, pred, tune.macro_f1, n=200)
    assert a[0] <= a[1] and a == tune.bootstrap_ci(gold, pred, tune.macro_f1, n=200)
