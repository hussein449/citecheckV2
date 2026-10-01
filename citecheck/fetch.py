"""Download the text of a cited source so its content can be compared.

Handles HTML landing pages and PDFs, prefers an open-access mirror when the
publisher page is a paywall stub, and always leaves *something* to match
against — falling back to the abstract from Crossref/OpenAlex.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, asdict

import fitz
import requests
from bs4 import BeautifulSoup

from .resolve import HEADERS, TIMEOUT, ResolvedSource

MAX_BYTES = 12 * 1024 * 1024

_PAYWALL_HINTS = (
    "access through your institution", "purchase pdf", "get access",
    "subscribe to view", "sign in to read", "buy article", "rent this article",
    "you do not have access", "institutional login", "please enable javascript",
    "checking your browser", "verify you are human", "captcha",
)

# What a site says when it has decided the visitor is a program. Distinct from
# a paywall: a paywall shows a person the abstract and a price, this shows
# nobody anything.
_BOT_HINTS = (
    "checking your browser", "verify you are human", "verifying you are human",
    "are you a robot", "captcha", "just a moment", "unusual traffic",
    "automated access", "access denied", "request blocked", "enable javascript and cookies",
    "pardon our interruption", "bot detection", "security check",
    "not a bot", "proof-of-work", "anubis",
)
# 401/403/429 refuse the request outright. 202 and 503 are what challenge pages
# are served under (IEEE Xplore answers a script with an empty 202).
_REFUSED = {401, 403, 429, 451}
_CHALLENGE = {202, 503}

_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form",
               "noscript", "svg", "button")


@dataclass
class FetchedContent:
    url: str = ""
    final_url: str = ""
    kind: str = ""             # "html" | "pdf" | "abstract" | "none"
    title: str = ""
    text: str = ""
    status: int = 0
    ok: bool = False
    paywalled: bool = False
    # The site refused automated access: nothing was read, and not because
    # there was nothing there.
    blocked: bool = False
    # Why the address cannot be loaded at all ("HTTP 404", "no such host").
    dead: str = ""
    has_abstract: bool = False
    pdf_bytes: bytes | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("pdf_bytes", None)
        data["text_chars"] = len(self.text)
        data["text"] = self.text[:1500]
        return data


def fetch_source(src: ResolvedSource) -> FetchedContent:
    """Fetch the best available representation of the cited work."""
    candidates: list[str] = []
    for url in (src.oa_url, src.url, src.landing_url):
        if url and url not in candidates:
            candidates.append(url)

    best: FetchedContent | None = None
    blocked: list[FetchedContent] = []
    for url in candidates:
        got = _fetch_one(url)
        if got.ok and len(got.text) > 600 and not got.paywalled and not got.blocked:
            got.notes.extend(_note_if_short(got))
            return _with_abstract(got, src)
        if got.blocked:
            blocked.append(got)
        if best is None or len(got.text) > len(best.text):
            best = got

    result = best or FetchedContent(url=src.url or "")
    if blocked and not result.blocked:
        # Some other address answered, but with nothing usable. The refusal is
        # still why this source went unread.
        result.blocked = True
        result.notes.extend(blocked[0].notes)
    return _with_abstract(result, src)


def _with_abstract(result: FetchedContent, src: ResolvedSource) -> FetchedContent:
    """Guarantee the abstract is part of whatever gets judged.

    Publisher pages are paywalled more often than not, but the abstract is
    almost always indexed somewhere. Leading with it means a locked-down source
    is still read on its actual content rather than scored against a login page
    — and when full text did arrive, the abstract costs nothing and states the
    paper's claims more directly than its introduction does.
    """
    abstract = (src.abstract or "").strip()
    result.has_abstract = bool(abstract)

    if not abstract:
        if len(result.text.strip()) < 400:
            result.notes.append(
                "Neither full text nor an abstract could be retrieved, so there "
                "was nothing to check this citation against."
            )
        return result

    if len(result.text.strip()) < 400:
        result.text = abstract
        result.kind = "abstract"
        result.ok = True
        result.title = result.title or src.title
        result.notes.append(
            "Full text was not retrievable (usually a paywall); judged against "
            "the indexed abstract."
        )
    elif abstract[:120].lower() not in result.text[:6000].lower():
        # Full text arrived, but extraction often starts mid-document — make
        # sure the abstract is in there rather than assuming it was captured.
        result.text = f"{abstract}\n\n{result.text}"
        result.notes.append("Indexed abstract was prepended to the retrieved text.")
    return result


def _note_if_short(got: FetchedContent) -> list[str]:
    if len(got.text) < 1200:
        return ["Retrieved page was unusually short; it may be a landing stub."]
    return []


def _fetch_one(url: str) -> FetchedContent:
    out = FetchedContent(url=url)
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True
        )
    except requests.RequestException as exc:
        out.notes.append(f"Request failed: {type(exc).__name__}")
        if any(hint in str(exc) for hint in ("NameResolutionError", "getaddrinfo", "Name or service not known")):
            out.dead = "no such host"
        return out

    out.status = resp.status_code
    out.final_url = resp.url
    ctype = (resp.headers.get("Content-Type") or "").lower()

    try:
        body = _read_capped(resp)
    except requests.RequestException as exc:
        out.notes.append(f"Download failed: {type(exc).__name__}")
        return out
    finally:
        resp.close()

    challenged = (
        resp.status_code in _REFUSED
        or bool(resp.headers.get("cf-mitigated"))
        or (resp.status_code in _CHALLENGE and _looks_like_challenge(body))
    )
    if challenged:
        out.blocked = True
        out.notes.append(
            f"The site refused automated access (HTTP {resp.status_code} from "
            f"{_host(resp.url)}). It may open normally in a browser."
        )
        return out

    if resp.status_code >= 400:
        out.notes.append(f"HTTP {resp.status_code} from {resp.url}")
        if resp.status_code in (404, 410):
            out.dead = f"HTTP {resp.status_code}"
        return out

    if "pdf" in ctype or body[:5] == b"%PDF-":
        out.kind = "pdf"
        out.pdf_bytes = body
        out.title, out.text = _pdf_text(body)
        out.ok = bool(out.text.strip())
        return out

    # Europe PMC serves JATS XML rather than a rendered page. It is the cleanest
    # full text available anywhere in this pipeline — no navigation furniture,
    # no cookie banner, no paywall stub — so it is worth parsing properly rather
    # than letting the HTML path scrape angle brackets off it.
    if "xml" in ctype and b"<article" in body[:4000]:
        out.kind = "fulltext-xml"
        out.title, out.text = _jats_text(body)
        out.ok = bool(out.text.strip())
        if out.ok:
            out.notes.append("Full text parsed from Europe PMC JATS XML.")
        return out

    out.kind = "html"
    out.title, out.text = _html_text(body, resp.encoding)
    lowered = out.text[:4000].lower()
    out.paywalled = any(h in lowered for h in _PAYWALL_HINTS) and len(out.text) < 6000
    if len(out.text) < 2500 and any(h in lowered for h in _BOT_HINTS):
        out.blocked = True
        # The challenge page is not the source. Left in, it gets judged as one.
        out.text = ""
        out.notes.append(
            f"The site answered with a bot check instead of the page ({_host(resp.url)}). "
            "It may open normally in a browser."
        )
        return out
    if out.paywalled:
        out.notes.append("Landing page looks paywalled or bot-gated.")
    out.ok = bool(out.text.strip())
    return out


def _host(url: str) -> str:
    return re.sub(r"^https?://([^/]+).*$", r"\1", url or "")


def _looks_like_challenge(body: bytes) -> bool:
    text = body[:6000].decode("utf-8", errors="replace").lower()
    return len(body) < 3000 or any(h in text for h in _BOT_HINTS)


def _read_capped(resp: requests.Response) -> bytes:
    buf = io.BytesIO()
    for chunk in resp.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > MAX_BYTES:
            break
    return buf.getvalue()


def _pdf_text(data: bytes) -> tuple[str, str]:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return "", ""
    try:
        title = (doc.metadata or {}).get("title") or ""
        pages = []
        for page in doc:
            pages.append(page.get_text("text") or "")
            if len(pages) >= 40:
                break
        text = "\n".join(pages)
    finally:
        doc.close()
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    return title.strip(), text.strip()


def _jats_text(data: bytes) -> tuple[str, str]:
    """Pull title, abstract and body prose out of a JATS full-text document."""
    try:
        soup = BeautifulSoup(data, "lxml-xml")
    except Exception:
        soup = BeautifulSoup(data, "lxml")

    title_node = soup.find("article-title")
    title = title_node.get_text(" ", strip=True) if title_node else ""

    # Tables, figures and the reference list are noise for claim matching, and
    # a cited paper's own bibliography is actively misleading — it is full of
    # other people's titles that match all sorts of claims.
    for tag in soup.find_all(["ref-list", "table-wrap", "fig", "table",
                              "back", "front-stub", "graphic", "inline-formula"]):
        tag.decompose()

    parts: list[str] = []
    abstract = soup.find("abstract")
    if abstract:
        parts.append(abstract.get_text(" ", strip=True))
    body = soup.find("body")
    if body:
        for node in body.find_all(["title", "p"]):
            chunk = node.get_text(" ", strip=True)
            if chunk:
                parts.append(chunk)

    text = "\n\n".join(p for p in parts if p)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return title.strip(), text.strip()


def _html_text(data: bytes, encoding: str | None) -> tuple[str, str]:
    try:
        html = data.decode(encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = data.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "") or ""

    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    # Prefer the real article body when the page marks one up.
    node = (
        soup.find("article")
        or soup.find(attrs={"id": re.compile(r"(article|content|main)", re.I)})
        or soup.find(attrs={"class": re.compile(r"(article|fulltext|content-body)", re.I)})
        or soup.find("main")
        or soup.body
        or soup
    )

    text = node.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Abstract often lives in a meta tag even when the body is gated.
    if len(text) < 500:
        for name in ("description", "citation_abstract", "og:description",
                     "dc.Description", "twitter:description"):
            meta = soup.find("meta", attrs={"name": name}) or soup.find(
                "meta", attrs={"property": name}
            )
            if meta and meta.get("content"):
                text = (text + "\n\n" + meta["content"]).strip()
                break

    if not title:
        meta_title = soup.find("meta", attrs={"name": "citation_title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"]

    return title.strip(), text.strip()
