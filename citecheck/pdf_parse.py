"""Turn an uploaded PDF into clean, position-aware text.

The rest of the pipeline needs three things out of a paper:
  * readable body prose (hyphenation repaired, columns in reading order),
  * a page number for every stretch of text, so findings can be cited back,
  * the boundary where the bibliography starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF


# Headings that mark the start of the bibliography. Matched against a whole
# short line, so "References" wins but "...references therein" does not.
_REF_HEADING = re.compile(
    r"^\s*(?:\d+\s*\.?\s*|[IVXLC]+\s*\.\s*)?"
    r"(references?|bibliography|works\s+cited|literature\s+cited|"
    r"references?\s+and\s+notes|reference\s+list)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)

# Sections that legitimately follow the bibliography in some templates. If we
# see one of these we stop consuming reference text.
_POST_REF_HEADING = re.compile(
    r"^\s*(?:\d+\s*\.?\s*)?(appendix|appendices|supplementary|supporting\s+information|"
    r"author\s+contributions?|acknowledge?ments?|about\s+the\s+authors?|"
    r"biograph(?:y|ies)|funding|conflicts?\s+of\s+interest)\b",
    re.IGNORECASE,
)


@dataclass
class Page:
    number: int          # 1-based
    text: str


@dataclass
class ParsedPDF:
    path: str
    pages: list[Page]
    body_text: str
    references_text: str
    references_page: int | None
    meta: dict = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    def page_of_offset(self, offset: int) -> int:
        """Map a character offset in ``body_text`` back to a 1-based page."""
        running = 0
        for page in self.pages:
            running += len(page.text) + 1
            if offset < running:
                return page.number
        return self.pages[-1].number if self.pages else 1


# Zero-width and soft-hyphen characters used by publishers as line-break hints.
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")
# Non-breaking, thin, and other exotic spaces, normalised to a plain space.
_ODD_SPACES = re.compile("[       ]")
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff",
              "ﬃ": "ffi", "ﬄ": "ffl"}


def _dehyphenate(text: str) -> str:
    """Rejoin words split across a line break: "agri-\ncultural" -> "agricultural"."""
    return re.sub(r"(\w)[-‐‑]\s*\n\s*(\w)", r"\1\2", text)


def _normalise_whitespace(text: str) -> str:
    # Publishers inject invisible break opportunities into long strings, so a
    # DOI printed as "10.<ZWSP>1109/<ZWSP>ISTAS" reads normally but matches no
    # pattern. Strip them before anything tries to read identifiers out.
    text = _INVISIBLE.sub("", text)
    text = _ODD_SPACES.sub(" ", text)
    for ligature, plain in _LIGATURES.items():
        text = text.replace(ligature, plain)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _looks_like_running_header(line: str, seen: dict[str, int], total_pages: int) -> bool:
    """A short line repeated on most pages is a header/footer, not content."""
    key = re.sub(r"\d+", "#", line.strip().lower())
    if len(key) < 4 or len(key) > 90:
        return False
    return seen.get(key, 0) >= max(3, total_pages // 2)


def _page_text(page: fitz.Page) -> str:
    """Extract text in reading order, tolerating multi-column layouts."""
    blocks = page.get_text("blocks", sort=True)
    parts: list[str] = []
    for block in blocks:
        # (x0, y0, x1, y1, text, block_no, block_type)
        if len(block) >= 7 and block[6] != 0:
            continue  # image block
        chunk = (block[4] or "").strip()
        if chunk:
            parts.append(chunk)
    if parts:
        return "\n\n".join(parts)
    return page.get_text("text") or ""


def parse_pdf(path: str) -> ParsedPDF:
    """Read *path* and split it into body prose and bibliography text."""
    doc = fitz.open(path)
    try:
        raw_pages = [_page_text(page) for page in doc]
        meta = {
            "title": (doc.metadata or {}).get("title") or "",
            "author": (doc.metadata or {}).get("author") or "",
            "page_count": doc.page_count,
        }
    finally:
        doc.close()

    # Identify repeated headers/footers so they don't pollute sentences.
    line_counts: dict[str, int] = {}
    for text in raw_pages:
        for line in {l.strip() for l in text.splitlines() if l.strip()}:
            key = re.sub(r"\d+", "#", line.lower())
            line_counts[key] = line_counts.get(key, 0) + 1

    pages: list[Page] = []
    for idx, text in enumerate(raw_pages, start=1):
        kept = [
            line
            for line in text.splitlines()
            if not _looks_like_running_header(line, line_counts, len(raw_pages))
        ]
        cleaned = _normalise_whitespace(_dehyphenate("\n".join(kept)))
        pages.append(Page(number=idx, text=cleaned))

    body, refs, ref_page = _split_references(pages)
    return ParsedPDF(
        path=path,
        pages=pages,
        body_text=body,
        references_text=refs,
        references_page=ref_page,
        meta=meta,
    )


def _split_references(pages: list[Page]) -> tuple[str, str, int | None]:
    """Find the bibliography heading and cut the document in two there.

    Papers occasionally mention "References" early (e.g. in a section list), so
    we search from the back and require the heading to sit in the latter part of
    the document.
    """
    joined = "\n".join(p.text for p in pages)
    lines = joined.splitlines()
    if not lines:
        return joined, "", None

    earliest = int(len(lines) * 0.45)
    heading_idx: int | None = None
    for idx in range(len(lines) - 1, earliest - 1, -1):
        if _REF_HEADING.match(lines[idx]):
            heading_idx = idx
            break

    if heading_idx is None:
        # Fall back: a run of lines that look like numbered bibliography entries.
        heading_idx = _guess_reference_start(lines, earliest)

    if heading_idx is None:
        return joined, "", None

    body = "\n".join(lines[:heading_idx])
    tail = lines[heading_idx + 1 :]

    # Stop at an appendix / acknowledgements heading if one follows.
    end = len(tail)
    for idx, line in enumerate(tail):
        if idx > 5 and _POST_REF_HEADING.match(line):
            end = idx
            break
    refs = "\n".join(tail[:end])

    consumed = len("\n".join(lines[:heading_idx]))
    ref_page = _page_for_line_offset(pages, consumed)
    return body, refs, ref_page


def _guess_reference_start(lines: list[str], earliest: int) -> int | None:
    """Detect an unlabelled bibliography by its dense run of "[n]" entries."""
    marker = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+\S")
    best: int | None = None
    run = 0
    for idx in range(earliest, len(lines)):
        if marker.match(lines[idx]):
            run += 1
            if run >= 4 and best is None:
                best = idx - run + 1
        elif lines[idx].strip():
            if run and best is not None and run < 4:
                best = None
            run = 0
    return best


def _page_for_line_offset(pages: list[Page], char_offset: int) -> int:
    running = 0
    for page in pages:
        running += len(page.text) + 1
        if char_offset < running:
            return page.number
    return pages[-1].number if pages else 1
