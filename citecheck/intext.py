"""Locate in-text citation markers and the exact claim each one supports.

Given body prose this finds every "[1]", "[3-5]" or "(Smith et al., 2019)"
marker, expands ranges, and captures the clause the marker actually governs.

That clause — not the whole sentence — is what the cited source is later tested
against, and the distinction decides a great many verdicts. A sentence like

    parameters such as soil moisture [37], field temperature [38] and
    crop yield [39] can all be predicted

makes three separate claims. Handing the whole sentence to all three sources
asks each of them to support the other two's content as well, and each comes
back reported as only weakly supporting a claim it was never cited for.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, asdict, field

# Numeric styles: [1] [1,2] [1, 2] [1-3] [1–3] [1,3-5]
_NUMERIC = re.compile(r"\[\s*(\d{1,3}(?:\s*[-–—,;]\s*\d{1,3})*)\s*\]")

# Surnames routinely carry non-ASCII letters (Eißfeldt, Osório, Muñoz), so the
# name classes have to be Unicode-aware. An ASCII-only class stops at the first
# such letter, which loses the marker entirely rather than merely truncating it.
_U = r"A-ZÀ-ÖØ-Þ"
_L = r"A-Za-zÀ-ÖØ-öø-ÿĀ-ſ"
_NAME_WORD = rf"[{_U}][{_L}'’\-]+"
# A surname printed as two words: "Betti Sorbelli", "Rojas Viloria".
_SURNAME = rf"{_NAME_WORD}(?:\s+{_NAME_WORD})?"
# "Smith" / "Smith et al." / "Smith & Jones". "et al." has to stand on its own:
# unlike "and"/"&" it is never followed by another surname, and requiring one
# there silently drops every multi-author citation in the paper.
_AUTHORS = rf"{_SURNAME}(?:\s+et\s+al\.?|\s*(?:and|&)\s*{_SURNAME})?"

# Author-year styles: (Smith, 2019) (Smith & Jones 2019; Doe et al., 2020)
_AUTHOR_YEAR = re.compile(
    rf"\(\s*({_AUTHORS}(?:\s*,\s*)?\s*(?:19|20)\d{{2}}[a-z]?"
    rf"(?:\s*;\s*[^()]{{3,80}}?(?:19|20)\d{{2}}[a-z]?)*)\s*\)"
)

# Narrative author-year: Smith et al. (2019) showed ...
# The surname stays one word here. The parenthetical form is anchored by its
# opening bracket, but this one is not, so allowing a second word would let an
# ordinary preceding word be read as part of the name ("However Smith (2019)").
_NARRATIVE = re.compile(
    rf"\b({_NAME_WORD}(?:\s+et\s+al\.?|\s*(?:and|&)\s*{_NAME_WORD})?)"
    rf"\s*\(\s*((?:19|20)\d{{2}}[a-z]?)\s*\)"
)

# Capitalised words that open an author-year marker but are never surnames.
# "Table 2 (2019)", "Figure 4 (2021)" and "Since (2019) the fleet has grown"
# all satisfy the narrative pattern, and each invents a reference key that
# matches no bibliography entry and is then reported as an orphan marker.
_NOT_A_SURNAME = {
    "table", "tables", "figure", "figures", "fig", "figs", "section", "sections",
    "equation", "equations", "eq", "eqs", "appendix", "chapter", "part", "phase",
    "step", "stage", "case", "class", "group", "level", "type", "volume", "issue",
    "panel", "box", "chart", "column", "row", "model", "algorithm", "scenario",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "spring", "summer", "autumn",
    "winter", "since", "during", "between", "from", "until", "after", "before",
    "the", "this", "these", "those", "however", "moreover", "although", "while",
}

# Abbreviations that must not end a sentence. Checked in Python rather than in
# the pattern, because `re` only allows fixed-width lookbehind.
_ABBREV = {
    "et al", "e.g", "i.e", "cf", "vs", "fig", "figs", "eq", "eqs", "ref", "refs",
    "sec", "tab", "no", "vol", "pp", "approx", "dr", "prof", "mr", "mrs", "ms",
    "st", "jr", "sr", "ca", "ibid", "viz", "al", "inc", "ltd", "est", "resp",
}
# Candidate boundary: terminator, optional closing quotes/brackets, space, then
# something that can start a sentence.
_SENT_END = re.compile(r"([.!?])[\"'’”\)\]]*\s+(?=[A-Z\"'“\(\[])")
# Trailing token immediately before the terminator.
_LAST_TOKEN = re.compile(r"([A-Za-z][A-Za-z.]*)\s*$")


@dataclass
class Citation:
    """One occurrence of one reference key in the body text."""

    key: str            # normalised reference key, e.g. "1" or "smith2019"
    label: str          # marker as printed, e.g. "[1]" or "(Smith, 2019)"
    style: str          # "numeric" | "author-year"
    sentence: str       # full sentence containing the marker
    line: str           # tighter window around the marker
    page: int
    char_offset: int
    # The part of the sentence this marker actually governs. Equal to the whole
    # sentence when the marker is the only one in it.
    claim: str = ""
    # How many references were cited together at this point. A group citation
    # asks each source for part of the claim, not all of it, and a judge that is
    # not told so reports five of the six as failing to support it.
    group_size: int = 1
    # False for table rows, headings and figure legends — text where a "[12]" is
    # a row label rather than an assertion about a source.
    prose: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Sentence splitting
# --------------------------------------------------------------------------- #

class _Flat:
    """Newline-collapsed text that can still map an offset back to the original.

    Every offset this module reports ends up at ``ParsedPDF.page_of_offset``,
    which counts characters through the *original* page text. Measuring those
    offsets against the flattened copy instead makes every reported page a
    little early, and the error compounds with every line break above it — so
    by the end of a long paper the page a citation is said to be on is not the
    page it is on, and the report's deep links land in the wrong place.
    """

    def __init__(self, text: str) -> None:
        parts: list[str] = []
        self._flat_starts: list[int] = [0]
        self._orig_starts: list[int] = [0]
        length = 0
        cursor = 0
        for gap in re.finditer(r"\s*\n\s*", text):
            chunk = text[cursor : gap.start()]
            if chunk:
                self._flat_starts.append(length)
                self._orig_starts.append(cursor)
                parts.append(chunk)
                length += len(chunk)
            parts.append(" ")
            length += 1
            cursor = gap.end()
        tail = text[cursor:]
        if tail:
            self._flat_starts.append(length)
            self._orig_starts.append(cursor)
            parts.append(tail)
        self.text = "".join(parts)

    def origin(self, offset: int) -> int:
        """Where *offset* in the flattened copy came from in the original text."""
        index = max(0, bisect_right(self._flat_starts, offset) - 1)
        return self._orig_starts[index] + (offset - self._flat_starts[index])


def _is_real_boundary(flat: str, dot_index: int) -> bool:
    """Reject a candidate boundary that is really an abbreviation or initial."""
    token_match = _LAST_TOKEN.search(flat[:dot_index])
    if not token_match:
        return True
    token = token_match.group(1).rstrip(".").lower()
    if token in _ABBREV:
        return False
    # A lone capital is an initial ("J. Doe"), not the end of a sentence.
    raw = token_match.group(1).rstrip(".")
    return not (len(raw) == 1 and raw.isupper())


def _sentences_of(flat_text: str) -> list[tuple[int, str]]:
    """(offset, sentence) pairs indexing the *flattened* text."""
    sentences: list[tuple[int, str]] = []
    start = 0
    for match in _SENT_END.finditer(flat_text):
        if not _is_real_boundary(flat_text, match.start(1)):
            continue
        end = match.end(1)
        chunk = flat_text[start:end].strip()
        if chunk:
            sentences.append((start, chunk))
        start = match.end()
    tail = flat_text[start:].strip()
    if tail:
        sentences.append((start, tail))
    return sentences


def split_sentences(text: str) -> list[tuple[int, str]]:
    """Split prose into (offset, sentence) pairs, protecting abbreviations.

    Offsets index *text* as it was given, not the newline-collapsed copy the
    splitter works on internally.
    """
    flat = _Flat(text)
    return [(flat.origin(offset), sentence) for offset, sentence in _sentences_of(flat.text)]


# --------------------------------------------------------------------------- #
# Markers
# --------------------------------------------------------------------------- #

@dataclass
class _Marker:
    """One printed marker, with every reference key it stands for."""

    start: int
    end: int
    style: str
    keys: list[tuple[str, str]] = field(default_factory=list)   # (key, label)


def _expand_numeric(group: str) -> list[str]:
    """"1,3-5" -> ["1","3","4","5"]."""
    keys: list[str] = []
    for part in re.split(r"\s*[,;]\s*", group):
        part = part.strip()
        if not part:
            continue
        span = re.match(r"^(\d{1,3})\s*[-–—]\s*(\d{1,3})$", part)
        if span:
            lo, hi = int(span.group(1)), int(span.group(2))
            if 0 < hi - lo <= 60:
                keys.extend(str(n) for n in range(lo, hi + 1))
                continue
            part = span.group(1)
        # No bibliography has an entry zero, so the "0" in "[0]" or in a "[0,1]"
        # value range is arithmetic, not a citation.
        if part.isdigit() and int(part) > 0:
            keys.append(str(int(part)))
    return keys


def _author_year_keys(group: str) -> list[tuple[str, str]]:
    """Split a possibly multi-citation parenthetical into (key, label) pairs."""
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"\s*;\s*", group):
        chunk = chunk.strip()
        year = re.search(r"((?:19|20)\d{2})[a-z]?", chunk)
        name = re.match(rf"({_NAME_WORD})", chunk)
        if not (year and name):
            continue
        if name.group(1).lower() in _NOT_A_SURNAME:
            continue
        out.append((normalise_key(name.group(1), year.group(1)), chunk))
    return out


def normalise_key(surname: str, year: str) -> str:
    return f"{re.sub(r'[^a-z]', '', surname.lower())}{year}"


def _markers_in(sentence: str) -> list[_Marker]:
    """Every citation marker in one sentence, in print order and non-overlapping."""
    found: list[_Marker] = []

    for match in _NUMERIC.finditer(sentence):
        keys = _expand_numeric(match.group(1))
        if keys:
            found.append(
                _Marker(match.start(), match.end(), "numeric",
                        [(key, match.group(0)) for key in keys])
            )

    for match in _AUTHOR_YEAR.finditer(sentence):
        pairs = _author_year_keys(match.group(1))
        if pairs:
            found.append(
                _Marker(match.start(), match.end(), "author-year",
                        [(key, f"({label})") for key, label in pairs])
            )

    for match in _NARRATIVE.finditer(sentence):
        surname = match.group(1).split()[0]
        if surname.lower() in _NOT_A_SURNAME:
            continue
        found.append(
            _Marker(match.start(), match.end(), "author-year",
                    [(normalise_key(surname, match.group(2)), match.group(0))])
        )

    # The narrative and parenthetical patterns can both claim the same brackets.
    # Keeping both double-counts the citation and, worse, splits the clause
    # around a boundary that is really one marker.
    found.sort(key=lambda m: (m.start, -m.end))
    kept: list[_Marker] = []
    for marker in found:
        if kept and marker.start < kept[-1].end:
            continue
        kept.append(marker)
    return kept


# --------------------------------------------------------------------------- #
# Claim scoping
# --------------------------------------------------------------------------- #

# Words carrying no subject matter, used only to decide whether a clause has
# enough content to stand on its own. Deliberately tiny — this is a length
# test, not the scoring vocabulary, which lives in `match`.
_THIN = {
    "and", "or", "the", "a", "an", "of", "in", "on", "for", "to", "as", "at",
    "by", "with", "such", "also", "well", "are", "was", "were", "been", "being",
    "which", "that", "this", "these", "those", "its", "their", "from", "into",
    "can", "may", "has", "have", "had", "not", "but", "while", "both",
}
_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]{1,}")
# Punctuation and conjunctions left dangling when a clause is cut away from the
# one before it.
_CLAUSE_LEAD = re.compile(r"^[\s,;:.…)\]}\-–—]+")
_CLAUSE_CONJ = re.compile(r"^(?:and|or|as\s+well\s+as|along\s+with|together\s+with)\s+", re.I)
# Bracketed spans, removed before judging whether a sentence reads as prose: a
# sentence citing eight sources is still prose, and its brackets should not be
# counted against it.
_BRACKETED = re.compile(r"\[[^\]]{0,80}\]|\([^)]{0,120}\)")


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text) if w.lower() not in _THIN and len(w) > 2]


# A table has no sentence terminator anywhere in it, so the whole thing arrives
# as one "sentence" carrying one marker per row. No sentence anyone wrote cites
# this many sources at once — the busiest real one in a paper runs to four or
# five — so the count alone separates the two.
_TABLE_MARKERS = 8


def _clause_for(
    sentence: str,
    markers: list[_Marker],
    index: int,
    leading: bool = False,
) -> str:
    """The stretch of *sentence* that marker *index* is answerable for.

    A clause runs from the end of the previous marker through this one; the last
    marker in a sentence also takes the tail after it, which is usually the
    predicate the whole list depends on. A clause too thin to mean anything on
    its own falls back to the full sentence rather than being judged as a
    fragment — half a claim is worse evidence than a shared one.

    ``leading`` reverses that direction for a table whose reference number is
    printed in the first column. There the marker introduces the row instead of
    closing a clause, so reading backwards hands every row to the number below
    it and the whole table is judged one row out of step. Reading forwards is
    also why the thin-clause fallback is skipped here: a sparse row is still
    that row, and widening it would substitute the entire table.
    """
    if len(markers) < 2:
        return sentence

    if leading:
        start = markers[index].start
        end = markers[index + 1].start if index + 1 < len(markers) else len(sentence)
        return sentence[start:end].strip(" ,;:")

    start = markers[index - 1].end if index else 0
    end = markers[index].end if index + 1 < len(markers) else len(sentence)

    clause = _CLAUSE_LEAD.sub("", sentence[start:end])
    clause = _CLAUSE_CONJ.sub("", clause).strip(" ,;:")
    if len(_content_words(clause)) < 4:
        return sentence
    return clause


def _row_labels(
    body: str,
    flat: "_Flat",
    sent_offset: int,
    sentence: str,
    markers: list[_Marker],
) -> bool:
    """Do these markers open their lines, the way a table's row labels do?

    Flattening a table loses the one thing that says which way a marker points:
    on the page a row label sits alone at the start of its line, while a prose
    marker sits mid-line at the end of the clause it closes. The original text
    still has the line breaks, so ask it.

    Asked per marker this would be noise — prose wraps, and a marker can land at
    a line start by accident — so it is a vote across the whole block. A marker
    whose mapped offset does not still hold its own text has drifted and simply
    does not vote.
    """
    if len(markers) < _TABLE_MARKERS:
        return False

    opens = decided = 0
    for marker in markers:
        label = sentence[marker.start:marker.end]
        at = flat.origin(sent_offset + marker.start)
        if body[at:at + len(label)] != label:
            continue
        decided += 1
        before = at - 1
        while before >= 0 and body[before] in " \t":
            before -= 1
        if before < 0 or body[before] == "\n":
            opens += 1

    return decided >= len(markers) * 0.5 and opens >= decided * 0.7


def _is_prose(sentence: str, marker_count: int = 0) -> bool:
    """Does this read as an assertion, or as a table row / heading / legend?

    Non-prose markers are kept — a reference cited only from a comparison table
    is still a reference worth checking — but a table row asserts nothing, so
    where a reference is cited from both, the prose sentences are what get
    judged. Scoring a source against "Ref. Method Year Dataset" yields a "weak"
    verdict about nothing at all.
    """
    # Both tests below are ratios, so a table large enough dilutes each of them
    # past its threshold and the whole table reads as one prose sentence: the
    # column headings stop outweighing the lowercase cell text, and the digit
    # budget grows with the very length that should have condemned it. A row
    # count does not dilute.
    if marker_count >= _TABLE_MARKERS:
        return False
    bare = _BRACKETED.sub(" ", sentence)
    words = _WORD.findall(bare)
    if len(words) < 6:
        return False
    # Running text is mostly lowercase. A row of title-cased column values
    # ("Hub Location Model Heuristic Genetic Algorithm") is not.
    if sum(1 for w in words if w[0].islower()) < len(words) * 0.4:
        return False
    noise = len(re.findall(r"[\d|=+/\\]", bare))
    return noise <= max(8, len(bare) * 0.12)


def _window(sentence: str, marker_pos: int, width: int = 220) -> str:
    lo = max(0, marker_pos - width // 2)
    hi = min(len(sentence), marker_pos + width // 2)
    snippet = sentence[lo:hi].strip()
    if lo > 0:
        snippet = "… " + snippet
    if hi < len(sentence):
        snippet = snippet + " …"
    return snippet


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def extract_citations(body_text: str, page_of_offset=None) -> list[Citation]:
    """Find every in-text citation in *body_text*.

    ``page_of_offset`` maps a character offset back to a page number; when it is
    omitted every citation is reported on page 1.
    """
    flat = _Flat(body_text)
    citations: list[Citation] = []
    numeric_hits = 0
    author_year_hits = 0

    for sent_offset, sentence in _sentences_of(flat.text):
        markers = _markers_in(sentence)
        if not markers:
            continue

        page = page_of_offset(flat.origin(sent_offset)) if page_of_offset else 1
        prose = _is_prose(sentence, len(markers))
        leading = _row_labels(body_text, flat, sent_offset, sentence, markers)

        for index, marker in enumerate(markers):
            claim = _clause_for(sentence, markers, index, leading=leading)
            # A whole flattened table is no one's context. Where the row is the
            # claim it is also all the context there is, and quoting the rest of
            # the table back at the reader — or at the judge — only invites a
            # verdict about somebody else's row.
            context = claim if leading else sentence
            window = _window(context, 0 if leading else marker.start)
            if marker.style == "numeric":
                numeric_hits += len(marker.keys)
            else:
                author_year_hits += len(marker.keys)

            for key, label in marker.keys:
                citations.append(
                    Citation(
                        key=key,
                        label=label,
                        style=marker.style,
                        sentence=context,
                        claim=claim,
                        line=window,
                        page=page,
                        char_offset=sent_offset + marker.start,
                        group_size=len(marker.keys),
                        prose=prose,
                    )
                )

    # A paper uses one scheme, and each pattern misfires on the other's papers:
    # "(2019)" year asides read as author-year in a numeric paper, and a summary
    # table with a "Study ID" column of "[126]" cells reads as numeric in an
    # author-year one. So the majority style wins rather than numeric winning
    # outright — the latter threw away every real citation in an author-year
    # review whose tables happened to number their rows, leaving nothing to
    # check and a bibliography that matched none of the surviving markers.
    if numeric_hits >= 5 or author_year_hits >= 5:
        winner = "numeric" if numeric_hits >= author_year_hits else "author-year"
        citations = [c for c in citations if c.style == winner]

    return _dedupe(citations)


def _dedupe(citations: list[Citation]) -> list[Citation]:
    seen: set[tuple[str, int]] = set()
    out: list[Citation] = []
    for cite in sorted(citations, key=lambda c: c.char_offset):
        token = (cite.key, cite.char_offset)
        if token in seen:
            continue
        seen.add(token)
        out.append(cite)
    return out


def group_by_reference(citations: list[Citation]) -> dict[str, list[Citation]]:
    grouped: dict[str, list[Citation]] = {}
    for cite in citations:
        grouped.setdefault(cite.key, []).append(cite)
    return grouped
