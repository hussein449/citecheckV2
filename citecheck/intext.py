"""Locate in-text citation markers and the exact claim each one supports.

Given body prose this finds every "[1]", "[3-5]" or "(Smith et al., 2019)"
marker, expands ranges, and captures the sentence the marker sits in — that
sentence is the claim we later test the cited source against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

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

    def to_dict(self) -> dict:
        return asdict(self)


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


def split_sentences(text: str) -> list[tuple[int, str]]:
    """Split prose into (offset, sentence) pairs, protecting abbreviations."""
    flat = re.sub(r"\s*\n\s*", " ", text)
    sentences: list[tuple[int, str]] = []
    start = 0
    for match in _SENT_END.finditer(flat):
        if not _is_real_boundary(flat, match.start(1)):
            continue
        end = match.end(1)
        chunk = flat[start:end].strip()
        if chunk:
            sentences.append((start, chunk))
        start = match.end()
    tail = flat[start:].strip()
    if tail:
        sentences.append((start, tail))
    return sentences


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
        if part.isdigit():
            keys.append(str(int(part)))
    return keys


def _author_year_keys(group: str) -> list[tuple[str, str]]:
    """Split a possibly multi-citation parenthetical into (key, label) pairs."""
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"\s*;\s*", group):
        chunk = chunk.strip()
        year = re.search(r"((?:19|20)\d{2})[a-z]?", chunk)
        name = re.match(rf"({_NAME_WORD})", chunk)
        if year and name:
            out.append((normalise_key(name.group(1), year.group(1)), chunk))
    return out


def normalise_key(surname: str, year: str) -> str:
    return f"{re.sub(r'[^a-z]', '', surname.lower())}{year}"


def _window(sentence: str, marker_pos: int, width: int = 220) -> str:
    lo = max(0, marker_pos - width // 2)
    hi = min(len(sentence), marker_pos + width // 2)
    snippet = sentence[lo:hi].strip()
    if lo > 0:
        snippet = "… " + snippet
    if hi < len(sentence):
        snippet = snippet + " …"
    return snippet


def extract_citations(body_text: str, page_of_offset=None) -> list[Citation]:
    """Find every in-text citation in *body_text*.

    ``page_of_offset`` maps a character offset back to a page number; when it is
    omitted every citation is reported on page 1.
    """
    citations: list[Citation] = []
    numeric_hits = 0

    for sent_offset, sentence in split_sentences(body_text):
        page = page_of_offset(sent_offset) if page_of_offset else 1

        for match in _NUMERIC.finditer(sentence):
            keys = _expand_numeric(match.group(1))
            numeric_hits += len(keys)
            for key in keys:
                citations.append(
                    Citation(
                        key=key,
                        label=match.group(0),
                        style="numeric",
                        sentence=sentence,
                        line=_window(sentence, match.start()),
                        page=page,
                        char_offset=sent_offset + match.start(),
                    )
                )

        for match in _AUTHOR_YEAR.finditer(sentence):
            for key, label in _author_year_keys(match.group(1)):
                citations.append(
                    Citation(
                        key=key,
                        label=f"({label})",
                        style="author-year",
                        sentence=sentence,
                        line=_window(sentence, match.start()),
                        page=page,
                        char_offset=sent_offset + match.start(),
                    )
                )

        for match in _NARRATIVE.finditer(sentence):
            key = normalise_key(match.group(1).split()[0], match.group(2))
            citations.append(
                Citation(
                    key=key,
                    label=match.group(0),
                    style="author-year",
                    sentence=sentence,
                    line=_window(sentence, match.start()),
                    page=page,
                    char_offset=sent_offset + match.start(),
                )
            )

    # A paper uses one scheme. If numeric markers dominate, author-year hits are
    # almost always false positives (year ranges, parenthetical asides).
    if numeric_hits >= 5:
        citations = [c for c in citations if c.style == "numeric"]

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
