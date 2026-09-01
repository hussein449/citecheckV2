"""Parse the bibliography block into individual, structured entries.

Each entry ends up with whatever identifiers we can salvage — DOI, arXiv id,
bare URL, title, authors, year — which is what makes the source retrievable in
the next stage.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field, asdict

from .intext import normalise_key

_ENTRY_MARKER = re.compile(r"(?m)^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})[.)])\s+(?=\S)")

_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]*[A-Za-z0-9]", re.IGNORECASE)
_ARXIV = re.compile(r"arXiv[:\s]*\s*(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s,;>\]\)]+", re.IGNORECASE)
_YEAR = re.compile(r"\b((?:19|20)\d{2})[a-z]?\b")
_PMID = re.compile(r"\bPMID[:\s]+(\d{6,9})", re.IGNORECASE)

# Publisher furniture that trails the bibliography and is not a reference.
_BOILERPLATE = re.compile(
    r"(publisher'?[’']?s?\s+note|remains\s+neutral\s+with\s+regard|"
    r"springer\s+nature\s+remains|open\s+access\s+this\s+article\s+is\s+licensed|"
    r"creative\s+commons\s+attribution|all\s+rights\s+reserved)",
    re.IGNORECASE,
)


@dataclass
class Reference:
    key: str
    raw: str
    number: int | None = None
    authors: str = ""
    title: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    arxiv: str = ""
    pmid: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def display(self) -> str:
        label = f"[{self.number}]" if self.number is not None else f"({self.key})"
        return f"{label} {self.title or self.raw[:110]}"


def parse_references(refs_text: str) -> list[Reference]:
    """Split the bibliography into entries and pull identifiers from each."""
    if not refs_text.strip():
        return []

    numbered = _split_numbered(refs_text)
    if len(numbered) >= 2:
        return _build_all(numbered)

    # Without entry numbers there is no unambiguous boundary marker, and each
    # way of guessing one fails on layouts the other handles: splitting on
    # entry-initial surnames misses authors whose given name is spelled out,
    # while splitting on blank lines finds nothing in a bibliography set solid.
    # A missed boundary silently welds two entries into one, so rather than
    # committing to either rule, run both and keep whichever recovers more
    # complete entries. Over-splitting does not win by default: a fragment with
    # no author or no year builds no reference and so does not count.
    return max(
        (
            _build_all(split(refs_text))
            for split in (_split_author_year, _split_blocks, _split_year_anchored)
        ),
        key=len,
    )


def _build_all(chunks: list[tuple[int | None, str]]) -> list[Reference]:
    references: list[Reference] = []
    for number, raw in chunks:
        raw = _tidy(raw)

        # Publisher furniture ("Publisher's Note: Springer Nature remains
        # neutral…") trails the last entry on the same line, with no number of
        # its own. Trim it off the tail rather than discarding the whole chunk —
        # doing the latter silently deletes a real, cited reference.
        boilerplate = _BOILERPLATE.search(raw)
        if boilerplate:
            raw = raw[: boilerplate.start()].strip(" .;,")

        if len(raw) < 12:
            continue
        ref = _build_reference(number, raw)
        if ref:
            references.append(ref)
    return references


# A DOI wrapped across a line becomes "10.1109/ICCNC.2016. 7440563" once the
# newline collapses to a space. Rejoining is only safe when the break landed
# right after a separator character, which a complete DOI never ends with.
_SPLIT_IDENTIFIER = re.compile(
    r"(\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]*[-._/])\s+([A-Za-z0-9][-._;()/:A-Za-z0-9]*)"
)
_SPLIT_URL = re.compile(r"(https?://[^\s]*[-._/])\s+([A-Za-z0-9][^\s]*)")


def _tidy(text: str) -> str:
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = _SPLIT_URL.sub(r"\1\2", text)
    text = _SPLIT_IDENTIFIER.sub(r"\1\2", text)
    return text.strip(" .;,")


def _split_numbered(refs_text: str) -> list[tuple[int | None, str]]:
    matches = list(_ENTRY_MARKER.finditer(refs_text))
    if len(matches) < 2:
        return []

    # Entry numbers should mostly ascend; a stray "2020." at line start would
    # break that and means we picked the wrong pattern. The fraction is of
    # adjacent *pairs*, of which there is one fewer than there are numbers —
    # measuring it against the count instead put a perfectly ordered two-entry
    # list below the bar, so short numbered bibliographies were handed to the
    # author-year splitters, which have no numbers to find.
    numbers = [int(m.group(1) or m.group(2) or m.group(3)) for m in matches]
    ascending = sum(1 for a, b in zip(numbers, numbers[1:]) if b > a)
    if ascending < (len(numbers) - 1) * 0.6:
        return []

    matches, numbers = _drop_strays(refs_text, matches, numbers)
    if len(matches) < 2:
        return []

    chunks: list[tuple[int | None, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(refs_text)
        chunks.append((numbers[idx], refs_text[start:end]))

    # Everything above the first marker is normally just the "References"
    # heading. When the first marker found is not [1] it is the opening entries
    # instead, and they would be dropped along with the heading.
    head = refs_text[: matches[0].start()]
    if numbers[0] > 1 and head.strip():
        opening = _recover(0, head, range(1, numbers[0]))
        chunks = [chunk for chunk in opening if chunk[0]] + chunks

    return _fill_gaps(chunks)


# A number that ends the previous entry rather than opening the next one is
# preceded by the comma that introduced it: "Royal Society Open Science, 11."
_CONTINUES_ENTRY = (",", ";", ":")


def _char_before(text: str, at: int) -> str:
    idx = at - 1
    while idx >= 0 and text[idx].isspace():
        idx -= 1
    return text[idx] if idx >= 0 else ""


def _drop_strays(
    refs_text: str,
    matches: list[re.Match],
    numbers: list[int],
) -> tuple[list[re.Match], list[int]]:
    """Discard line-initial numbers that are not entry numbers.

    A volume or page number wrapped onto its own line is the same shape as an
    entry marker, and one landing between two entries silently becomes the
    boundary for its own value — welding every entry from there to the next
    match into a single reference that carries the wrong title. Two things tell
    it apart, and both have to fail before a marker is dropped: it follows the
    comma that introduced it, and it does not continue the count.

    Whatever survives that is then reduced to its longest ascending run. Entry
    numbers only ever ascend, so a number that goes backwards is furniture no
    matter what precedes it — and picking the *longest* run rather than walking
    left to right keeps a stray first match from evicting the real list behind
    it.
    """
    kept: list[int] = []
    last: int | None = None
    for idx, (match, number) in enumerate(zip(matches, numbers)):
        interrupts = last is not None and number != last + 1
        if interrupts and _char_before(refs_text, match.start()) in _CONTINUES_ENTRY:
            continue
        kept.append(idx)
        last = number

    run = _longest_ascending([numbers[i] for i in kept])
    kept = [kept[i] for i in run]
    return [matches[i] for i in kept], [numbers[i] for i in kept]


def _longest_ascending(numbers: list[int]) -> list[int]:
    """Positions of the longest strictly ascending subsequence of *numbers*."""
    tail_values: list[int] = []
    tail_at: list[int] = []
    came_from = [-1] * len(numbers)
    for idx, number in enumerate(numbers):
        pos = bisect_left(tail_values, number)
        if pos:
            came_from[idx] = tail_at[pos - 1]
        if pos == len(tail_values):
            tail_values.append(number)
            tail_at.append(idx)
        else:
            tail_values[pos] = number
            tail_at[pos] = idx

    out: list[int] = []
    idx = tail_at[-1] if tail_at else -1
    while idx >= 0:
        out.append(idx)
        idx = came_from[idx]
    return out[::-1]


def _fill_gaps(chunks: list[tuple[int | None, str]]) -> list[tuple[int | None, str]]:
    """Recover entries the strict marker pattern walked past.

    A separator the pattern does not accept — a tab, a zero-width space, no
    separator at all — hides an entry inside its predecessor's chunk, and the
    two are then welded into one reference carrying the earlier one's title and
    identifiers. Every citation of the later entry is checked against the wrong
    work, and every entry in between vanishes from the bibliography entirely.

    A gap in the numbering says exactly which entries went missing and which
    chunk they must be inside, which is enough to go back and cut them out.
    """
    filled: list[tuple[int | None, str]] = []
    for idx, (number, raw) in enumerate(chunks):
        following = chunks[idx + 1][0] if idx + 1 < len(chunks) else None
        if number is None or following is None or following <= number + 1:
            filled.append((number, raw))
            continue
        filled.extend(_recover(number, raw, range(number + 1, following)))
    return filled


def _recover(
    number: int,
    raw: str,
    missing: range,
) -> list[tuple[int | None, str]]:
    """Cut *raw* at whichever of the *missing* entry numbers it still contains."""
    cuts: list[tuple[int, int, int]] = []
    at = 0
    for want in missing:
        pattern = re.compile(rf"(?m)^[ \t]*{want}[.)][ \t]*")
        for found in pattern.finditer(raw, at):
            if _char_before(raw, found.start()) in _CONTINUES_ENTRY:
                continue
            cuts.append((found.start(), found.end(), want))
            at = found.end()
            break

    if not cuts:
        return [(number, raw)]

    pieces = [(number, raw[: cuts[0][0]])]
    for idx, (_, end, want) in enumerate(cuts):
        stop = cuts[idx + 1][0] if idx + 1 < len(cuts) else len(raw)
        pieces.append((want, raw[end:stop]))
    return pieces


def _split_author_year(refs_text: str) -> list[tuple[int | None, str]]:
    """Split an unnumbered bibliography on entry-initial surnames."""
    lines = [l for l in refs_text.splitlines()]
    # Any author order can open an entry: "Agatz, N., …", "N Agatz, …", or
    # "Bosona, Tesfaye. …" — Chicago and MLA spell the given name out in full.
    # Recognising only the first form leaves this function returning a single
    # chunk for a whole initials-first bibliography, which then falls through to
    # blank-line splitting and depends on the PDF having blank lines to find.
    # A wrapped continuation line cannot match any of these: a venue reads as
    # "Aviation, 26(1)", whose comma is followed by a digit rather than a name.
    starter = re.compile(
        rf"^\s*(?:{_NAME}\s*,\s*(?:[{_U}]\.\s*)+"
        rf"|{_INITIALS_HEAD}{_NAME}\s*[,(]"
        rf"|{_NAME}\s*,\s+{_NAME}\s*[,.])"
    )
    chunks: list[tuple[int | None, str]] = []
    current: list[str] = []
    for line in lines:
        if starter.match(line) and current:
            chunks.append((None, " ".join(current)))
            current = [line]
        elif line.strip():
            current.append(line)
    if current:
        chunks.append((None, " ".join(current)))
    return chunks


def _split_blocks(refs_text: str) -> list[tuple[int | None, str]]:
    """Split on blank lines, which many PDFs leave between entries."""
    return [(None, b) for b in re.split(r"\n\s*\n", refs_text) if b.strip()]


def _split_year_anchored(refs_text: str) -> list[tuple[int | None, str]]:
    """Split where an author run followed by "(2019)" begins a new entry.

    Line starts and blank lines are both layout accidents that PDF text
    extraction routinely loses, which is how two entries end up welded into one
    chunk — the second then has no key and vanishes from the bibliography
    entirely. The authors-then-parenthesised-year pairing is not layout, it is
    the citation style itself, so it survives reflow and finds those boundaries
    wherever they landed.
    """
    flat = re.sub(r"\s*\n\s*", " ", refs_text)
    starts = [0] + [m.start() for m in _ENTRY_HEAD.finditer(flat)]
    return [
        (None, flat[start:end])
        for start, end in zip(starts, starts[1:] + [len(flat)])
    ]


def _build_reference(number: int | None, raw: str) -> Reference | None:
    doi = ""
    doi_match = _DOI.search(raw)
    if doi_match:
        doi = doi_match.group(0).rstrip(".,;")

    arxiv = ""
    arxiv_match = _ARXIV.search(raw)
    if arxiv_match:
        arxiv = arxiv_match.group(1)

    pmid = ""
    pmid_match = _PMID.search(raw)
    if pmid_match:
        pmid = pmid_match.group(1)

    url = ""
    url_match = _URL.search(raw)
    if url_match:
        url = url_match.group(0).rstrip(".,;)")
        if not doi and "doi.org/" in url.lower():
            doi = url.lower().split("doi.org/", 1)[1]

    year = ""
    year_match = _YEAR.search(raw)
    if year_match:
        year = year_match.group(1)

    authors, title, venue = _split_fields(raw)

    if number is not None:
        key = str(number)
    else:
        # Derived from the same phrase the alias index works from, so an entry's
        # own key is always one of the keys a marker can reach it by.
        words = _name_words(_entry_surname(authors or raw))
        if not (words and year):
            return None
        key = normalise_key("".join(words), year)

    return Reference(
        key=key,
        raw=raw,
        number=number,
        authors=authors,
        title=title,
        year=year,
        venue=venue,
        doi=doi,
        arxiv=arxiv,
        pmid=pmid,
        url=url,
    )


# Surnames routinely carry accents (Faiçal, Muñoz-Villamizar, Osório), so the
# name classes have to be Unicode-aware or the whole author run fails at the
# first such author and the title is lost.
_U = r"A-ZÀ-ÖØ-Þ"                                  # uppercase letters
# Latin-1 + Latin Extended-A, plus the spacing modifier letters that PDF text
# extraction leaves behind ("Přikryl" often arrives as "Pˇrikryl").
_L = r"A-Za-zÀ-ÖØ-öø-ÿĀ-ſˀ-˿"
# Name particles that carry a lowercase first letter: "de Freitas", "van der Berg".
_PARTICLE = (
    r"(?:(?:de|del|della|da|do|dos|das|di|du|van|von|der|den|ten|ter|la|le|"
    r"el|al|bin|ibn|abu|mac|mc|st)\s+)*"
)
# Typesetting turns the hyphen in a double-barrelled surname into any of these,
# and PDF extraction hands back whichever was printed: "Solano-Charris" arrives
# as "Solano–Charris" often enough that an ASCII-only hyphen splits the name.
_DASH = r"\-‐‑‒–—"
_NAME = rf"{_PARTICLE}[{_U}][{_L}'’{_DASH}]+"      # a capitalised name word

# One author's initials, however the publisher glues them together: "N", "N.",
# "KW", "AAR", "J.M.", "H.-Y.". Kept greedy-free of the surname by requiring the
# surname itself to start a fresh capitalised word.
_INITIAL = rf"[{_U}]{{1,3}}\.?(?:\s*-\s*[{_L}]\.?)?"
_INITIALS_HEAD = rf"(?:{_INITIAL}\s*){{1,4}}"

# One author's full name, however many parts it runs to: "Agatz", "Rashid
# Alyassi", "Seyed Mahdi Shavarani", "David C. Edwards", "Raïssa G. Mbiadou
# Saleu". Stopping at two words leaves the surname outside the phrase whenever a
# given name is spelled out in full, and the surname is the only part an
# author-year marker ever prints. A comma or "&" ends the run, so it cannot run
# on into the next author.
_NAME_RUN = rf"{_NAME}(?:\s+(?:[{_U}]\.|{_NAME})){{0,3}}"

# A run of "Surname, A. B.," entries — the author block of a numeric-style entry.
# Matching this explicitly avoids mistaking the final initial's period ("…,
# Polosukhin, I. Attention is all you need") for the end of a sentence.
_AUTHOR_RUN = re.compile(
    rf"^\s*((?:"
    rf"(?:and\s+|&\s+)?"                   # conjunction before the last author
    rf"{_NAME}"                            # surname
    rf"(?:\s+{_NAME})?"                    # optional second surname word
    # , A. B. (initials may be lowercase, and may be hyphenated: "H.-Y.")
    rf",\s*(?:[{_L}]\.(?:\s*-\s*[{_L}]\.?)?\s*)+"
    rf"(?:[,;]\s*)?"                       # separator before the next author
    rf")+)"
)
# The mirror-image convention: "J.M. Sullivan, Z. Xiaoning, H.-Y. Kim, Title…".
# Springer and IEEE both print references this way, so it is at least as common
# as the surname-first form above.
_INITIALS_RUN = re.compile(
    rf"^\s*((?:"
    rf"(?:and\s+|&\s+)?"
    rf"(?:[{_U}]\.(?:-[{_L}]\.?)?\s*)+"    # J.M. / H.-Y. / H.-u."
    rf"{_NAME}"                            # surname
    rf"(?:\s+{_NAME})?"                    # optional second surname word
    rf"\s*[,;]\s*"
    rf")+)"
)
# Vancouver, the house style of most medical and many Elsevier journals:
# "Kastner M, Tricco AC, Soobiah C, et al. Title. Venue 2012;1:28."
# The initials trail the surname and carry no periods at all, so neither run
# above matches and the whole author list is mistaken for the title — which is
# the field every "is this the work the author cited?" test measures against, so
# the reference then resolves to nothing and is reported as not found.
_VANCOUVER_AUTHOR = (
    rf"{_NAME}(?:\s+{_NAME})?\s+[{_U}]{{1,4}}"
    rf"(?:\s+(?:Jr|Sr|2nd|3rd|I{{1,3}}|IV))?"
)
_VANCOUVER_RUN = re.compile(
    rf"^\s*((?:{_VANCOUVER_AUTHOR}\s*,\s*)*"    # every author but the last
    rf"(?:{_VANCOUVER_AUTHOR}|et\s+al)"         # the last one, or "et al."
    rf"(?:\s*,\s*(?:editors?|eds?))?\.)"        # edited books name their editors
)
_LEADING_ETAL = re.compile(r"^(?:et\s+al\.?\s*,?\s*)+", re.IGNORECASE)

# A quoted title, in whichever quote characters the typesetter used. Long enough
# to be a title rather than a scare-quoted word, and it may not span the whole
# entry, which would mean the quotes were really wrapping the entire reference.
_QUOTED_TITLE = re.compile(r"[\"“”«]\s*([^\"“”«»]{15,300}?)\s*[\"“”»]\s*[,.]?")

# Harvard and Springer put the year between the authors and the title
# ("Bosona, T., 2020. Urban freight…"), where it would otherwise be read as the
# opening of the title and searched for as part of it.
_LEADING_YEAR = re.compile(r"^\(?\s*(?:19|20)\d{2}[a-z]?\s*\)?\s*[.,:;]?\s*")
# A part that carries no information but the year.
_YEAR_ONLY = re.compile(r"^\(?\s*(?:19|20)\d{2}[a-z]?\s*\)?[.,;]?$")


def _split_fields(raw: str) -> tuple[str, str, str]:
    """Best-effort author / title / venue split across common styles."""
    stripped = _URL.sub("", raw)
    stripped = re.sub(r"\bdoi:\s*\S+", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\barXiv:\s*\S+", "", stripped, flags=re.IGNORECASE).strip()

    # Style Q -- the title in quotes: IEEE, ACM, Chicago and MLA all print
    # 'A. Smith, "Drone routing," Proc. ICRA, 2019.' Quotation marks say
    # outright where the title begins and ends, which no amount of guessing at
    # sentence boundaries can match — without this the comma inside the quotes
    # reads as an author separator and the title comes back as a fragment of
    # the venue. Checked first because it is evidence rather than heuristic.
    quoted = _QUOTED_TITLE.search(stripped)
    if quoted:
        title = quoted.group(1).strip(" .,;")
        authors = stripped[: quoted.start()].strip(" .,;&")
        venue = stripped[quoted.end():].strip(" .,;")[:200]
        return authors, title, venue

    # Style A -- "Authors (2019). Title. Venue, 12(3), 45-67."
    # The remainder must be substantial: Springer entries end with a trailing
    # "(2020)." year, which otherwise matches here and swallows the whole entry.
    m = re.match(r"^(.{3,160}?)\(\s*(?:19|20)\d{2}[a-z]?\s*\)\.?\s*(.+)$", stripped)
    if m and len(m.group(2).strip()) >= 15:
        authors = m.group(1).strip(" .,;")
        rest = m.group(2).strip()
        title, venue = _first_sentence(rest)
        return authors, title, venue

    # Style B -- "Surname, A., Surname, B. Title. Venue, 2019."
    # Style B' -- "A. Surname, B. Surname, Title. Venue (2019)."
    for pattern in (_AUTHOR_RUN, _INITIALS_RUN, _VANCOUVER_RUN):
        run = pattern.match(stripped)
        if not (run and len(run.group(1)) >= 6):
            continue
        authors = run.group(1).strip(" .,;&")
        rest = _LEADING_ETAL.sub("", stripped[run.end():]).strip(" .,;:")
        rest = _LEADING_YEAR.sub("", rest)
        if len(rest) >= 8:
            title, venue = _first_sentence(rest)
            return authors, title, venue

    # Style C -- period-delimited, no recognisable author run.
    parts = [p.strip() for p in re.split(r"\.\s+", stripped) if p.strip()]
    if len(parts) >= 2:
        head = parts[0]
        # A long head with no initials is far more likely to be the title.
        if not re.search(r"[A-Z]\.", head) and len(head.split()) > 6:
            return "", head, ". ".join(parts[1:])[:200]
        # ACM prints the year as a sentence of its own between the authors and
        # the title ("Tesfaye Bosona. 2020. Urban freight…"), so the slot after
        # the authors is not always the title. Take the first part that is more
        # than a bare year, or the reference resolves on the string "2020".
        rest = [p for p in parts[1:] if not _YEAR_ONLY.match(p)]
        if rest:
            return head, rest[0], ". ".join(rest[1:])[:200]
        return head, parts[1], ". ".join(parts[2:])[:200]

    return "", _first_sentence(stripped)[0], ""


def _first_sentence(text: str) -> tuple[str, str]:
    # "?" and "!" close a title as surely as "." does, and review literature is
    # full of them ("What kind of review should I conduct?"). The question mark
    # is part of the title and stays; a full stop is punctuation and goes.
    m = re.match(r"^(.{5,300}?)([.?!])\s+(.*)$", text)
    if m:
        title = m.group(1).strip() + (m.group(2) if m.group(2) in "?!" else "")
        return title, m.group(3).strip()[:200]
    # Elsevier separates title from venue with a comma and no period at all
    # ("Urban freight logistics, Logistics 4 (2020) 24-38"), so without a
    # sentence break the whole tail — volume, pages and year — becomes the
    # title. The venue is the part carrying the numbers, so cut at the first
    # comma whose remainder has any; a comma inside a title rarely does.
    m = re.match(r"^(.{5,300}?),\s+([^,]*\d.*)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()[:200]
    return text.strip(" .")[:300], ""


def index_references(references: list[Reference]) -> dict[str, Reference]:
    """Map each entry onto its own key, dropping keys two entries share.

    Numbered entries are unique by construction, but an author-year key is not:
    two unrelated 2022 papers whose first author is named Li both key to
    "li2022". Keeping either one silently hands "(Li et al., 2022)" whichever
    entry happened to be parsed last, and the reader is then shown a verdict on
    a source the author never cited. Reporting the marker as unmatched is the
    honest outcome, and the same trade the alias index already makes.
    """
    counts: dict[str, int] = {}
    for ref in references:
        counts[ref.key] = counts.get(ref.key, 0) + 1
    return {ref.key: ref for ref in references if counts[ref.key] == 1}


# "Agatz, N." / "Betti Sorbelli, F." -- surname first, confirmed by the initials
# that follow it. Without that confirmation "Rashid Alyassi, Majid Khonji" would
# read as a two-word surname followed by a co-author.
_SURNAME_FIRST = re.compile(
    rf"^\s*({_NAME_RUN})\s*,\s*(?:[{_L}]\.|[{_U}]{{1,3}}\b(?!\s*[{_L}]))"
)
# "N Agatz" / "KW Chen" / "J.-M. Sullivan" -- initials first, surname after.
_INITIALS_FIRST = re.compile(rf"^\s*{_INITIALS_HEAD}({_NAME_RUN})")
# "Rashid Alyassi" -- a spelled-out given name, indistinguishable from a two-word
# surname here, so both words are kept and the alias index tries each.
_BARE_NAME = re.compile(rf"^\s*({_NAME_RUN})")


# An author's name in any of the orders above, for locating the *start* of an
# entry rather than reading the surname out of one. The leading initials are
# optional, which covers surname-first and spelled-out-given-name styles too.
_AUTHOR_HEAD = rf"(?:{_INITIALS_HEAD})?{_NAME_RUN}"
# What separates that first author from the year is more authors — letters,
# initials, separators, "et al." Admitting no digits, colons or brackets is what
# keeps a venue line ("Transportation Science, 12:3-4 (2019)") from reading as an
# author list, since volume and page numbers cannot survive the class.
_AUTHOR_LIST = rf"[{_L}\s.,;&'’{_DASH}]{{0,200}}"
# The period ending the previous entry, but never the period after an initial:
# "…, A. Karapetyan, S. Chau & C. Tseng (2017)" otherwise splits at every author,
# and each tail fragment still carries a surname and that year, so it builds a
# plausible-looking reference that no marker will ever point at.
_ENTRY_HEAD = re.compile(
    rf"(?<=[.])(?<![{_U}]\.)\s+"
    rf"(?={_AUTHOR_HEAD}{_AUTHOR_LIST}\(\s*(?:19|20)\d{{2}}[a-z]?\s*\))"
)


def _entry_surname(text: str) -> str:
    """The surname phrase an author-year marker would print for *text*.

    Bibliographies disagree on author order, and picking the first capitalised
    word regardless is wrong for the majority of them: it yields the given name
    for "Rashid Alyassi", and for "N Agatz" it matches nothing at all, because a
    lone initial is not a name word. The latter is the damaging case — an entry
    with no derivable key is dropped outright, so an entire initials-first
    bibliography parses down to only those entries that happened to spell their
    first author's given name in full.
    """
    for pattern in (_SURNAME_FIRST, _INITIALS_FIRST, _BARE_NAME):
        match = pattern.match(text)
        if match:
            return match.group(1).strip()
    return ""


def _leading_surname(ref: Reference) -> str:
    """The surname phrase an author-year marker would print for this entry."""
    return _entry_surname(ref.authors or ref.raw)


def _name_words(phrase: str) -> list[str]:
    """The name words in *phrase*, dropping initials.

    "David C. Edwards" is cited as "Edwards", never as "C", so an initial is
    noise in an alias — and a one-letter alias would collide across unrelated
    entries and get discarded as ambiguous, taking a real surname alias with it.
    """
    return [w for w in phrase.split() if len(w.rstrip(".")) > 1]


def _author_year_aliases(ref: Reference) -> set[str]:
    """Every author-year key that could plausibly point at *ref*."""
    if not ref.year:
        return set()
    words = _name_words(_leading_surname(ref))
    if not words:
        return set()
    # Which part of a name the marker prints is not recoverable from the
    # bibliography: a two-word surname may be cited by either word or by both
    # ("Betti Sorbelli, 2024" vs "Sorbelli, 2024"), and a spelled-out given name
    # ("Seyed Mahdi Shavarani") is cited by the surname buried at the end. So
    # index every word and the joined form, and let the marker pick.
    candidates = [normalise_key(w, ref.year) for w in words]
    candidates.append(normalise_key("".join(words), ref.year))
    # A word of pure punctuation normalises away to a bare year; that is not a
    # name and would match any entry published that year.
    return {key for key in candidates if key != ref.year}


def _alias_index(references: list[Reference]) -> dict[str, Reference]:
    """Map author-year keys onto the entries of a numbered bibliography.

    A paper may number its reference list while citing it as "(Bosona, 2020)" —
    Word's numbered-list styling does exactly this. The two key schemes then
    never meet on an exact lookup, so every marker in the paper is reported as
    an orphan and nothing gets checked at all.
    """
    candidates: dict[str, list[Reference]] = {}
    for ref in references:
        for alias in _author_year_aliases(ref):
            candidates.setdefault(alias, []).append(ref)
    # An alias two entries both answer to cannot be resolved from the marker
    # alone. Verifying a claim against the wrong source is a worse failure than
    # reporting the marker as an orphan, so drop the ambiguous ones.
    return {alias: found[0] for alias, found in candidates.items() if len(found) == 1}


def link_citations(
    grouped: dict[str, list],
    ref_index: dict[str, Reference],
) -> tuple[dict[str, Reference], list[str]]:
    """Return the references that are actually cited, plus unmatched keys."""
    matched: dict[str, Reference] = {}
    orphans: list[str] = []
    aliases = _alias_index(list(ref_index.values()))
    for key in grouped:
        ref = ref_index.get(key) or aliases.get(key)
        if ref:
            matched[key] = ref
        else:
            orphans.append(key)
    return matched, orphans
