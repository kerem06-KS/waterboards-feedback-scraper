from pathlib import Path

import pytest

from conftest import REPO_CONFIG
from wbscraper.relevance import RelevanceScorer
from wbscraper.terms import TermLibrary


@pytest.fixture(scope="module")
def scorer():
    return RelevanceScorer(TermLibrary.load(REPO_CONFIG))


RELEVANT = [
    "SMARTS keeps timing out when I upload my annual report for the stormwater permit",
    "Anyone having trouble uploading EDF to GeoTracker? Rejected every time",
    "State Water Board website is down again, can't log in",
    "Does anyone know how to file a CIWQS spill report?",
    "CalWATRS PIN never arrived and the login is confusing",
    "California wastewater operator certification renewal portal is so slow, anyone else?",
    "The waterboards.ca.gov site search is useless, I can't find the form I need",
    "eWRIMS was clunky but CalWATRS is worse, the website keeps logging me out",
]

NOT_RELEVANT = [
    "GeoTracker app for my motorcycle GPS tracking",
    "Looking for SaaS founders to test GEO Tracker AI and give honest feedback",
    "GeoTracker for Houdini is out of beta! Now stable + Houdini 22 support",
    "Waterboarding is torture",
    "I love my smarts about water rights",
    "My bank website login is slow and confusing",
    "The Texas Water Development Board website is down",
    "The State Water Board adopted a new rule on PFAS limits",
    "SMARTS solar irradiance model spectral atmospheric transmittance",
    "street smarts and common sense are underrated",
]


@pytest.mark.parametrize("text", RELEVANT)
def test_relevant_feedback_is_kept(scorer, text):
    assert scorer.score(text).tier == "relevant", scorer.score(text)


@pytest.mark.parametrize("text", NOT_RELEVANT)
def test_look_alikes_and_policy_chatter_are_not_relevant(scorer, text):
    assert scorer.score(text).tier != "relevant", scorer.score(text)


def test_comment_in_relevant_thread_qualifies_without_naming_the_system(scorer):
    text = "the upload kept timing out for me too"
    assert scorer.score(text).tier == "irrelevant"
    assert scorer.score(text, thread_relevant=True).tier == "relevant"


def test_thread_context_alone_is_not_enough(scorer):
    for text in ("Thanks, that worked!", "same here", "I agree with you, my husband did the same"):
        assert scorer.score(text, thread_relevant=True).tier == "irrelevant", text


def test_generic_complaint_without_any_anchor_is_capped(scorer):
    s = scorer.score("this website is so slow and confusing, the login keeps failing and the upload is broken")
    assert s.total <= 1.0 and s.tier == "irrelevant"


def test_system_name_alone_is_kept_for_review_but_not_exported(scorer):
    s = scorer.score("CIWQS")
    assert s.tier == "irrelevant" and s.entity_hit        # stored (entity hit) so rule changes can resurrect it


def test_acronym_collision_needs_case_and_context(scorer):
    assert not scorer.score("smarts and stormwater permits").entity_hit
    assert scorer.score("SMARTS annual report stormwater is due").entity_hit
    assert not scorer.score("SMARTS is a great mutual fund").entity_hit


def test_regional_board_names_are_recognised(scorer):
    s = scorer.score("The Central Valley Water Board portal is broken again")
    assert s.entity_hit and s.tier == "relevant"
