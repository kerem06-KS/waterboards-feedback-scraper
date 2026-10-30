import httpx
import pytest
import respx

from helpers import make_app, make_pdf, run_all
from wbscraper.sources.waterboards_docs import classify_document, letter_date, passages

HOST = "https://www.waterboards.ca.gov"
ROBOTS = "User-agent: *\nDisallow: /images/\nDisallow: /dev/\n"

LETTER = ("March 5, 2024\n\nDear Chair and Members of the Board:\n\n"
          "Thank you for the opportunity to comment on the proposed order. We write on behalf of a coalition of small "
          "dischargers. The SMARTS website is difficult to use: the portal times out when we upload the annual report "
          "and the login keeps failing. Our staff waited weeks for the help desk to respond.\n\n"
          "The policy itself is reasonable and we support the proposed schedule for implementation.\n\n"
          "Respectfully submitted,\n\nJane Q. Public\nExecutive Director\n(916) 555-0100\njane@example.org")
AGENCY = ("Response to Comments\n\nStaff report summarizing public comments received on the proposed order. "
          "Commenters said the SMARTS website is difficult to use and the login keeps failing.")


def test_classification():
    assert classify_document("https://x/a.pdf", LETTER) == "letter"
    assert classify_document("https://x/a.pdf", AGENCY) == "agency"
    assert classify_document("https://x/Final-Statement-of-Reasons.pdf", "Final Statement of Reasons\nbla") == "agency"
    assert classify_document("https://x/comment_letter_12.pdf", "text without markers") == "letter"
    assert classify_document("https://x/random.pdf", "some other document") == "unknown"


def test_dates_and_passages():
    assert letter_date(LETTER) == "2024-03-05"
    ps = passages(["First sentence. Second sentence.\n\nA new paragraph here.", "Page two text."], max_chars=40)
    assert [p for p, _ in ps][-1] == 2 and all(len(t) <= 80 for _, t in ps)
    assert "".join(t for _, t in ps).replace(" ", "").startswith("Firstsentence.Secondsentence.")


@respx.mock
def test_crawl_extracts_letter_passages_and_skips_agency_summaries(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("wb_page", {"url": f"{HOST}/public_notices/comments/", "depth": 0}, 500, "waterboards_docs")
    respx.get(f"{HOST}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(f"{HOST}/public_notices/comments/").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="""
        <html><body>
          <a href="/public_notices/comments/2024-order.html">Written Comments Received</a>
          <a href="/about/staff.html">About our staff</a>
          <a href="/dev/secret.pdf">dev</a>
        </body></html>"""))
    respx.get(f"{HOST}/public_notices/comments/2024-order.html").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="""
        <a href="/docs/comment_letter_smallcoalition.pdf">Small coalition letter</a>
        <a href="/docs/response_to_comments.pdf">Response to comments</a>
        <a href="/docs/scanned.pdf">Scanned letter</a>"""))
    respx.get(f"{HOST}/docs/comment_letter_smallcoalition.pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=make_pdf([LETTER])))
    respx.get(f"{HOST}/docs/response_to_comments.pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=make_pdf([AGENCY])))
    respx.get(f"{HOST}/docs/scanned.pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=make_pdf([" "])))
    run_all(app, clock)

    rows = list(app.db.conn.execute("SELECT * FROM items"))
    assert rows, "the letter's relevant passage should be stored"
    assert all(r["url"].startswith(f"{HOST}/docs/comment_letter_smallcoalition.pdf#page=") for r in rows)
    assert all(r["kind"] == "letter" and r["date"] == "2024-03-05" for r in rows)
    texts = " ".join(r["text"] for r in rows)
    assert "SMARTS website is difficult to use" in texts
    assert "jane@example.org" not in texts and "555-0100" not in texts and "Executive Director" not in texts
    c = app.db.counters()
    assert c["wb_letters"] == 1 and c["wb_agency_docs"] == 1 and c["wb_scanned_pdfs"] == 1
    assert c["wb_pages_fetched"] == 2                                   # /about/ not followed, /dev/ blocked by robots
    assert not [r for r in rows if "Response to Comments" in r["text"]]


@respx.mock
def test_robots_txt_is_obeyed_even_for_direct_requests(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("wb_pdf", {"url": f"{HOST}/dev/secret.pdf"}, 480, "waterboards_docs")
    respx.get(f"{HOST}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    pdf = respx.get(f"{HOST}/dev/secret.pdf").mock(return_value=httpx.Response(200, content=make_pdf([LETTER])))
    run_all(app, clock)
    assert pdf.call_count == 0 and app.db.counters()["wb_robots_or_host_blocked"] == 1


@respx.mock
def test_unreadable_pdf_does_not_crash_the_run(tmp_path):
    app, clock = make_app(tmp_path, {})
    app.db.add_task("wb_pdf", {"url": f"{HOST}/docs/broken.pdf"}, 480, "waterboards_docs")
    respx.get(f"{HOST}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{HOST}/docs/broken.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1.4 not really"))
    result = run_all(app, clock)
    assert result.stop_reason == "exhausted" and app.db.counters()["wb_pdf_unreadable"] == 1
