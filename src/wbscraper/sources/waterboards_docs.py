"""Public comment letters and related documents on waterboards.ca.gov (robots.txt obeyed).

Crawl: seed pages (yearly "Documents for Public Comment" indexes) -> follow links that mention comments/letters ->
collect PDFs. Each PDF is classified:
  letter   - an individual public comment letter (kept; split into verbatim, page-anchored passages)
  agency   - staff report, response to comments, comment summary, notice, agenda, minutes, order… (skipped: it is the
             agency's own text or a summary of many submissions, never counted as independent public posts)
  unknown  - cannot tell (skipped and counted so it is visible in `wbscraper status`/`report`)
Scanned PDFs without a text layer are counted as a coverage gap (OCR is a documented future enhancement).
"""
from __future__ import annotations

import hashlib
import io
import re
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from ..dates import to_iso
from ..http import FetchError
from ..robots import RobotsCache
from .base import AllProvidersFailed, RawItem
from .handler_base import ApiHandler

_STRONG_AGENCY = re.compile(
    r"(response[s]? to (public )?comment|summary of (public )?comment|comment summary|staff report|fact sheet|"
    r"board meeting agenda|\bagenda\b|minutes of|meeting minutes|advisory committee|notice of (public|availability|opportunity|filing|violation)|"
    r"final statement of reasons|environmental impact report|substitute environmental document|draft order|proposed order|"
    r"tentative (order|resolution)|stipulated order|administrative civil liability|cease and desist|settlement agreement|"
    r"waste discharge requirements|monitoring and reporting program|\border no\.|\bresolution no\.)", re.I)
# a person writing to the Board: a salutation or an explicit "comment letter" label (bare "re:" or "on behalf of"
# appear in orders, complaints and minutes too)
_SALUTATION = re.compile(
    r"(\bdear\s+(chair|chairman|chairwoman|chairperson|members?|board|ms\.?|mr\.?|mrs\.?|dr\.?|state|regional|"
    r"executive|honorable|water|director|commissioners?)|\bcomment letter\b|\bthank you for the opportunity\b|"
    r"\bwe appreciate the opportunity\b)", re.I)
_SIGNOFF_TAIL = re.compile(r"(sincerely|respectfully submitted|respectfully|very truly yours|yours truly)[,.]?\s", re.I)
# a document that *starts* with the agency's own name is agency-authored (a public comment letter starts with its
# writer's name and has the Board only in the address block)
_AGENCY_LETTERHEAD = re.compile(
    r"^\W*(california\s+)?(state\s+water\s+resources?\s+control\s+board|(\w+\s+){0,3}regional\s+water\s+quality\s+control\s+board|"
    r"california\s+environmental\s+protection\s+agency|cal\s?epa|division\s+of\s+(water\s+rights|drinking\s+water|"
    r"water\s+quality|financial\s+assistance))", re.I)
_RE_LINE = re.compile(r"^\s*re:\s", re.I | re.M)
_URL_LETTER = re.compile(r"(comment|letter|ltr|cl[_-]?\d)", re.I)
# file names that are obviously the agency's own documents: skipped before downloading (saves the crawl budget)
_AGENCY_NAME = re.compile(
    r"(response[_ -]?to[_ -]?comment|summary[_ -]?of[_ -]?comment|comment[_ -]?summary|staff[_ -]?report|agenda|minutes|"
    r"fact[_ -]?sheet|notice|transcript|presentation|slides|brochure|resolution|tentative|draft[_ -]?(policy|permit|plan|order)|"
    r"\border\b|\brtc\b|fsor|\beir\b)", re.I)
_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
                   r"\d{1,2},?\s+(19|20)\d{2}\b")
_SIGNOFF = re.compile(r"\n\s*(sincerely|respectfully submitted|respectfully|regards|best regards|very truly yours|"
                      r"yours truly)[,.]?\s*\n", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+")


def skip_by_name(path: str, label: str = "") -> bool:
    """True for PDFs whose file name says they are the agency's own document (and not a comment letter)."""
    name = path.rsplit("/", 1)[-1]
    if re.search(r"response[_ -]?to[_ -]?comment|summary[_ -]?of[_ -]?comment|comment[_ -]?summary", name, re.I):
        return True
    return bool(_AGENCY_NAME.search(name)) and not _URL_LETTER.search(name)


def classify_document(url: str, text: str, hint: str = "") -> str:
    """letter | agency | unknown. `hint` = "letter" when the page that linked the PDF is a comment-letter index."""
    top, head, tail = text[:800], text[:3000], text[-1500:]
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    if _AGENCY_LETTERHEAD.search(text[:120]):
        return "agency"
    salutation = bool(_SALUTATION.search(text[:1500]))
    signed = bool(_SIGNOFF_TAIL.search(tail))
    if _STRONG_AGENCY.search(top) and not salutation:
        return "agency"
    if salutation or (signed and (_URL_LETTER.search(filename) or _RE_LINE.search(top))):
        return "letter"
    if _STRONG_AGENCY.search(head):
        return "agency"
    if _URL_LETTER.search(filename) or hint == "letter":
        return "letter"
    return "unknown"


def letter_date(text: str, reader_meta=None) -> str | None:
    m = _DATE.search(text[:1500])
    if m:
        return to_iso(m.group(0).replace(",", ""), date_only=True)
    try:
        created = getattr(reader_meta, "creation_date", None)
        return to_iso(created, date_only=True) if created else None
    except Exception:
        return None


def passages(page_texts: list[str], max_chars: int = 1500) -> list[tuple[int, str]]:
    """(page_number, passage) pairs: sentences grouped up to max_chars, never crossing a page break."""
    out: list[tuple[int, str]] = []
    for pno, raw in enumerate(page_texts, start=1):
        text = re.sub(r"[ \t]+", " ", raw.replace("\r", ""))
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)           # single line breaks inside a paragraph
        buf = ""
        for para in re.split(r"\n\s*\n", text):
            for sent in _SENT.split(para.strip()):
                sent = sent.strip()
                if not sent:
                    continue
                if buf and len(buf) + len(sent) + 1 > max_chars:
                    out.append((pno, buf.strip()))
                    buf = ""
                buf += (" " if buf else "") + sent
            if buf and len(buf) > max_chars * 0.6:
                out.append((pno, buf.strip()))
                buf = ""
        if buf.strip():
            out.append((pno, buf.strip()))
    return out


def plan(lib, settings, since=None) -> list[tuple[str, dict, int, str]]:
    cfg = lib.forums.get("waterboards_docs", {})
    return [("wb_page", {"url": u, "depth": 0}, 500, "waterboards_docs") for u in cfg.get("seeds", [])]


class WaterBoardsDocsHandler(ApiHandler):
    TASK_TYPES = {"wb_page", "wb_pdf"}
    PROVIDER = "waterboards_docs"
    DOMAIN = "www.waterboards.ca.gov"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        c = self.cfg
        self.hosts = set(c.get("hosts", ["waterboards.ca.gov", "www.waterboards.ca.gov"]))
        self.follow_words = [w.lower() for w in c.get("follow_link_words", ["comment", "letter"])]
        self.max_depth = int(c.get("max_depth", 3))
        self.max_pages = int(c.get("max_pages", 1500))
        self.max_pdfs = int(c.get("max_pdfs", 1500))
        self.max_pdf_pages = int(c.get("max_pdf_pages", 120))
        self.max_pdf_bytes = int(c.get("max_pdf_bytes", 25_000_000))
        self.passage_chars = int(c.get("passage_chars", 1500))
        self.implicit = c.get("implicit_context", "State Water Board")
        self.robots = RobotsCache(self.http)

    def domain_for(self, task_type: str, payload: dict) -> str:
        return urlparse(payload["url"]).hostname or self.DOMAIN

    def _counter(self, name: str) -> int:
        return self.db.counters().get(name, 0)

    # -- HTML pages -----------------------------------------------------------------------------
    def _run_wb_page(self, p: dict, priority: int):
        url, depth = p["url"], int(p.get("depth", 0))
        host = urlparse(url).hostname or ""
        self.stats.current_task = f"page {url[-70:]}"
        if host not in self.hosts or not self.robots.allowed(url):
            self.db.add_url_seen(url, host, "wb"); self.db.bump_counter("wb_robots_or_host_blocked")
            return 0, None
        if self._counter("wb_pages_fetched") >= self.max_pages:      # budget spent: wait, don't pretend the page was read
            raise AllProvidersFailed([f"page budget reached (waterboards_docs.max_pages={self.max_pages}); raise it to continue"],
                                     retry_at=self.db.clock() + 30 * 86400, counted=False)
        res = self.http.get(url, domain=host)
        self.db.bump_counter("wb_pages_fetched")
        ctype = res.headers.get("content-type", "")
        if "pdf" in ctype.lower():
            self.db.add_task("wb_pdf", {"url": url}, 480, "waterboards_docs")
            return 0, None
        soup = BeautifulSoup(res.content, "html.parser")
        page_title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        index_page = "comment" in urlparse(url).path.lower() or "comment" in page_title
        n_links = 0
        for a in soup.find_all("a", href=True):
            link = urldefrag(urljoin(res.url, a["href"].strip()))[0]
            lp = urlparse(link)
            if lp.scheme not in ("http", "https") or (lp.hostname or "") not in self.hosts:
                continue
            label = (a.get_text(" ", strip=True) + " " + lp.path).lower()
            if lp.path.lower().endswith(".pdf"):
                if skip_by_name(lp.path, label):
                    if self.db.add_url_seen(link, lp.hostname, url):
                        self.db.set_url_status(link, "skipped")
                        self.db.bump_counter("wb_pdfs_skipped_by_name")
                    continue
                if self._counter("wb_pdfs_queued") < self.max_pdfs and self.db.add_url_seen(link, lp.hostname, url):
                    self.db.bump_counter("wb_pdfs_queued")
                    letter_ctx = index_page or any(w in label for w in ("comment", "letter"))
                    self.db.add_task("wb_pdf", {"url": link, **({"ctx": "letter"} if letter_ctx else {})}, 480,
                                     "waterboards_docs")
                    n_links += 1
            elif depth < self.max_depth and any(w in label for w in self.follow_words) \
                    and not re.search(r"\.(jpe?g|png|gif|zip|docx?|xlsx?|pptx?)$", lp.path, re.I):
                if self.db.add_url_seen(link, lp.hostname, url):
                    self.db.add_task("wb_page", {"url": link, "depth": depth + 1}, 490 - depth, "waterboards_docs")
                    n_links += 1
        self.stats.urls_discovered += n_links
        return 1, None

    # -- PDFs -----------------------------------------------------------------------------------
    def _run_wb_pdf(self, p: dict, priority: int):
        url = p["url"]
        host = urlparse(url).hostname or ""
        self.stats.current_task = f"pdf {url[-70:]}"
        if host not in self.hosts or not self.robots.allowed(url):
            self.db.bump_counter("wb_robots_or_host_blocked")
            return 0, None
        try:
            res = self.http.get(url, domain=host, max_bytes=self.max_pdf_bytes)
        except FetchError as err:
            if err.kind == "too_large":
                self.db.bump_counter("wb_pdf_too_large")
                return 0, None
            raise
        try:
            reader = PdfReader(io.BytesIO(res.content))
            if reader.is_encrypted:
                reader.decrypt("")
            n_pages = len(reader.pages)
            texts = []
            for page in reader.pages[: self.max_pdf_pages]:
                try:
                    texts.append(page.extract_text() or "")
                except Exception:
                    texts.append("")
            meta = reader.metadata
        except Exception as exc:            # corrupt/unsupported PDF: count it, never crash the run
            self.db.bump_counter("wb_pdf_unreadable")
            self.stats.last_error = f"unreadable PDF {url[-60:]}: {type(exc).__name__}"
            return 0, None
        total_chars = sum(len(t.strip()) for t in texts)
        if total_chars < 80 * max(1, min(n_pages, 3)):
            self.db.bump_counter("wb_scanned_pdfs")     # no text layer: needs OCR (documented gap)
            self.db.set_url_status(url, "scanned")
            return 1, None
        full = "\n\n".join(texts)
        kind = classify_document(url, full, p.get("ctx", ""))
        self.db.set_url_status(url, kind)
        if kind != "letter":
            self.db.bump_counter("wb_agency_docs" if kind == "agency" else "wb_unclassified_docs")
            return 1, None
        self.db.bump_counter("wb_letters")
        date = letter_date(full, meta)
        m = None
        for m in _SIGNOFF.finditer(full):
            pass
        if m and m.start() > len(full) * 0.5:          # drop the signature block / contact details after the sign-off
            cut = m.start()
            acc, trimmed = 0, []
            for t in texts:
                if acc + len(t) + 2 <= cut:
                    trimmed.append(t); acc += len(t) + 2
                else:
                    trimmed.append(t[: max(0, cut - acc)]); break
            texts = trimmed
        h = hashlib.sha1(url.encode()).hexdigest()[:12]
        raws = []
        for i, (pno, text) in enumerate(passages(texts, self.passage_chars)):
            raws.append(RawItem(uid=f"wb:{h}:{pno}:{i}", source="waterboards", kind="letter", url=f"{url}#page={pno}",
                                text=text, date=date, thread_uid=None, community=host,
                                score_hint=self.implicit))
        self.ingest(raws, "wb_pdf")
        return len(raws), None
