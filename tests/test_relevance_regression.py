"""Regression tests for the two-stage relevance filter.

`fixtures/false_positives.json` holds 20 real records that the earlier, looser rules exported as "relevant"
(relationship advice, banking, laptop passwords, phone carriers, car repair, music, a podcast, a moderator bot).
None of them concerns a California Water Boards website. They are scored as posts and as comments, both
normally and in the worst case where their thread had somehow been flagged relevant.
"""
import json
from pathlib import Path

import pytest

from conftest import REPO_CONFIG
from helpers import make_app
from wbscraper.queries import pullpush_tokens, web_queries
from wbscraper.relevance import RelevanceScorer, cap_tier
from wbscraper.rescore import rescore_all
from wbscraper.sources.base import RawItem
from wbscraper.terms import TermLibrary

FALSE_POSITIVES = json.loads((Path(__file__).parent / "fixtures" / "false_positives.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lib():
    return TermLibrary.load(REPO_CONFIG)


@pytest.fixture(scope="module")
def scorer(lib):
    return RelevanceScorer(lib)


def _id(case):
    return f"{case['category']}-{case['kind']}-{case['text'][:20]}"


# ----------------------------------------------------------------------------------------- the 20 false positives
def test_fixture_has_twenty_cases_covering_posts_and_comments():
    assert len(FALSE_POSITIVES) == 20
    assert {c["kind"] for c in FALSE_POSITIVES} == {"post", "comment"}
    assert {"relationship advice", "banking", "car repair", "music"} <= {c["category"] for c in FALSE_POSITIVES}


@pytest.mark.parametrize("case", FALSE_POSITIVES, ids=_id)
def test_false_positive_is_irrelevant_on_its_own(scorer, case):
    s = scorer.score(case["text"], thread_relevant=False)
    assert s.tier == "irrelevant" and not s.entity_hit, s.reasons


@pytest.mark.parametrize("case", FALSE_POSITIVES, ids=_id)
def test_false_positive_never_becomes_relevant_even_in_a_flagged_thread(scorer, case):
    """The old filter let thread context do the work. Now context only helps text that itself talks about a
    website, and these never do."""
    s = scorer.score(case["text"], thread_relevant=True)
    assert s.tier != "relevant", s.reasons


# ----------------------------------------------------------------------------------------- genuine feedback
RELEVANT_POSTS = [
    "SMARTS keeps timing out when I upload my annual report for the stormwater permit",
    "Anyone having trouble uploading EDF to GeoTracker? Rejected every time",
    "State Water Board website is down again, can't log in",
    "Does anyone know how to file a CIWQS spill report? The login page is confusing",
    "CalWATRS PIN never arrived and the login is confusing",
    "California wastewater operator certification renewal portal is so slow, anyone else?",
    "The waterboards.ca.gov site search is useless, I can't find the form I need",
    "eWRIMS was clunky but CalWATRS is worse, the website keeps logging me out",
    "I'm applying for the wastewater exam in California. Can I upload scans of my CEU certificates to the SWRCB "
    "portal or do I need the transcript? I'm confused about the proof part.",
    "Specifically go to the map in GeoTracker, open the layers under the dropdown menu and turn on the site cleanup layer",
    "The websites are so confusing, they take me all over the internet. I ended up on waterboards.ca.gov and this is so confusing",
    "The eSMR upload in CIWQS fails with a validation error every month, the help desk never answers",
]

REPLIES_IN_RELEVANT_THREADS = [
    "the upload kept timing out for me too",
    "Same here, the login page just reloads and the website is slow",
    "Scan them then upload them and do the application online. Way better than snail mail.",
    "Upload the pdf file you'll get once you pay the fee.",
]


@pytest.mark.parametrize("text", RELEVANT_POSTS)
def test_genuine_posts_are_relevant(scorer, text):
    s = scorer.score(text)
    assert s.tier == "relevant" and s.entity_hit, s.reasons


@pytest.mark.parametrize("text", RELEVANT_POSTS[:6])
def test_genuine_comments_are_relevant_without_any_thread_context(scorer, text):
    assert scorer.score(text, thread_relevant=False).tier == "relevant"


@pytest.mark.parametrize("text", REPLIES_IN_RELEVANT_THREADS)
def test_replies_in_relevant_threads_qualify_through_the_narrow_exception(scorer, text):
    assert scorer.score(text, thread_relevant=False).tier == "irrelevant"
    s = scorer.score(text, thread_relevant=True)
    assert s.tier == "relevant" and s.thread_exception, s.reasons


# ----------------------------------------------------------------------------------------- stage 1 and stage 2
@pytest.mark.parametrize("text", [
    "water", "the system is down", "I keep getting an error", "my account is locked", "I can't login",
    "the water system error locked my account and I can't login to the portal",
    "this website is so slow and confusing, the login keeps failing and the upload is broken",
    "my bank website login is slow and the account page shows an error",
    "The County water district portal is down and my login fails",
])
def test_generic_words_never_suffice(scorer, text):
    s = scorer.score(text)
    assert s.tier == "irrelevant" and s.total <= 1.0, s.reasons


def test_stage_one_alone_is_not_enough(scorer):
    for text in ("The State Water Board adopted a new rule on PFAS limits", "CIWQS", "Hiring: Water Resource Control Engineer at the SWRCB, apply by Friday"):
        s = scorer.score(text)
        assert s.entity_hit and s.tier != "relevant", s.reasons


def test_stage_two_alone_is_not_enough_even_in_a_long_thread(scorer):
    text = "The portal login is broken and the upload keeps timing out, super frustrating website"
    assert scorer.score(text, thread_relevant=False).tier == "irrelevant"


def test_organisation_mention_needs_a_strong_experience_signal(scorer):
    assert scorer.score("I found the form on the State Water Board website").tier == "borderline"
    assert scorer.score("The State Water Board website is confusing, I can't find the form").tier == "relevant"


def test_named_system_mentioned_but_not_used_is_only_borderline(scorer):
    s = scorer.score("GeoTracker lists the UST cleanup case T0600112233 with status open and a closure date pending")
    assert s.tier == "borderline"


def test_evidence_must_be_local_to_the_mention(scorer):
    """A Water Boards mention in the first paragraph cannot borrow 'website is confusing' from a later, unrelated one."""
    filler = "We spent the afternoon repainting the garden fence and talking about nothing in particular. " * 4
    text = ("The SWRCB posted a new fact sheet about drinking water.\n\n" + filler + "\n\n" + filler +
            "\n\nBy the way my insurance website is so confusing and the login keeps failing, upload is broken.")
    s = scorer.score(text)
    assert s.tier != "relevant", s.reasons
    near = "The SWRCB website is so confusing and the login keeps failing.\n\n" + filler + filler
    assert scorer.score(near).tier == "relevant"


def test_curly_apostrophes_and_action_phrases(scorer):
    assert scorer.score("State Water Board website: I can\u2019t log in and it\u2019s so confusing").tier == "relevant"
    # a bare modal verb is not a problem signal ("what can't be done with the soil")
    assert scorer.score("The covenant says what can't be done with the soil. The PDF is in the GeoTracker case file at "
                        "geotracker.waterboards.ca.gov").tier != "relevant"


def test_agency_announcements_are_not_feedback(scorer):
    for text in ("California's State Water Board launched a revamped BeachWatch dashboard, making it easier than ever to track "
                 "beach advisories! Learn more here",
                 "The California Division of Drinking Water is gearing up for the 2026 Electronic Annual Report with training "
                 "sessions you won't want to miss! Click to read more!"):
        assert scorer.score(text).tier != "relevant", text


def test_urls_are_not_usability_evidence_and_a_bare_link_is_not_a_connection(scorer):
    link = "Look at https://geotracker.waterboards.ca.gov/search?CMD=search&upload=login&error=1 neat map"
    s = scorer.score(link)
    assert s.tier == "irrelevant" and s.connection in ("none", "implicit")
    assert scorer.score("https://example.com/login/upload/error has the site search broken, so confusing").tier == "irrelevant"


def test_topic_phrases_need_a_california_or_water_board_qualifier(scorer):
    assert scorer.score("Operator certification renewal portal is slow, anyone else?").tier == "irrelevant"
    assert scorer.score("California operator certification renewal portal is slow, anyone else?").tier == "relevant"


def test_thread_context_without_interface_vocabulary_is_irrelevant(scorer):
    for text in ("Thanks, that worked!", "I agree, same experience here", "You should call the office on Monday"):
        assert scorer.score(text, thread_relevant=True).tier == "irrelevant"


def test_thread_exception_is_not_granted_without_a_flagged_thread(scorer):
    assert scorer.score("the upload kept timing out for me too").tier == "irrelevant"


def test_hint_and_repository_context_never_make_an_item_relevant(scorer):
    text = "The website map does not load and the page is blank, login fails"
    hint = "waterboards.ca.gov California Water Boards CAWaterBoardDataCenter/tool"
    s = scorer.score(text, hint=hint)
    assert s.tier == "borderline" and s.connection == "implicit"
    assert cap_tier(scorer.score("SMARTS stormwater upload keeps timing out", hint=hint), "borderline").tier == "borderline"


def test_negatives_demote_or_reject(scorer):
    assert scorer.score("Waterboarding is torture, the CIA website login is confusing").tier == "irrelevant"
    s = scorer.score("GeoTracker GPS app for my motorcycle, the login is broken and the upload fails")
    assert s.tier != "relevant"


# ----------------------------------------------------------------------------------------- ambiguous terms
@pytest.mark.parametrize("text", [
    "WDID? My boyfriend won't talk to me and the website login is confusing",
    "wdid about my account password, I can't log in to the website",
    "AITA/ WDID? my website upload failed and I'm confused",
    "I used to be on ESMR REVENGERS, my beat tape upload to the website keeps failing",
    "What is ESMR Treatment? Anyone else had the upload problem on the clinic portal?",
    "street smarts about the website login problem, anyone else?",
    "Clean Water Act Section 404 permitting website is confusing in California",
    "CEDEN data on the Thai clinic booking website is a problem, anyone else?",
    "The Texas Water Development Board website is down and the login is confusing",
])
def test_ambiguous_abbreviations_do_not_create_a_connection(scorer, text):
    assert scorer.score(text).tier != "relevant"


def test_wdid_counts_only_uppercase_with_stormwater_wording(scorer):
    assert scorer.score("Which WDID number do I enter in SMARTS? The NOI upload says invalid, anyone else?").tier == "relevant"
    assert not scorer.score("wdid? the stormwater people are being weird").entity_hit
    assert not scorer.score("WDID? relationship question, my boyfriend is distant").entity_hit


def test_esmr_needs_exact_case_and_water_wording(scorer):
    assert scorer.score("eSMR upload in CIWQS fails with an error every month").entity_hit
    assert not scorer.score("ESMR beat tape drops tomorrow").entity_hit
    assert not scorer.score("eSMR is great for streaming").entity_hit


# ----------------------------------------------------------------------------------------- query generation
AMBIGUOUS_GLOBAL_TOKENS = {"wdid", "esmr", "smarts", "ocis", "elap", "gama", "faast", "upward", "dww", "ddw", "dwq", "dfa", "ear"}


def test_global_reddit_search_never_sends_an_ambiguous_acronym(lib):
    tokens = {t.lower() for t in pullpush_tokens(lib)}
    assert not tokens & AMBIGUOUS_GLOBAL_TOKENS, tokens & AMBIGUOUS_GLOBAL_TOKENS
    assert "ciwqs" in tokens and "swrcb" in tokens


def test_no_search_engine_query_is_a_bare_ambiguous_acronym(lib):
    qs = {q.lower().strip('"') for _, q in web_queries(lib)}
    assert not qs & AMBIGUOUS_GLOBAL_TOKENS


# ----------------------------------------------------------------------------------------- pipeline + reclassification
def _raw(uid, text, kind="post", thread=None, community="Wastewater", hint="", cap=""):
    return RawItem(uid=uid, source="reddit", kind=kind, url=f"https://www.reddit.com/{uid}", text=text,
                   date="2024-05-01T10:00:00Z", thread_uid=thread or uid, community=community, score_hint=hint,
                   tier_cap=cap)


def test_unrelated_thread_is_never_flagged_so_its_comments_never_qualify(tmp_path):
    app, _ = make_app(tmp_path)
    p = app.pipeline
    r = p.ingest(_raw("reddit:t3_a", "WDID? my boyfriend lied and I can't log in to his account, website is confusing",
                      community="whatdoIdo"), "pullpush", "search")
    assert r.status == "below_threshold" and not app.db.thread_relevant("reddit:t3_a")
    for i in range(3):
        c = p.ingest(_raw(f"reddit:t1_{i}", "just reset your password and log in again, the website upload is easy", "comment",
                          "reddit:t3_a", community="whatdoIdo"), "pullpush", "search")
        assert c.status == "below_threshold"
    assert app.db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_relevant_thread_lets_only_interface_replies_through(tmp_path):
    app, _ = make_app(tmp_path)
    p = app.pipeline
    p.ingest(_raw("reddit:t3_s", "SMARTS stormwater annual report website is down, login confusing"), "pullpush", "s")
    assert app.db.thread_relevant("reddit:t3_s")
    ok = p.ingest(_raw("reddit:t1_ok", "the upload kept timing out for me too", "comment", "reddit:t3_s"), "pullpush", "s")
    chat = p.ingest(_raw("reddit:t1_chat", "Thanks, that worked! My sister says hi", "comment", "reddit:t3_s"), "pullpush", "s")
    assert ok.tier == "relevant" and chat.status == "below_threshold"


def _seed_old_database(app):
    """Rows exactly as the old filter stored them, plus completed and pending work."""
    db = app.db
    old = [
        ("reddit:t3_fp", "post", "whatdoIdo", "WDID My boyfriend's an extreme misogynist, the website is confusing", "relevant", 1),
        ("reddit:t1_fp1", "comment", "whatdoIdo", "Block him and reset your password, thanks", "relevant", 0),
        ("reddit:t1_fp2", "comment", "personalfinance", "if your name is on the account you can create an online login", "borderline", 0),
        ("reddit:t3_ok", "post", "Wastewater", "SMARTS keeps timing out when I upload my annual report for the stormwater permit",
         "borderline", 0),
        ("reddit:t1_ok1", "comment", "Wastewater", "the upload kept timing out for me too", "borderline", 0),
        ("reddit:t1_ok2", "comment", "Wastewater", "Thanks, my sister says hi", "borderline", 0),
    ]
    for uid, kind, comm, text, tier, own in old:
        thread = uid if kind == "post" else ("reddit:t3_fp" if "fp" in uid else "reddit:t3_ok")
        db.insert_item(uid=uid, source="reddit", kind=kind, url=f"https://www.reddit.com/{uid}", thread_uid=thread,
                       parent_uid=None, community=comm, text=text, date="2024-01-01T00:00:00Z", tier=tier,
                       own_relevant=own, score=5, entity_hit=0, reasons="[]", text_hash=None, flags=None)
    db.upsert_thread("reddit:t3_fp", "reddit", community="whatdoIdo", relevant=True)       # wrongly flagged
    db.upsert_thread("reddit:t3_ok", "reddit", community="Wastewater", relevant=False)
    db.set_meta("discovered_subs", json.dumps(["whatdoIdo", "Wastewater"]))
    db.add_task("reddit_search", {"kind": "posts", "q": "SMARTS", "subreddit": "whatdoIdo", "after": 0, "before": 1}, 5, "x")
    db.add_task("reddit_search", {"kind": "posts", "q": "SMARTS", "subreddit": "Wastewater", "after": 0, "before": 1}, 5, "x")
    db.add_task("reddit_search", {"kind": "posts", "q": "WDID", "after": 0, "before": 1}, 5, "x")
    db.add_task("reddit_thread", {"post_id": "fp", "after": 0, "before": 1, "page": 0}, 5, "x")
    done = db.add_task("reddit_search", {"kind": "posts", "q": "CIWQS", "after": 0, "before": 1}, 5, "x")
    row = db.conn.execute("SELECT id FROM tasks WHERE payload LIKE '%CIWQS%'").fetchone()
    db.complete_task(row["id"])
    db.commit()
    return done


def test_rescore_reclassifies_without_deleting_or_resetting_anything(tmp_path):
    app, _ = make_app(tmp_path)
    _seed_old_database(app)
    rows_before = app.db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    done_before = app.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]

    res = rescore_all(app.db, app.scorer, retired_queries=["WDID"])

    assert app.db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == rows_before        # raw rows kept
    assert app.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == done_before
    tiers = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert tiers["reddit:t3_fp"] == "irrelevant" and tiers["reddit:t1_fp1"] == "irrelevant"
    assert tiers["reddit:t1_fp2"] == "irrelevant"
    assert tiers["reddit:t3_ok"] == "relevant"
    assert tiers["reddit:t1_ok1"] == "relevant"                       # narrow exception, thread established by t3_ok
    assert tiers["reddit:t1_ok2"] == "irrelevant"
    assert not app.db.thread_relevant("reddit:t3_fp") and app.db.thread_relevant("reddit:t3_ok")
    assert res["after"].get("relevant") == 2 and res["before"].get("relevant") == 2

    statuses = {json.loads(r["payload"]).get("subreddit", json.loads(r["payload"]).get("q")): r["status"]
                for r in app.db.conn.execute("SELECT payload, status FROM tasks WHERE type='reddit_search'")}
    assert statuses["whatdoIdo"] == "cancelled" and statuses["Wastewater"] == "pending"
    assert statuses["WDID"] == "cancelled"
    assert statuses["CIWQS"] == "done"                                 # completed work is never reset
    assert app.db.conn.execute("SELECT status FROM tasks WHERE type='reddit_thread'").fetchone()["status"] == "cancelled"
    assert json.loads(app.db.get_meta("discovered_subs")) == ["Wastewater"]


def test_rescore_is_idempotent(tmp_path):
    app, _ = make_app(tmp_path)
    _seed_old_database(app)
    rescore_all(app.db, app.scorer, retired_queries=["WDID"])
    first = app.db.tier_counts()
    again = rescore_all(app.db, app.scorer, retired_queries=["WDID"])
    assert app.db.tier_counts() == first and again["tier_changes"] == 0


def test_export_after_rescore_has_only_relevant_rows_and_review_file_has_borderline(tmp_path):
    import csv

    from wbscraper.export import HEADER, REVIEW_HEADER, export_csv, export_review_csv

    app, _ = make_app(tmp_path)
    _seed_old_database(app)
    app.db.insert_item(uid="reddit:t3_b", source="reddit", kind="post", url="https://www.reddit.com/reddit:t3_b",
                       thread_uid="reddit:t3_b", parent_uid=None, community="Wastewater",
                       text="GeoTracker lists the UST cleanup case T0600112233 with status open and a closure date pending", date=None, tier="relevant",
                       own_relevant=1, score=5, entity_hit=1, reasons="[]", text_hash=None, flags=None)
    app.db.commit()
    rescore_all(app.db, app.scorer, retired_queries=["WDID"])
    out = tmp_path / "out" / "waterboards_feedback.csv"
    res = export_csv(app.db, out)
    rows = list(csv.reader(out.open(encoding="utf-8")))
    assert rows[0] == HEADER and res.rows == 2
    assert all("boyfriend" not in r[0] and "GeoTracker" not in r[0] for r in rows[1:])
    review = tmp_path / "out" / "borderline_review.csv"
    assert export_review_csv(app.db, review) == 1
    rrows = list(csv.reader(review.open(encoding="utf-8")))
    assert rrows[0] == REVIEW_HEADER and "GeoTracker" in rrows[1][0] and "stage1:system" in rrows[1][3]


# ------------------------------------------------------------------------------- appliance (water filter) thread
# A Reddit thread about refrigerator water filters was flagged relevant because one comment said "my water quality"
# (matching the portal name "My Water Quality", whose context "water quality" was satisfied by the phrase itself).
# Every other comment then qualified through the thread exception on "filter", "drop down" and similar words.
APPLIANCE = json.loads((Path(__file__).parent / "fixtures" / "appliance_thread.json").read_text(encoding="utf-8"))


def test_appliance_fixture_has_the_seed_and_its_followers():
    assert len(APPLIANCE) >= 30 and sum(c["seed"] for c in APPLIANCE) == 1


def test_my_water_quality_in_ordinary_english_is_not_a_portal_mention(scorer):
    seed = next(c for c in APPLIANCE if c["seed"])["text"]
    s = scorer.score(seed)
    assert s.tier == "irrelevant" and s.connection == "none" and not s.entity_hit
    assert not scorer.score("If my water quality is that bad I would have noticed it in the tap by now.").entity_hit


@pytest.mark.parametrize("case", [c for c in APPLIANCE if not c["seed"]], ids=lambda c: c["text"][:24])
def test_appliance_comments_are_never_relevant_even_when_their_thread_is_flagged(scorer, case):
    assert scorer.score(case["text"]).tier == "irrelevant"
    assert scorer.score(case["text"], thread_relevant=True).tier != "relevant"      # worst case: thread wrongly flagged


@pytest.mark.parametrize("text", [
    "The California My Water Quality site is confusing and I can't find my water system on the map.",
    "My Water Quality from the State Water Board has a clunky search page, the website is hard to use.",
])
def test_the_real_portal_is_still_recognised(scorer, text):
    s = scorer.score(text)
    assert s.connection == "system" and s.tier in ("relevant", "borderline")
    assert scorer.score(text).entity_hit


def test_ambiguous_interface_words_still_count_next_to_a_water_boards_system(scorer):
    text = ("Specifically go to the map in GeoTracker, open up the list of layers under the dropdown menu, go to "
            "filterable map coverages > site cleanup and turn on the superfund layer.")
    assert scorer.score(text).tier == "relevant"
    assert scorer.score("GeoTracker for the UST cleanup site: where is the filter button? I can't find it on the site.").tier != "irrelevant"


def test_ambiguous_interface_words_alone_do_not_carry_the_thread_exception(scorer):
    for text in ("The filter keeps failing and the button is stuck, so annoying.",
                 "Pressed the menu button and the screen stays frozen, it just fails every time.",
                 "Power outage again, the drop down shelf fails and I'm confused."):
        assert scorer.score(text, thread_relevant=True).tier != "relevant", text
    # unambiguous digital wording still lets a reply in a relevant thread through
    assert scorer.score("The upload kept failing for me too, the website is so confusing.",
                        thread_relevant=True).tier == "relevant"
    assert scorer.score("Same here, I can't log in on the portal either.", thread_relevant=True).tier == "relevant"


def test_no_weak_term_is_satisfied_by_its_own_text(lib):
    """The My Water Quality bug class: a weak term whose required context is contained in the term itself."""
    allowed = {("swrcb", "water boards"),                    # the plural is the collective California name
               ("stormwater_topic", "SWPPP"), ("stormwater_topic", "QSD"), ("stormwater_topic", "QSP")}   # topics: own context
    offenders = [(e.id, w.term) for e in lib.entities for w in e.weak
                 if w.context_regex is not None and w.context_regex.search(w.term) and (e.id, w.term) not in allowed]
    assert offenders == []


def test_rescore_unflags_the_appliance_thread_and_keeps_water_boards_feedback(tmp_path):
    app, _ = make_app(tmp_path)
    scorer = RelevanceScorer(app.lib)
    thread = "reddit:t3_appl"
    for i, c in enumerate(APPLIANCE):
        app.db.insert_item(uid=f"reddit:t1_a{i}", source="reddit", kind="comment", url=f"https://reddit.com/a{i}",
                           thread_uid=thread, parent_uid=None, community="Appliances", text=c["text"], date="2023-06-01T00:00:00Z",
                           tier="relevant", own_relevant=1 if c["seed"] else 0, score=5.0, entity_hit=1, reasons="[]",
                           text_hash=f"h{i}", flags="")
    good = "The SMARTS stormwater website is so confusing, the upload kept failing when I submitted my annual report."
    app.db.insert_item(uid="reddit:t1_good", source="reddit", kind="comment", url="https://reddit.com/good",
                       thread_uid="reddit:t3_good", parent_uid=None, community="stormwater", text=good,
                       date="2023-06-01T00:00:00Z", tier="relevant", own_relevant=1, score=5.0, entity_hit=1,
                       reasons="[]", text_hash="hg", flags="")
    app.db.commit()
    rescore_all(app.db, scorer, prune=False)
    tiers = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert app.db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == len(APPLIANCE) + 1       # nothing deleted
    assert tiers["reddit:t1_good"] == "relevant"
    assert not [u for u, t in tiers.items() if t == "relevant" and u != "reddit:t1_good"]
    assert not app.db.thread_relevant(thread)


# --------------------------------------------------------------- GeoTracker look-alikes found while reviewing survivors
def test_geotracker_context_needs_more_than_the_ordinary_words_lust_and_cleanup(scorer):
    partner = ('"IF" she is willing to give you a chance... Give her access to all of your passwords, be honest to any '
               'questions she asks, and enable any geotracker on your phones or vehicles. But you will need to figure '
               'out why you cheated out of "pure lust" and why you wouldn\'t act on the same feelings again.')
    code = ('Refactor GeoTracker architecture and Thymeleaf dashboard. Add DB-backed authentication, login and upload '
            'handling; error handling for failed uploads; UI cleanup. Prompt used: chatgpt')
    assert scorer.score(partner).tier == "irrelevant" and scorer.score(code).tier == "irrelevant"
    assert scorer.score(partner, thread_relevant=True).tier != "relevant"


@pytest.mark.parametrize("text", [
    "Our LUST site is on GeoTracker and the EDF upload keeps failing, the website is so confusing.",
    "GeoTracker site cleanup case: the portal upload was rejected and I can't figure out why, very confusing.",
])
def test_geotracker_with_real_site_context_is_still_relevant(scorer, text):
    assert scorer.score(text).tier == "relevant"


def test_rescore_cli_keep_pending_leaves_every_pending_task_alone(tmp_path, monkeypatch):
    from wbscraper import cli
    app, _ = make_app(tmp_path)
    app.db.add_task("reddit_thread", {"post_id": "zzz", "after": 1, "before": 2}, 700, "reddit_thread")   # thread not relevant
    app.db.commit()
    before = app.db.task_counts()
    res = rescore_all(app.db, RelevanceScorer(app.lib), prune=False)
    assert "pruned" not in res and app.db.task_counts() == before
    pruned = rescore_all(app.db, RelevanceScorer(app.lib), prune=True)
    assert pruned["pruned"]["thread_reads"] == 1                       # the default still cleans up
    args = cli.build_parser().parse_args(["rescore", "--keep-pending"]) if hasattr(cli, "build_parser") else None
    assert args is None or args.keep_pending is True
