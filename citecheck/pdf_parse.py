"""Turn an uploaded PDF into clean, position-aware text.

The rest of the pipeline needs three things out of a paper:
  * readable body prose (hyphenation repaired, columns in reading order),
  * a page number for every stretch of text, so findings can be cited back,
  * the boundary where the bibliography starts.
"""

from __future__ import annotations

import re
import unicodedata
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
    r"biograph(?:y|ies)|funding|conflicts?\s+of\s+interest|"
    r"methods?(?:\s+summary)?\s*$|extended\s+data|data\s+availability|code\s+availability|"
    r"author\s+information|competing\s+interests?)",
    re.IGNORECASE,
)
# ICLR and NeurIPS head an appendix "A  RUBBISH CLASS EXAMPLES": one letter, then
# capitals. A reference entry is never a line of capitals.
_CAPITALS_APPENDIX = re.compile(
    r"^\s*[A-Z]\s+[A-Z][A-Z0-9 \-:,&]{5,70}$"
    # CVPR: "A. Object Detection Baselines". Two or more capitalised words, so
    # that an author's "A. Krizhevsky" at the start of a wrapped line is not one.
    r"|^\s*A\.\s+(?:[A-Z][a-z]+\s+){1,6}[A-Z][a-z]+\s*$"
)

# ACL, NeurIPS and most LaTeX templates letter their appendices and never print
# the word: the section after the bibliography is headed "A  Annotation
# Guidelines". PDF extraction usually puts the letter on a line of its own.
_APPENDIX_TITLE = r"[A-Z][^.,;:\d]{2,60}"
_APPENDIX_A = re.compile(rf"^\s*A(?:\s+{_APPENDIX_TITLE})?\s*$")
_APPENDIX_NEXT = re.compile(rf"^\s*(?:A\.1\b|B(?:\s+{_APPENDIX_TITLE})?\s*$)")


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
# The same set written out as escapes, so the list-marker rule below can build
# its own class from it without repeating characters no diff can show.
_INVISIBLE_CLASS = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
# A numbered list whose only separator is a zero-width space. Word and Google
# Docs both export bibliographies this way: "12.<ZWSP>I Abdulrashid" reads as
# "12. I Abdulrashid" on the page, but deleting the character outright welds the
# number onto the entry and leaves the bibliography splitter no boundary to
# find, which drops every such entry from the reference list. Widen it to a real
# space instead. A DOI carries the same character in the same position
# ("10.<ZWSP>1109/"), so this stops short of the digit that always follows a DOI
# prefix and leaves those to be deleted as before.
_LIST_MARKER_INVISIBLE = re.compile(
    rf"(?m)^([ \t]*\d{{1,3}}[.)])[{_INVISIBLE_CLASS}]+(?=[^\d\s])"
)
# Non-breaking, thin, and other exotic spaces, normalised to a plain space.
_ODD_SPACES = re.compile("[       ]")
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff",
              "ﬃ": "ffi", "ﬄ": "ffl"}

# LaTeX draws an accented letter as two glyphs, the accent and then the letter,
# and extraction hands them back in that order: "Rocktäschel" arrives as
# "Rockt¨aschel" and "Álvaro" as "´Alvaro". The loose accent is not a letter, so
# the surname stops matching halfway through and the citation is lost on both
# sides — the marker in the prose and the entry in the bibliography.
_LOOSE_ACCENTS = {
    "\u00a8": "\u0308", "\u00b4": "\u0301", "\u02c6": "\u0302",
    "\u02dc": "\u0303", "\u02c7": "\u030c", "\u02da": "\u030a",
    "\u02d8": "\u0306", "\u00af": "\u0304", "\u02d9": "\u0307",
}
_DOTLESS_I = "\u0131"
_LOOSE_ACCENT = re.compile(
    "([" + "".join(_LOOSE_ACCENTS) + "])([A-Za-z" + _DOTLESS_I + "])"
)


_LOOSE_CEDILLA = re.compile("([cCsStT])\u00b8 ?(?=[a-z])")
# A grave accent drawn as a backtick inside a word: "Rodol`a".
_LOOSE_GRAVE = re.compile("(?<=[A-Za-z])`([aeiouAEIOU])")


def _compose_accents(text: str) -> str:
    def compose(match: re.Match) -> str:
        letter = "i" if match.group(2) == _DOTLESS_I else match.group(2)
        composed = unicodedata.normalize("NFC", letter + _LOOSE_ACCENTS[match.group(1)])
        # No such letter: leave the text exactly as it was printed.
        return composed if len(composed) == 1 else match.group(0)

    text = _LOOSE_ACCENT.sub(compose, text)
    # The cedilla is drawn after its letter, and sometimes with a gap before
    # the rest of the word: "Gülc¸ehre, C¸ aglar".
    text = _LOOSE_GRAVE.sub(
        lambda m: unicodedata.normalize("NFC", m.group(1) + "\u0300"), text
    )
    return _LOOSE_CEDILLA.sub(
        lambda m: unicodedata.normalize("NFC", m.group(1) + "\u0327"), text
    )


def _dehyphenate(text: str) -> str:
    """Rejoin words split across a line break: "agri-\ncultural" -> "agricultural"."""
    return re.sub(r"(\w)[-‐‑]\s*\n\s*(\w)", r"\1\2", text)


def _normalise_whitespace(text: str) -> str:
    # Publishers inject invisible break opportunities into long strings, so a
    # DOI printed as "10.<ZWSP>1109/<ZWSP>ISTAS" reads normally but matches no
    # pattern. Strip them before anything tries to read identifiers out — but
    # widen the ones doing a separator's job first, or the strip destroys the
    # only boundary between a list number and the entry it introduces.
    text = _LIST_MARKER_INVISIBLE.sub(r"\1 ", text)
    text = _INVISIBLE.sub("", text)
    text = _ODD_SPACES.sub(" ", text)
    for ligature, plain in _LIGATURES.items():
        text = text.replace(ligature, plain)
    text = _compose_accents(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _looks_like_running_header(line: str, seen: dict[str, int], total_pages: int) -> bool:
    """A short line repeated on most pages is a header/footer, not content."""
    key = re.sub(r"\d+", "#", line.strip().lower())
    if len(key) < 4 or len(key) > 90:
        return False
    return seen.get(key, 0) >= max(3, total_pages // 2)


def _order_blocks(blocks: list, page: fitz.Page) -> list:
    """Put a page's text blocks into reading order.

    PyMuPDF's own `sort=True` orders blocks by position across the whole page,
    which on a two-column paper interleaves the columns: a left-column block at
    y=64 is followed by a right-column block at y=65, so consecutive sentences
    come from opposite sides of the gutter. Prose survives that badly and a
    reference list not at all — entries arrive shredded into alternating halves
    and none of them parse.

    So columns are detected from the gutter and read one at a time. Blocks that
    span the gutter (running headers, wide tables) split the page into bands and
    are emitted in place, which keeps a full-width element from jumping to the
    top of the page.
    """
    mid = page.rect.x0 + page.rect.width / 2
    slack = page.rect.width * 0.02

    def side(block) -> int:
        if block[2] <= mid + slack:
            return 0                     # left of the gutter
        if block[0] >= mid - slack:
            return 1                     # right of it
        return -1                        # spans it

    by_position = sorted(blocks, key=lambda b: (round(b[1], 1), b[0]))
    sides = [side(b) for b in blocks]
    # One column, or a single stray block on the far side of a wide one: reading
    # order is plain top-to-bottom and splitting on a gutter would invent one.
    if sides.count(0) < 2 or sides.count(1) < 2:
        return by_position

    def band(pending: list) -> list:
        return sorted(pending, key=lambda b: (side(b), round(b[1], 1), b[0]))

    ordered: list = []
    pending: list = []
    for block in by_position:
        if side(block) == -1:
            ordered.extend(band(pending))
            pending = []
            ordered.append(block)
        else:
            pending.append(block)
    ordered.extend(band(pending))
    return ordered


# Nature, Science and most biomedical journals cite with a bare superscript:
# "statistical inference¹⁻⁵". Extracted as text that is "inference1–5", which
# cannot be told from "Cas9", "hg38" or "RuleSet1" by its characters. The PDF
# still knows which glyphs were raised, so the markers are read from the layout
# and rewritten as "[1–5]", the form the rest of the pipeline already handles.
_SUPERSCRIPT_CITE = re.compile(r"^\d{1,3}(?:\s*[,\-‐‑–—]\s*\d{1,3})*$")
_SUPERSCRIPT_PART = re.compile(r"^[\d\s,\-‐‑–—]+$")
_INLINE_BRACKET = re.compile(r"\S ?\[\d{1,3}(?:\s*[-–—,;]\s*\d{1,3})*\]")
_FLAG_SUPERSCRIPT = 1
_FLAG_ITALIC = 2


def _superscript_runs(line: dict) -> list[tuple[int, int, float]]:
    """(first span, one past the last, size) of each raised citation in *line*."""
    spans = line.get("spans") or []
    if not spans:
        return []
    full_size = max(span["size"] for span in spans)

    def raised(span: dict) -> bool:
        # The flag alone is not enough: it stays set on the full-size text that
        # follows a superscript.
        return bool(
            span["flags"] & _FLAG_SUPERSCRIPT
            and span["size"] < full_size * 0.85
            and _SUPERSCRIPT_PART.match(span["text"])
        )

    runs: list[tuple[int, int, float]] = []
    idx = 0
    while idx < len(spans):
        if not raised(spans[idx]):
            idx += 1
            continue
        end = idx
        while end < len(spans) and raised(spans[end]):
            end += 1
        text = "".join(span["text"] for span in spans[idx:end]).strip()
        # A raised number after an italic letter is an exponent, not a citation.
        after_variable = idx > 0 and spans[idx - 1]["flags"] & _FLAG_ITALIC
        if _SUPERSCRIPT_CITE.match(text) and not after_variable:
            runs.append((idx, end, round(spans[idx]["size"], 1)))
        idx = end
    return runs


def _line_text(line: dict, cite_size: float | None) -> str:
    spans = line.get("spans") or []
    out: list[str] = []
    at = 0
    for start, end, size in _superscript_runs(line) if cite_size is not None else []:
        if abs(size - cite_size) > 0.3:
            continue
        out.extend(span["text"] for span in spans[at:start])
        raw = "".join(span["text"] for span in spans[start:end])
        out.append(f"[{raw.strip()}]" + raw[len(raw.rstrip()):])
        at = end
    out.extend(span["text"] for span in spans[at:])
    return "".join(out)


def _marked_pages(doc: fitz.Document) -> list[str] | None:
    """Page texts with superscript citations bracketed, or None if there are none.

    Author affiliations and footnote marks are raised numbers too. Citations
    are told from them by being set at one size throughout and by turning up on
    page after page, where affiliations stay on the first.
    """
    layouts = [page.get_text("dict") for page in doc]
    sizes: dict[float, set[int]] = {}
    counts: dict[float, int] = {}
    for number, layout in enumerate(layouts):
        for block in layout["blocks"]:
            for line in block.get("lines") or []:
                for _, _, size in _superscript_runs(line):
                    sizes.setdefault(size, set()).add(number)
                    counts[size] = counts.get(size, 0) + 1
    if not counts:
        return None
    cite_size = max(counts, key=counts.get)
    if counts[cite_size] < 10 or len(sizes[cite_size]) < 3:
        return None

    pages: list[str] = []
    for page, layout in zip(doc, layouts):
        blocks = [
            (*block["bbox"], "\n".join(_line_text(l, cite_size) for l in block["lines"]),
             block.get("number", 0), 0)
            for block in layout["blocks"]
            if block.get("type") == 0
        ]
        pages.append(_join_blocks(blocks, page))
    return pages


def _page_text(page: fitz.Page) -> str:
    """Extract text in reading order, tolerating multi-column layouts."""
    # (x0, y0, x1, y1, text, block_no, block_type)
    blocks = [b for b in page.get_text("blocks") if len(b) < 7 or b[6] == 0]
    return _join_blocks(blocks, page)


def _join_blocks(blocks: list, page: fitz.Page) -> str:
    parts: list[str] = []
    for block in _order_blocks(blocks, page):
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
        # Only when the paper does not already cite in brackets: one that does
        # uses its superscripts for something else.
        marked = _marked_pages(doc)
        if marked and sum(
            len(_INLINE_BRACKET.findall(a)) - len(_INLINE_BRACKET.findall(b))
            for a, b in zip(marked, raw_pages)
        ) <= sum(len(_INLINE_BRACKET.findall(b)) for b in raw_pages):
            marked = None
        meta = {
            "title": (doc.metadata or {}).get("title") or "",
            "author": (doc.metadata or {}).get("author") or "",
            "page_count": doc.page_count,
        }
    finally:
        doc.close()

    plain = _assemble(raw_pages)
    if marked:
        # And only when the bibliography is numbered. A superscript can cite
        # entry 12; it cannot cite "Duchi et al., 2011", so in an author-year
        # paper every raised number is an exponent or a footnote.
        from .refs import parse_references

        cited = _assemble(marked)
        if any(r.number is not None for r in parse_references(cited[2])):
            plain = cited
    pages, body, refs, ref_page = plain
    return ParsedPDF(
        path=path,
        pages=pages,
        body_text=body,
        references_text=refs,
        references_page=ref_page,
        meta=meta,
    )


def _assemble(raw_pages: list[str]) -> tuple[list[Page], str, str, int | None]:
    """Clean the page texts and split them into body and bibliography."""
    # Identify repeated headers/footers so they don't pollute sentences.
    line_counts: dict[str, int] = {}
    for text in raw_pages:
        # Once per page, counted after the digits are masked. Counted before,
        # the "(1916)." and "(1918)." ending two entries on one page were two
        # sightings of "(#).", and a reference list wrapped that way put the
        # line on "more pages than the paper has": every such year was deleted
        # as a running header, and so was every entry that was a bare URL.
        keys = {re.sub(r"\d+", "#", l.strip().lower()) for l in text.splitlines() if l.strip()}
        for key in keys:
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
    return pages, body, refs, ref_page


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

    from .refs import parse_references

    # Where the heading sits says little. A paper with long methods, appendices
    # or supplementary material after its bibliography has "References" less
    # than half way through, and a table column headed "Reference" is the same
    # line as the heading. What tells them apart is what follows: take the
    # heading with the most parseable entries under it, the later one on a tie.
    earliest = int(len(lines) * 0.1)
    candidates = [i for i in range(len(lines) - 1, earliest - 1, -1) if _REF_HEADING.match(lines[i])]
    heading_idx: int | None = None
    best = 0
    for idx in candidates:
        found = len(parse_references(_reference_tail(lines, idx)))
        if found > best:
            heading_idx, best = idx, found
    if best < 3:
        # Nothing convincing under any heading: keep the old reading, the last
        # heading in the latter part of the document, if there is one.
        late = [i for i in candidates if i >= len(lines) * 0.45]
        heading_idx = late[0] if late else None

    if heading_idx is None or best < 3:
        # Physics templates print no heading at all: the list simply starts.
        guessed = _guess_reference_start(lines, earliest)
        if guessed is not None:
            heading_idx = guessed - 1

    if heading_idx is None:
        return joined, "", None

    # The list as one or more stretches of lines. Nature-format papers number
    # one list across two: 1-50 after the main text, 51-62 after the Methods.
    first_end = _reference_end(lines, heading_idx)
    stretches = [(heading_idx + 1, first_end)]
    refs = "\n".join(lines[heading_idx + 1 : first_end])
    while True:
        numbers = [r.number for r in parse_references(refs) if r.number is not None]
        resume = _continuation(lines, stretches[-1][1], max(numbers) + 1) if numbers else None
        if resume is None:
            break
        end = _reference_end(lines, resume - 1)
        stretches.append((resume, end))
        refs += "\n" + "\n".join(lines[resume:end])

    # Everything that is not the list is body, including what follows it: the
    # methods, appendices and tables of a paper cite references too, and read
    # as "never cited" if the text stops at the bibliography. The list itself
    # is blanked rather than cut, so every offset still maps to its page.
    listed = {heading_idx} | {i for start, end in stretches for i in range(start, end)}
    body = "\n".join(" " * len(line) if i in listed else line for i, line in enumerate(lines))

    consumed = len("\n".join(lines[:heading_idx]))
    ref_page = _page_for_line_offset(pages, consumed)
    return body, refs, ref_page


def _reference_end(lines: list[str], heading_idx: int) -> int:
    """Index of the first line after the list that starts below ``heading_idx``."""
    start = heading_idx + 1
    for idx in range(start + 6, len(lines)):
        if (
            _POST_REF_HEADING.match(lines[idx])
            or _CAPITALS_APPENDIX.match(lines[idx])
            or _opens_lettered_appendix(lines[start:], idx - start)
        ):
            return idx
    return len(lines)


def _reference_tail(lines: list[str], heading_idx: int) -> str:
    """The bibliography text under the heading at ``lines[heading_idx]``."""
    return "\n".join(lines[heading_idx + 1 : _reference_end(lines, heading_idx)])


def _continuation(lines: list[str], after: int, number: int) -> int | None:
    """Where a numbered list picks up again at *number*, if it does."""
    for marker in _LIST_MARKERS:
        found = [(i, int(m.group(1))) for i in range(after, len(lines))
                 if (m := marker.match(lines[i]))]
        for pos, (idx, n) in enumerate(found):
            if n != number:
                continue
            # One matching number is a coincidence; the next one after it is a list.
            if any(m == number + 1 and j - idx <= 60 for j, m in found[pos + 1:pos + 8]):
                return idx
    return None


def _opens_lettered_appendix(lines: list[str], idx: int) -> bool:
    """Whether ``lines[idx]`` is the heading of a lettered "Appendix A".

    A lone "A" is also how a wrapped title can begin, so the heading alone is
    not enough: it only counts when the appendix goes on to number itself, with
    an "A.1" subsection or a "B" heading further down. Without the cut, the
    appendix is parsed as bibliography, and any numbered list inside it is taken
    for a numbered reference list that replaces the real one.
    """
    line = lines[idx]
    if not _APPENDIX_A.match(line):
        return False
    following = [l for l in lines[idx + 1 :] if l.strip()]
    if line.strip() == "A" and not (following and re.match(r"\s*[A-Z]", following[0])):
        return False
    # "A" alone, then a line of capitals: the ICLR appendix heading split over
    # two lines. No reference entry reads like that.
    if line.strip() == "A" and re.fullmatch(r"\s*[A-Z][A-Z0-9 \-:,&]{5,70}", following[0]):
        return True
    return any(_APPENDIX_NEXT.match(l) for l in following)


# The ways a numbered entry can open its line: "[12] ...", "(12) ...", "12. ..."
# and the bare "12 Surname, A." that revtex prints under superscript citations.
_LIST_MARKERS = (
    re.compile(r"^\s*\[(\d{1,3})\]\s*\S"),
    re.compile(r"^\s*\((\d{1,3})\)\s+\S"),
    re.compile(r"^\s*(\d{1,3})[.)]\s+\S"),
    re.compile(r"^\s*(\d{1,3})\s+(?:[a-z]{2,3}\s+){0,2}[A-Z][\w'’`\-]+(?:\s+[A-Z][\w'’`\-]+)?,\s"),
)


def _guess_reference_start(lines: list[str], earliest: int) -> int | None:
    """Find an unlabelled bibliography: the line where its entry 1 starts.

    Entries run over several lines, so the list is not a block of consecutive
    marker lines. It is a count: 1, then 2 a few lines later, then 3, in one
    marker style. Body text has numbered lists too ("1. Load the atoms"), so
    the longest such count wins, and it has to reach at least five.
    """
    best_start, best_run = None, 4
    for marker in _LIST_MARKERS:
        found = [(i, int(m.group(1))) for i in range(earliest, len(lines))
                 if (m := marker.match(lines[i]))]
        for pos, (start, number) in enumerate(found):
            if number != 1:
                continue
            run, at = 1, start
            for idx, n in found[pos + 1:]:
                if idx - at > 60:
                    break
                if n == run + 1:
                    run, at = run + 1, idx
            if run > best_run or (run == best_run and best_start is not None and start > best_start):
                best_start, best_run = start, run
    return best_start


def _page_for_line_offset(pages: list[Page], char_offset: int) -> int:
    running = 0
    for page in pages:
        running += len(page.text) + 1
        if char_offset < running:
            return page.number
    return pages[-1].number if pages else 1
