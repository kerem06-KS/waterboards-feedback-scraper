"""Why a run found few "relevant" items, and the fixes: item-type aware gates, source caps, letter classification.

`fixtures/yield_cases.json` holds 40 real records from a collection database, labelled by hand: 8 genuine
usability feedback items that must stay relevant, and 31 that the earlier gates exported but are not public feedback
(passing mentions, agency pages and bulletins, vendor marketing, site checkers, code-change titles).
"""
import json
from pathlib import Path

import httpx
import pytest
import respx

from conftest import REPO_CONFIG
from helpers import make_app, make_pdf, run_all
from wbscraper.docs_recovery import requeue_skipped_pages
from wbscraper.relevance import RelevanceScorer, cap_tier
from wbscraper.rescore import effective_kind, letter_doc_caps, rescore_all, tier_cap
from wbscraper.sources.waterboards_docs import classify_document, skip_by_name
from wbscraper.terms import TermLibrary
from wbscraper.webclass import cap_for, domain_class

CASES = json.loads((Path(__file__).parent / "fixtures" / "yield_cases.json").read_text(encoding="utf-8"))
GENUINE = [c for c in CASES if c["label"] == "genuine"]
JUNK = [c for c in CASES if c["label"] == "junk"]
HOST = "https://www.waterboards.ca.gov"


@pytest.fixture(scope="module")
def lib():
    return TermLibrary.load(REPO_CONFIG)


@pytest.fixture(scope="module")
def scorer(lib):
    return RelevanceScorer(lib)


def classify(scorer, case, thread_relevant=None):
    row = {k: case[k] for k in ("uid", "source", "kind", "community", "url")}
    flag = case["thread_relevant"] if thread_relevant is None else thread_relevant
    s = scorer.score(case["text"], thread_relevant=flag, kind=effective_kind(row))
    return cap_tier(s, tier_cap(row)).tier


def test_fixture_is_labelled():
    assert len(GENUINE) == 8 and len(JUNK) == 31


@pytest.mark.parametrize("case", GENUINE, ids=lambda c: c["text"][:28])
def test_genuine_feedback_stays_relevant(scorer, case):
    assert classify(scorer, case) == "relevant"


@pytest.mark.parametrize("case", JUNK, ids=lambda c: c["text"][:28])
def test_what_is_not_public_feedback_is_never_relevant(scorer, case):
    assert classify(scorer, case) != "relevant"
    assert classify(scorer, case, thread_relevant=True) != "relevant"          # even inside a flagged thread


# --------------------------------------------------------------------------------------------- scoring rules
@pytest.mark.parametrize("text", [
    "Groundwater moves extremely slow. You can look the site up on the state water board GeoTracker website.",
    "I would reach out to the regional water board. It could be a broken pipe in the complex. They list a contact on their website.",
    "California has many regulatory agencies that are hard to navigate, so a job at the State Water Board is a good start.",
    "Check the GeoTracker website for your site, then confirm the case number with the State Water Board. Helpful tool.",
])
def test_a_passing_mention_with_a_stray_experience_word_is_not_relevant(scorer, text):
    assert scorer.score(text).tier != "relevant"


@pytest.mark.parametrize("text", [
    "The CIWQS website is down again and I can't submit my eSMR.",
    "Does anyone know how to file a CIWQS spill report online?",
    "GeoTracker's upload page keeps timing out and the EDF gets rejected, so frustrating.",
    "Go to the map in GeoTracker, open the layers list, then turn on the site cleanup layer.",
    "The State Water Board website is so confusing, I can't find where to renew online.",
])
def test_real_usability_reports_questions_and_walkthroughs_are_relevant(scorer, text):
    assert scorer.score(text).tier == "relevant"


def test_a_single_step_pointer_to_the_site_is_only_borderline(scorer):
    s = scorer.score("Go to the GeoTracker website and you can find every UST cleanup report for the site.")
    assert s.tier == "borderline"


def test_authored_documents_need_a_real_complaint_not_praise_or_a_question(scorer):
    praise = "Our CalWATRS annual report filing service is easy. File your water use report on the portal in minutes."
    question = "How do I file my CalWATRS annual report on the portal?"
    complaint = "CalWATRS is frustrating: the portal timed out twice while we tried to file our annual report."
    assert scorer.score(praise, kind="article").tier != "relevant"
    assert scorer.score(question, kind="article").tier != "relevant"
    assert scorer.score(complaint, kind="article").tier == "relevant"
    assert scorer.score(question).tier == "relevant"                               # a person asking is still relevant


def test_vendor_marketing_and_commit_titles_are_rejected(scorer):
    promo = "CIWQS eSMR filing made simple. Request a demo today: our cloud-based solution fixes the confusing portal."
    commit = "feat(data): add CEDEN portal source, fixes broken upload error handling"
    assert scorer.score(promo, kind="article").tier == "irrelevant"
    assert scorer.score(commit).tier == "irrelevant"


# ---------------------------------------------------------------------------------------------- source classes
@pytest.mark.parametrize("host,expected", [
    ("www.waterboards.ca.gov", "official"), ("content.govdelivery.com", "official"), ("data.ca.gov", "official"),
    ("www.epa.gov", "official"), ("deq.state.ut.us", "official"), ("mlml.sjsu.edu", "official"),
    ("builtwith.com", "reference"), ("www.scamadviser.com", "reference"), ("downornot.net", "reference"),
    ("www.ibm.com", "reference"), ("allwaterrights.com", ""), ("www.reddit.com", ""), ("example.us", ""),
])
def test_domain_classes(host, expected):
    assert domain_class(host) == expected


def test_domain_caps():
    assert cap_for("www.waterboards.ca.gov") == "borderline"
    assert cap_for("builtwith.com") == "irrelevant"
    assert cap_for("allwaterrights.com") == ""
    assert cap_for("anything.example", official_extra=("anything.example",)) == "borderline"


# ----------------------------------------------------------------------------------------------- comment letters
PUBLIC_LETTER = ("Central Valley Dischargers Coalition\nMarch 5, 2024\n\nDear Chair Esquivel and Members of the Board:\n\n"
                 "Thank you for the opportunity to comment. The CIWQS portal is difficult to use.\n\nSincerely,\nJane Public")


@pytest.mark.parametrize("url,text,expected", [
    ("https://x/a.pdf", PUBLIC_LETTER, "letter"),
    ("https://x/a.pdf", "State Water Resources Control Board\nAugust 2026\nJeffrey Tsai, US EPA Region 9\nDear Mr. Tsai: Enclosed is the annual report.", "agency"),
    ("https://x/a.pdf", "California Environmental Protection Agency\nState Water Resources Control Board\nPlease include COMMENT LETTER in the subject line.", "agency"),
    ("https://x/a.pdf", "Advanced Geo, Inc.\nSTATE WATER RESOURCES CONTROL BOARD\nSTIPULATED ORDER\nRe: settlement\nRespectfully, on behalf of the parties", "agency"),
    ("https://x/a.pdf", "Drinking Water Operator Certification Program Advisory Committee\nMeeting Minutes - May 13, 2026", "agency"),
    ("https://x/a.pdf", "Response to Comments\nStaff report summarizing comments.", "agency"),
    ("https://x/random.pdf", "some other document", "unknown"),
    ("https://x/comment_letter_12.pdf", "text without markers", "letter"),
])
def test_classification(url, text, expected):
    assert classify_document(url, text) == expected


def test_link_context_rescues_an_unclear_document_only():
    assert classify_document("https://x/attachment3.pdf", "plain text with no markers", hint="letter") == "letter"
    assert classify_document("https://x/attachment3.pdf", "Staff Report\nbla", hint="letter") == "agency"


@pytest.mark.parametrize("path,skipped", [
    ("/docs/response_to_comments.pdf", True), ("/docs/Staff_Report_2024.pdf", True), ("/docs/agenda_item_5.pdf", True),
    ("/docs/final-order-2019.pdf", True), ("/docs/comment_letter_smith.pdf", False), ("/docs/ltr_cvcwa.pdf", False),
    ("/docs/order_comment_letter.pdf", False), ("/docs/attachment3.pdf", False),
])
def test_agency_documents_are_skipped_by_name_before_download(path, skipped):
    assert skip_by_name(path) is skipped


@respx.mock
def test_crawl_budget_defers_pages_instead_of_marking_them_done(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.handlers[0].max_pages = 1 if hasattr(app.handlers[0], "max_pages") else None
    handler = next(h for h in app.handlers if "wb_page" in h.TASK_TYPES)
    handler.max_pages = 1
    for i in range(3):
        app.db.add_task("wb_page", {"url": f"{HOST}/public_notices/comments/p{i}.html", "depth": 1}, 490, "waterboards_docs")
    respx.get(f"{HOST}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /dev/\n"))
    respx.get(url__regex=rf"{HOST}/public_notices/.*").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html><body>no links</body></html>"))
    run_all(app, clock, min_minutes=0, max_minutes=2)
    counts = app.db.task_counts()
    assert counts.get("done") == 1 and counts.get("pending") == 2          # two pages wait for a bigger budget
    assert "budget" in app.db.conn.execute("SELECT last_error FROM tasks WHERE status='pending'").fetchone()[0]
    assert app.db.counters()["wb_pages_fetched"] == 1


def test_requeue_adds_second_pass_tasks_for_pages_that_were_never_fetched(tmp_path):
    app, _ = make_app(tmp_path, {})
    for i in range(5):
        app.db.add_task("wb_page", {"url": f"{HOST}/p{i}", "depth": 1}, 490, "waterboards_docs")
        app.db.conn.execute("UPDATE tasks SET status='done', updated_at=? WHERE payload LIKE ?", (100 + i, f'%p{i}%'))
    app.db.bump_counter("wb_pages_fetched", 3)                               # 5 done but only 3 fetched: 2 were skipped
    app.db.commit()
    assert requeue_skipped_pages(app.db, dry_run=True)["requeued"] == 2
    assert app.db.task_counts() == {"done": 5}
    res = requeue_skipped_pages(app.db)
    assert res["possibly_skipped"] == 2 and res["requeued"] == 2
    assert app.db.task_counts() == {"done": 5, "pending": 2}                 # completed tasks untouched
    assert requeue_skipped_pages(app.db)["requeued"] == 0                     # idempotent
    urls = {__import__("json").loads(r[0])["url"] for r in app.db.conn.execute("SELECT payload FROM tasks WHERE status='pending'")}
    assert urls == {f"{HOST}/p3", f"{HOST}/p4"}                               # the two completed last


def test_rescore_applies_item_type_rules_to_stored_rows_without_deleting_any(tmp_path):
    app, _ = make_app(tmp_path)
    scorer = RelevanceScorer(app.lib)
    for i, c in enumerate(CASES):
        app.db.insert_item(uid=f"x:{i}", source=c["source"], kind=c["kind"], url=c["url"], thread_uid=f"x:t{i}",
                           parent_uid=None, community=c["community"], text=c["text"], date="2024-01-01T00:00:00Z",
                           tier="relevant", own_relevant=1, score=5.0, entity_hit=1, reasons="[]", text_hash=f"h{i}", flags="")
    app.db.commit()
    rescore_all(app.db, scorer, prune=False)
    tiers = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert len(tiers) == len(CASES)
    # genuine replies were stored in a flagged thread; here every row is its own thread, so judge them on their own text
    own = {f"x:{i}" for i, c in enumerate(CASES) if c["label"] == "genuine" and not c["thread_relevant"]}
    assert all(tiers[u] == "relevant" for u in own)
    assert not [u for i, c in enumerate(CASES) if c["label"] == "junk" and (u := f"x:{i}") and tiers[u] == "relevant"]


def test_stored_letter_passages_are_reclassified_by_their_document(tmp_path):
    app, _ = make_app(tmp_path)

    def add(h, i, text, url):
        app.db.insert_item(uid=f"wb:{h}:{i + 1}:{i}", source="waterboards", kind="letter", url=f"{url}#page={i + 1}", thread_uid=None,
                           parent_uid=None, community="www.waterboards.ca.gov", text=text, date=None, tier="relevant",
                           own_relevant=1, score=5.0, entity_hit=1, reasons="[]", text_hash=f"{h}{i}", flags="")

    add("aaaa", 0, PUBLIC_LETTER, f"{HOST}/a.pdf")
    add("bbbb", 0, "State Water Resources Control Board\nAugust 2026\nDear Mr. Tsai: annual report enclosed.", f"{HOST}/b.pdf")
    add("bbbb", 1, "The CIWQS portal errors out when users try to submit; the website is confusing.", f"{HOST}/b.pdf")
    app.db.commit()
    assert letter_doc_caps(app.db) == {"aaaa": "", "bbbb": "irrelevant"}
    rescore_all(app.db, RelevanceScorer(app.lib), prune=False)
    tiers = {r["uid"]: r["tier"] for r in app.db.conn.execute("SELECT uid, tier FROM items")}
    assert tiers["wb:aaaa:1:0"] == "relevant" and tiers["wb:bbbb:2:1"] == "irrelevant"


@respx.mock
def test_comment_index_pages_hint_letters_and_agency_files_are_not_downloaded(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("wb_page", {"url": f"{HOST}/public_notices/comments/2024.html", "depth": 0}, 500, "waterboards_docs")
    respx.get(f"{HOST}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /dev/\n"))
    respx.get(f"{HOST}/public_notices/comments/2024.html").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="""<html><head><title>Comments Received</title></head><body>
        <a href="/docs/attachment3.pdf">Attachment 3</a><a href="/docs/staff_report.pdf">Staff report</a></body></html>"""))
    plain = ("Valley Water Users\nWe write about the SMARTS website. The portal is confusing and keeps timing out "
             "when we upload our annual report.\n\nRespectfully submitted,\nA. Person\n")
    route = respx.get(f"{HOST}/docs/attachment3.pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=make_pdf([plain])))
    skipped = respx.get(f"{HOST}/docs/staff_report.pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=make_pdf(["Staff Report"])))
    run_all(app, clock)
    assert route.called and not skipped.called
    c = app.db.counters()
    assert c["wb_letters"] == 1 and c["wb_pdfs_skipped_by_name"] == 1
