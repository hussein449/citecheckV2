"""Capture the two screenshots the report is built around.

For every reference we want:
  1. a "header" shot — the top of the source, showing the title, journal, and
     authors, so a reader can confirm the link resolved to the right work;
  2. an "evidence" shot — the passage that best matches the citing claim,
     highlighted in place.

HTML sources are driven through Chromium (Playwright); PDF sources are rendered
directly with PyMuPDF, which finds and boxes the text far more reliably than a
browser's built-in PDF viewer.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

import fitz

VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT = 30_000


@dataclass
class Shots:
    header: str = ""          # path relative to the run directory
    evidence: str = ""
    page_title: str = ""
    matched_text: str = ""
    backend: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# JavaScript injected into the page to locate and box a passage. It builds a
# whitespace-normalised copy of the visible text along with a per-character map
# back into the DOM, then overlays boxes on the matching range's client rects.
_LOCATE_JS = r"""
(candidates) => {
  document.querySelectorAll('.__cc_hl').forEach(el => el.remove());

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      const tag = parent.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
        return NodeFilter.FILTER_REJECT;
      }
      if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      const style = window.getComputedStyle(parent);
      if (style.visibility === 'hidden' || style.display === 'none') {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });

  let flat = '';
  const map = [];
  let lastWasSpace = true;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const raw = node.nodeValue;
    for (let i = 0; i < raw.length; i++) {
      const ch = raw[i];
      if (/\s/.test(ch)) {
        if (!lastWasSpace) { flat += ' '; map.push({node, offset: i}); lastWasSpace = true; }
      } else {
        flat += ch; map.push({node, offset: i}); lastWasSpace = false;
      }
    }
    if (!lastWasSpace) { flat += ' '; map.push({node, offset: raw.length}); lastWasSpace = true; }
  }

  const haystack = flat.toLowerCase();
  let found = null;
  for (const candidate of candidates) {
    const needle = candidate.toLowerCase().replace(/\s+/g, ' ').trim();
    if (needle.length < 12) continue;
    const at = haystack.indexOf(needle);
    if (at !== -1) { found = {at, len: needle.length, text: flat.substr(at, needle.length)}; break; }
  }
  if (!found) return null;

  const startEntry = map[found.at];
  const endEntry = map[found.at + found.len - 1];
  if (!startEntry || !endEntry) return null;

  const range = document.createRange();
  try {
    range.setStart(startEntry.node, Math.min(startEntry.offset, startEntry.node.nodeValue.length));
    range.setEnd(endEntry.node, Math.min(endEntry.offset + 1, endEntry.node.nodeValue.length));
  } catch (err) {
    return null;
  }

  const rects = Array.from(range.getClientRects()).filter(r => r.width > 1 && r.height > 1);
  if (!rects.length) return null;

  const sx = window.scrollX, sy = window.scrollY;
  for (const r of rects) {
    const box = document.createElement('div');
    box.className = '__cc_hl';
    Object.assign(box.style, {
      position: 'absolute',
      left: (r.left + sx) + 'px',
      top: (r.top + sy) + 'px',
      width: r.width + 'px',
      height: r.height + 'px',
      backgroundColor: 'rgba(255, 214, 10, 0.40)',
      boxShadow: '0 0 0 2px rgba(214, 122, 0, 0.95)',
      borderRadius: '2px',
      pointerEvents: 'none',
      zIndex: '2147483647'
    });
    document.body.appendChild(box);
  }

  const top = rects[0].top + sy;
  window.scrollTo({top: Math.max(0, top - window.innerHeight / 3), behavior: 'instant'});
  return {text: found.text, rects: rects.length};
}
"""


# Preferred launch order. Playwright's own download is best, but a system
# Chrome or Edge works identically and saves a ~150 MB install.
_CHANNELS = (None, "chrome", "msedge")

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# A container grants Chromium neither the SYS_ADMIN its own sandbox needs nor a
# /dev/shm large enough to render a full page, so it dies on launch and every
# screenshot silently degrades. Both protections are worth keeping locally, so
# dropping them is opt-in through the environment.
if os.environ.get("CITECHECK_NO_SANDBOX") == "1":
    _LAUNCH_ARGS += ["--no-sandbox", "--disable-dev-shm-usage"]


def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _launch(pw):
    """Launch the first browser that actually starts; report what we used."""
    errors: list[str] = []
    for channel in _CHANNELS:
        try:
            kwargs = {"args": _LAUNCH_ARGS}
            if channel:
                kwargs["channel"] = channel
            return pw.chromium.launch(**kwargs), (channel or "chromium")
        except Exception as exc:
            errors.append(f"{channel or 'chromium'}: {_brief(exc)}")
    raise RuntimeError("No usable browser. Tried — " + "; ".join(errors))


# Playwright's sync API is thread-affine, so each worker keeps its own browser
# and reuses it. Launching Chrome per reference costs well over a second each —
# on a 250-reference bibliography that alone is several minutes of pure startup.
_local = threading.local()


def _thread_browser():
    """The calling thread's browser, started on first use."""
    browser = getattr(_local, "browser", None)
    if browser is not None and browser.is_connected():
        return browser, _local.channel

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser, channel = _launch(pw)
    except Exception:
        pw.stop()
        raise
    _local.pw, _local.browser, _local.channel = pw, browser, channel
    return browser, channel


def close_thread_browser() -> None:
    """Shut down this thread's browser. Must run on the owning thread."""
    for attr, shutdown in (("browser", "close"), ("pw", "stop")):
        obj = getattr(_local, attr, None)
        if obj is not None:
            try:
                getattr(obj, shutdown)()
            except Exception:
                pass
            setattr(_local, attr, None)


def browser_status() -> tuple[bool, str]:
    """Report whether a browser can actually be launched, and which one."""
    if not playwright_available():
        return False, "playwright is not installed"
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser, name = _launch(pw)
            browser.close()
        return True, name
    except Exception as exc:
        return False, _brief(exc)


def _informative(text: str) -> bool:
    """Reject search strings too generic to be worth highlighting.

    Short windows are what make the highlight land at all, but an 8-word run of
    stopwords, citation debris ("et al., 2018a; Radford et al") or equation
    fragments is a technically-correct match that tells the reader nothing.
    """
    from .match import _STOPWORDS

    words = re.findall(r"[A-Za-z][A-Za-z\-']+", text)
    content = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 3]
    if len(content) < 4:
        return False
    # Mostly digits, brackets and semicolons means a reference list or formula.
    noise = len(re.findall(r"[\d;()\[\]=+*/]", text))
    return noise <= len(text) * 0.18


def candidates_from(claim: str, passages: str | list[str]) -> list[str]:
    """Build progressively shorter search strings for the highlight step.

    Long spans are tried first because they make the most convincing highlight,
    but they fail whenever the text crosses a column, page, or markup boundary.
    Sliding word windows are the fallback that actually lands: somewhere in a
    passage there is almost always a contiguous run of ~8 words on one line.
    """
    out: list[str] = []

    def add(text: str) -> None:
        text = re.sub(r"\s+", " ", (text or "")).strip(" .,;:—-")
        if len(text) >= 24 and text not in out and _informative(text):
            out.append(text)

    if isinstance(passages, str):
        passages = [passages]
    # The claim comes from the citing paper, so it rarely appears in the source
    # — it goes last, as a long shot for directly quoted material.
    for source in [*passages, claim]:
        if not source:
            continue
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", source) if len(s.split()) >= 5]

        # Whole sentences, longest first — the best-looking highlights.
        for sentence in sorted(sentences, key=len, reverse=True)[:3]:
            add(sentence)

        # Then sliding windows, widest first, stepping across each sentence.
        for width in (14, 10, 8):
            for sentence in sentences:
                words = sentence.split()
                if len(words) < width:
                    continue
                for start in range(0, len(words) - width + 1, max(1, width // 2)):
                    add(" ".join(words[start:start + width]))

    return out[:40]


def capture(
    url: str,
    out_dir: Path,
    stem: str,
    claim: str,
    passage: str | list[str],
    pdf_bytes: bytes | None = None,
) -> Shots:
    """Capture header + evidence shots for one reference."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if pdf_bytes:
        return _capture_pdf(pdf_bytes, out_dir, stem, claim, passage)
    if not url:
        return Shots(notes=["No URL to screenshot."])
    return _capture_html(url, out_dir, stem, claim, passage)


# The uploaded paper is searched once per reference. Tokenising all its pages
# every time would mean 250 full passes over the same document, so the word
# boxes are computed once and shared.
_TOKEN_CACHE: dict[tuple[str, float], list[tuple[list[str], list[list]]]] = {}
_TOKEN_LOCK = threading.Lock()


def _cached_tokens(pdf_path: str) -> list[tuple[list[str], list[list]]]:
    try:
        key = (str(pdf_path), os.path.getmtime(pdf_path))
    except OSError:
        return []

    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []
    try:
        pages = [_page_tokens(doc.load_page(i)) for i in range(doc.page_count)]
    except Exception:
        pages = []
    finally:
        doc.close()

    with _TOKEN_LOCK:
        _TOKEN_CACHE[key] = pages
    return pages


def capture_citing(
    pdf_path: str,
    out_dir: Path,
    stem: str,
    sentences: list[str],
    page_hint: int | None = None,
) -> tuple[str, int | None]:
    """Highlight a citing sentence inside the user's own uploaded paper.

    Returns (filename, 1-based page) so the report can show where in *their*
    document a questionable citation actually sits.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}_citing.png"

    candidates: list[str] = []
    for sentence in sentences:
        candidates.extend(candidates_from("", sentence))
    if not candidates:
        return "", None

    hit = _search_pages(_cached_tokens(pdf_path), candidates, start_page=page_hint)
    if not hit:
        return "", None

    page_no, rects, _ = hit
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return "", None
    try:
        page = doc.load_page(page_no)
        for rect in rects:
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=(0.42, 0.66, 1.0))   # blue: "your paper"
            annot.update()
        page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(str(path))
        return path.name, page_no + 1
    except Exception:
        return "", None
    finally:
        doc.close()


def render_abstract_card(
    out_dir: Path,
    stem: str,
    title: str,
    abstract: str,
    quote: str,
    source_label: str,
    url: str = "",
) -> str:
    """Render the indexed abstract as a labelled evidence card.

    Some publishers (IEEE, SAGE, AIAA) serve a bot-check page instead of the
    article, so there is genuinely nothing to screenshot — and defeating those
    checks is off the table. The abstract itself was retrieved legitimately from
    Crossref/OpenAlex/Semantic Scholar, so it is rendered here instead, captioned
    as a record rather than a page capture so it can never be mistaken for one.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}_evidence.png"

    width, margin, scratch = 900, 48, 4000
    inner = fitz.Rect(margin, 0, width - margin, 0)

    doc = fitz.open()
    page = doc.new_page(width=width, height=scratch)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))

    def block(text: str, y: float, size: float, font: str, colour, gap: float) -> float:
        """Insert wrapped text and return the y just below it."""
        if not text:
            return y
        box = fitz.Rect(inner.x0, y, inner.x1, scratch - margin)
        unused = page.insert_textbox(
            box, _ascii(text), fontsize=size, fontname=font,
            color=colour, lineheight=1.45,
        )
        consumed = box.height - unused if unused >= 0 else box.height
        return y + consumed + gap

    y = margin
    banner = fitz.Rect(margin, y, width - margin, y + 44)
    page.draw_rect(banner, color=None, fill=(0.96, 0.93, 0.82))
    page.insert_textbox(
        fitz.Rect(margin + 12, y + 15, width - margin - 12, y + 42),
        _ascii(f"INDEXED ABSTRACT - not a screenshot of the publisher page  |  via {source_label}"),
        fontsize=9.5, fontname="hebo", color=(0.35, 0.28, 0.05),
    )
    y = banner.y1 + 22

    y = block(title or "(untitled)", y, 15, "hebo", (0.06, 0.08, 0.12), 8)
    y = block(url, y, 8.5, "helv", (0.35, 0.4, 0.5), 18)
    y = block(abstract or "(no abstract available)", y, 11, "helv", (0.1, 0.12, 0.18), 20)

    page.draw_line(fitz.Point(margin, y), fitz.Point(width - margin, y), color=(0.85, 0.86, 0.9))
    y = block(
        "The publisher page could not be captured (bot check or paywall). This card "
        "shows the abstract as held by the indexing service named above.",
        y + 14, 8.5, "helv", (0.45, 0.45, 0.5), 0,
    )

    if quote:
        for probe in candidates_from("", quote)[:12]:
            found = page.search_for(_ascii(probe)[:160], quads=False)
            if found:
                for rect in found:
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=(1.0, 0.84, 0.04))
                    annot.update()
                break

    # Crop to the content rather than shipping a page of whitespace.
    clip = fitz.Rect(0, 0, width, min(scratch, y + margin))
    page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip).save(str(path))
    doc.close()
    return path.name


# The base-14 PDF fonts have no glyphs for typographic punctuation, which turns
# an em-dash into "?" on the rendered card.
_PUNCT = {
    "—": "-", "–": "-", "‒": "-", "−": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ", "•": "-", "·": "-",
}


def _ascii(text: str) -> str:
    for fancy, plain in _PUNCT.items():
        text = text.replace(fancy, plain)
    return text


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def _capture_html(
    url: str, out_dir: Path, stem: str, claim: str, passage: str | list[str]
) -> Shots:
    shots = Shots(backend="chromium")
    if not playwright_available():
        shots.notes.append("Playwright is not installed - run `pip install playwright`.")
        return shots

    from playwright.sync_api import Error as PlaywrightError

    header_path = out_dir / f"{stem}_header.png"
    evidence_path = out_dir / f"{stem}_evidence.png"

    try:
        browser, channel = _thread_browser()
    except Exception as exc:
        shots.notes.append(f"Could not start a browser: {_brief(exc)}")
        return shots

    shots.backend = channel
    context = None
    try:
        # A fresh context per reference keeps cookies and storage isolated
        # while reusing the (expensive) browser process itself.
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except PlaywrightError as exc:
            shots.notes.append(f"Navigation problem: {_brief(exc)}")

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightError:
            pass  # Many publisher pages never go idle; carry on.

        shots.page_title = (page.title() or "").strip()

        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(header_path))
        shots.header = header_path.name

        located = None
        try:
            located = page.evaluate(_LOCATE_JS, candidates_from(claim, passage))
        except PlaywrightError as exc:
            shots.notes.append(f"Highlight step failed: {_brief(exc)}")

        if located:
            shots.matched_text = located.get("text", "")
            page.screenshot(path=str(evidence_path))
            shots.evidence = evidence_path.name
        else:
            shots.notes.append(
                "The matching passage was not found in the rendered page "
                "(often a paywall, or the text lives in a linked PDF)."
            )
    except Exception as exc:
        shots.notes.append(f"Browser capture failed: {_brief(exc)}")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    if not shots.evidence and shots.header:
        shots.notes.append("Only the header screenshot is available for this reference.")
    return shots


def _brief(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:180]


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _capture_pdf(data: bytes, out_dir: Path, stem: str, claim: str, passage: str) -> Shots:
    """Render the first page, then find and box the passage on its own page."""
    shots = Shots(backend="pdf")
    header_path = out_dir / f"{stem}_header.png"
    evidence_path = out_dir / f"{stem}_evidence.png"

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        shots.notes.append(f"Could not open the cited PDF: {_brief(exc)}")
        return shots

    try:
        shots.page_title = (doc.metadata or {}).get("title", "") or ""
        zoom = fitz.Matrix(2, 2)

        first = doc.load_page(0)
        first.get_pixmap(matrix=zoom).save(str(header_path))
        shots.header = header_path.name

        hit = _find_in_pdf(doc, candidates_from(claim, passage))
        if not hit:
            shots.notes.append("The matching passage was not found in the cited PDF's text layer.")
            return shots

        page_no, rects, matched = hit
        page = doc.load_page(page_no)
        for rect in rects:
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=(1.0, 0.84, 0.04))
            annot.update()

        pix = page.get_pixmap(matrix=zoom)
        pix.save(str(evidence_path))
        shots.evidence = evidence_path.name
        shots.matched_text = matched
        shots.notes.append(f"Passage found on page {page_no + 1} of the cited PDF.")
    except Exception as exc:
        shots.notes.append(f"PDF capture failed: {_brief(exc)}")
    finally:
        doc.close()
    return shots


def _norm_word(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _page_tokens(page: fitz.Page) -> tuple[list[str], list[list]]:
    """Normalised word tokens for a page, with the rects they came from.

    `search_for` cannot match our candidates directly: the text we extracted was
    de-hyphenated ("representa-\\ntions" -> "representations") while the page
    itself still carries the break. Rebuilding from word boxes lets us rejoin
    hyphenated pairs the same way, so both sides line up — and it makes matches
    immune to line wraps and column breaks.
    """
    raw = page.get_text("words") or []           # (x0, y0, x1, y1, word, ...)
    tokens: list[str] = []
    rects: list[list] = []

    index = 0
    while index < len(raw):
        x0, y0, x1, y1, word = raw[index][:5]
        boxes = [fitz.Rect(x0, y0, x1, y1)]
        # A trailing hyphen means the word continues on the next line.
        while word.endswith("-") and index + 1 < len(raw):
            index += 1
            nx0, ny0, nx1, ny1, nxt = raw[index][:5]
            word = word[:-1] + nxt
            boxes.append(fitz.Rect(nx0, ny0, nx1, ny1))
            if not word.endswith("-"):
                break
        normalised = _norm_word(word)
        if normalised:
            tokens.append(normalised)
            rects.append(boxes)
        index += 1
    return tokens, rects


def _find_sublist(haystack: list[str], needle: list[str]) -> int:
    if not needle or len(needle) > len(haystack):
        return -1
    first = needle[0]
    span = len(needle)
    for start in range(len(haystack) - span + 1):
        if haystack[start] == first and haystack[start:start + span] == needle:
            return start
    return -1


def _find_in_pdf(
    doc: fitz.Document,
    candidates: list[str],
    start_page: int | None = None,
) -> tuple[int, list, str] | None:
    """Find the first candidate that appears in the document, best-first.

    `start_page` (1-based) is a hint: that page is searched first, which matters
    when the same sentence pattern could match in several places.
    """
    page_count = min(doc.page_count, 60)
    pages: list[tuple[list[str], list[list]]] = []
    for page_no in range(page_count):
        try:
            pages.append(_page_tokens(doc.load_page(page_no)))
        except Exception:
            pages.append(([], []))
    return _search_pages(pages, candidates, start_page)


def _search_pages(
    pages: list[tuple[list[str], list[list]]],
    candidates: list[str],
    start_page: int | None = None,
) -> tuple[int, list, str] | None:
    """Find the first candidate present in pre-tokenised pages."""
    page_count = len(pages)
    order = list(range(page_count))
    if start_page and 1 <= start_page <= page_count:
        hint = start_page - 1
        order = [hint] + [p for p in order if p != hint]

    for candidate in candidates:
        needle = [t for t in (_norm_word(w) for w in candidate.split()) if t]
        if len(needle) < 4:
            continue
        for page_no in order:
            tokens, rects = pages[page_no]
            at = _find_sublist(tokens, needle)
            if at == -1:
                continue
            matched: list = []
            for boxes in rects[at:at + len(needle)]:
                matched.extend(boxes)
            if matched:
                return page_no, matched, candidate
    return None
