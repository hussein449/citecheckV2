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
    first = _ENTRY_MARKER.search(refs_text)
    if len(numbered) >= 2 and not (first and len(refs_text[: first.start()].strip()) > 400):
        return _build_all(numbered)
    # Numbers that only start well into the text are something else: an
    # appendix listing its steps "1. ... 2. ..." after an unnumbered list. Taken
    # for the bibliography, four steps replaced thirty-seven references. Keep
    # them only if they are the better reading.
    by_number = _build_all(numbered) if len(numbered) >= 2 else []

    labelled = _split_alpha(refs_text)
    if len(labelled) >= 2:
        return _build_all(labelled)

    refs_text = _drop_trailing_blocks(refs_text)

    # Without entry numbers there is no unambiguous boundary marker, and each
    # way of guessing one fails on layouts the other handles: splitting on
    # entry-initial surnames misses authors whose given name is spelled out,
    # while splitting on blank lines finds nothing in a bibliography set solid.
    # A missed boundary silently welds two entries into one, so rather than
    # committing to either rule, run both and keep whichever recovers more
    # complete entries. Over-splitting does not win by default: a fragment with
    # no author or no year builds no reference and so does not count.
    return max(
        [
            _build_all(_trim_last(split(refs_text)))
            # A tie goes to the first, and the year-sentence split is the one
            # that keeps a wrapped author list in one piece: blank-line blocks
            # can find as many entries while keying one on its last author.
            for split in (
                _split_year_sentence, _split_year_terminated, _split_author_year,
                _split_blocks, _split_year_anchored,
            )
        ] + [by_number],
        key=len,
    )


def _drop_trailing_blocks(refs_text: str) -> str:
    """Cut the paragraphs after the last one that closes like a reference.

    An unnumbered list has nothing to say where it ends, and the splitters
    below flatten the text, so an appendix table that follows it becomes the
    tail of the last entry. Before flattening, the paragraph breaks are still
    there: a reference closes on its year, an identifier or a page range, and
    a page of anything else after the last paragraph that does is not the list.
    """
    blocks = re.split(r"(\n\s*\n)", refs_text)
    closes = re.compile(r"(?:(?:19|20)\d{2}[a-z]?\)?|\d{4,5}(?:v\d+)?|\d\s*[\-–]\s*\d+)\.?\s*$")
    last = max((i for i in range(0, len(blocks), 2) if closes.search(blocks[i].strip())), default=None)
    if last is None:
        return refs_text
    tail = "".join(blocks[last + 1:])
    # Only when the tail carries no year at all. Entries close in many ways
    # this pattern does not know (a DOI, a URL, an issue number), and four real
    # references at the end of a list were once cut for ending on a volume
    # number. Every reference has a year somewhere; an appendix table does not.
    if len(tail.strip()) > 300 and not _YEAR.search(tail):
        return "".join(blocks[: last + 1])
    return refs_text


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


# revtex under superscript citations labels entries with a bare number: "12
# Cross, M. C. & Hohenberg, P. C.". Only tried when the bracketed and dotted
# forms find nothing, since a wrapped page number looks the same.
_BARE_MARKER = re.compile(
    r"(?m)^\s*()()(\d{1,3})\s+"
    r"(?=(?:[a-z]{2,3}\s+){0,2}[A-ZÀ-ÖØ-Þ][\w'’`\-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’`\-]+)?,\s)"
)


# "[BJP12]", "[RMW+14]": the alpha style labels an entry with its authors'
# initials and a two-digit year, and the text cites it by that label.
_ALPHA_MARKER = re.compile(r"(?m)^\s*\[([A-Z][A-Za-z]{1,6}\+?\d{2}[a-z]?)\]\s*")


def _split_alpha(refs_text: str) -> list[tuple[str, str]]:
    matches = list(_ALPHA_MARKER.finditer(refs_text))
    return _trim_last([
        (match.group(1), refs_text[match.end(): nxt.start() if nxt else len(refs_text)])
        for match, nxt in zip(matches, matches[1:] + [None])
    ])


def _split_numbered(refs_text: str) -> list[tuple[int | None, str]]:
    chunks = _split_on(_ENTRY_MARKER, refs_text)
    if len(chunks) < 2:
        chunks = _split_on(_BARE_MARKER, refs_text)
    return _trim_last(chunks)


def _trim_last(chunks: list[tuple[int | None, str]]) -> list[tuple[int | None, str]]:
    """Cut what follows the bibliography off its last entry.

    Nothing marks the end of the last entry, so whatever the paper prints next
    — author biographies, an appendix, a figure caption — is read as part of
    it. An entry is a few lines; one that runs on for a page has swallowed
    something. Cut it at the first paragraph break after it has said what a
    reference says.
    """
    for at, (number, raw) in enumerate(chunks):
        # The last entry has nothing after it to stop at; an earlier one only
        # runs this long when a figure or a column of body text sits inside it.
        if len(raw) <= (500 if at == len(chunks) - 1 else 900):
            continue
        for match in re.finditer(r"\n\s*\n", raw):
            head = raw[: match.start()].strip()
            if len(head) >= 40 and (_YEAR.search(head) or head.endswith(".")):
                chunks[at] = (number, head)
                break
    return chunks


def _split_on(marker: re.Pattern, refs_text: str) -> list[tuple[int | None, str]]:
    matches = list(marker.finditer(refs_text))
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


def _split_year_sentence(refs_text: str) -> list[tuple[int | None, str]]:
    """Split where an author run followed by a bare "2019." begins a new entry.

    ACL and ACM print "Gabor Angeli and Christopher D. Manning. 2014. Title…":
    given names first and spelled out, the year a sentence of its own. No
    entry-initial surname pattern fits that, and in a justified column the
    author list itself wraps and picks up blank lines, so neither line starts
    nor blocks mark the entries either.
    """
    flat = re.sub(r"\s*\n\s*", " ", refs_text).strip()
    starts = [0] + [m.end() for m in _YEAR_SENTENCE_HEAD.finditer(flat)]
    return [
        (None, flat[start:end])
        for start, end in zip(starts, starts[1:] + [len(flat)])
    ]


def _split_year_terminated(refs_text: str) -> list[tuple[int | None, str]]:
    """Split where an entry ends on its year and the next opens "Surname, Given".

    The ICLR and ICML style: "Duchi, John, Hazan, Elad, and Singer, Yoram.
    Adaptive subgradient methods. JMLR, 12:2121-2159, 2011." Given names are
    spelled out after the surname, so no initials mark where an entry opens,
    and a justified column wraps the author list across blank lines. What is
    regular is the end: the year, then a full stop.
    """
    flat = re.sub(r"\s*\n\s*", " ", refs_text).strip()
    starts = [0] + [m.end() for m in _YEAR_TERMINATED_HEAD.finditer(flat)]
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

    # Identifiers are full of four-digit runs that read as years: the arXiv id
    # "2003.00648" was submitted in 2020, and "1912.03619" is not from 1912.
    year = ""
    dated = _URL.sub(" ", _DOI.sub(" ", _ARXIV.sub(" ", raw)))
    # "15(1):1929-1958, 2014" was published in 2014, not on page 1929.
    suffix = ""
    years = [
        m for m in _YEAR.finditer(dated)
        if not re.match(r"\s*[\-‐‑–—]\s*\d", dated[m.end():m.end() + 4])
        and not re.search(r"(?:\d\s*[\-‐‑–—]|:|pp?\.)\s*$", dated[max(0, m.start() - 6):m.start()])
    ] or list(_YEAR.finditer(dated))
    if years:
        year = years[0].group(1)
        suffix = years[0].group(0)[4:]

    authors, title, venue = _split_fields(raw)

    label = number if isinstance(number, str) else ""
    if label:
        number = None
    if label:
        key = label
    elif number is not None:
        key = str(number)
    else:
        # Derived from the same phrase the alias index works from, so an entry's
        # own key is always one of the keys a marker can reach it by.
        head = authors or raw
        words = _name_words(_entry_surname(head))
        if not (words and year):
            return None
        # A name with no initials to mark its surname is almost always a
        # spelled-out given name followed by the surname ("Gabor Angeli"), so
        # the last word is what a marker prints. The other words stay reachable
        # as aliases, which covers the two-word surname this guesses wrong.
        if not (_SURNAME_FIRST.match(head) or _INITIALS_FIRST.match(head)
                or _VANCOUVER_FIRST.match(head)):
            words = words[-1:]
        # The letter is how the paper itself tells two works by the same
        # authors in the same year apart; without it both key alike and neither
        # can be reached.
        key = normalise_key("".join(words), year) + suffix

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
# Latin Extended-A and -B interleave their cases, so past Latin-1 the
# "uppercase" class simply admits both: "Łukasz" has to be able to open a name.
_U = r"A-ZÀ-ÖØ-ÞĀ-ɏ"                               # uppercase letters
# Latin-1 + Latin Extended-A, plus the spacing modifier letters that PDF text
# extraction leaves behind ("Přikryl" often arrives as "Pˇrikryl").
_L = r"A-Za-zÀ-ÖØ-öø-ÿĀ-ɏˀ-˿"
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
# surname itself to start a fresh capitalised word. A capital running straight
# into lowercase is the start of a name, not an initial: "Ido Dagan" otherwise
# reads as the initial "I" and the particled surname "do Dagan".
_INITIAL = rf"[{_U}]{{1,3}}(?![a-zß-öø-ÿ])\.?(?:\s*-\s*[{_L}]\.?)?"
_INITIALS_HEAD = rf"(?:{_INITIAL}\s*){{1,4}}"

# One author's full name, however many parts it runs to: "Agatz", "Rashid
# Alyassi", "Seyed Mahdi Shavarani", "David C. Edwards", "Raïssa G. Mbiadou
# Saleu". Stopping at two words leaves the surname outside the phrase whenever a
# given name is spelled out in full, and the surname is the only part an
# author-year marker ever prints. A comma or "&" ends the run, so it cannot run
# on into the next author.
# A middle initial may be printed bare ("Joseph L Fleiss"); stopping at it leaves
# the given name standing in for the surname.
_NAME_RUN = rf"{_NAME}(?:\s+(?:{_NAME}|[{_U}]\.|[{_U}](?=\s+[{_U}]))){{0,3}}"

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
_INITIALED = rf"(?:[{_U}]\.(?:\s*-?\s*[{_U}]\.)*\s*)+{_NAME}(?:\s+{_NAME})?"
_INITIALS_THEN_STOP = re.compile(
    # "A. One", "A. One and B. Two", "A. One, B. Two, and C. Three" — then a stop.
    rf"^\s*({_INITIALED}(?:\s*,\s*{_INITIALED})*(?:\s*,?\s*(?:and|&)\s+{_INITIALED})?)"
    rf"\.\s+(?![{_U}]\.)(.{{8,}})$",
    re.S,
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


_ORGANISATION = re.compile(
    r"\b(organi[sz]ation|association|institute|institution|committee|consortium|"
    r"society|agency|council|commission|department|ministry|foundation|"
    r"corporation|federation|bureau|authority|union)\b",
    re.IGNORECASE,
)


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
    # Style Y -- "Given Surname and Given Surname. 2019. Title. Venue." (ACL,
    # ACM). The year sentence marks the end of the authors outright; left to the
    # later styles, the period after a middle initial is read as that boundary
    # and the title comes back as the last author's surname.
    m = _YEAR_SENTENCE_ENTRY.match(stripped)
    if m and len(m.group(2).strip()) >= 8:
        title, venue = _first_sentence(m.group(2).strip())
        return m.group(1).strip(" .,;&"), title, venue

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
    # Style I -- "Y. Bengio, P. Simard, and P. Frasconi. Title. Venue, 1994."
    # Initials first and no quotation marks: the last author ends on a full
    # stop, not a comma, so the run below stops one author short and the title
    # comes back as "and P".
    m = _INITIALS_THEN_STOP.match(stripped)
    if m:
        title, venue = _first_sentence(m.group(2).strip())
        return m.group(1).strip(" .,;&"), title, venue

    for pattern in (_AUTHOR_RUN, _INITIALS_RUN, _VANCOUVER_RUN):
        run = pattern.match(stripped)
        if not (run and len(run.group(1)) >= 6):
            continue
        if pattern is _VANCOUVER_RUN and "," not in run.group(1):
            # "Diederik P. Kingma and Jimmy Ba. Adam: ..." opens exactly like a
            # Vancouver author, "Bosona T. Urban freight ...". What follows
            # tells them apart: more names, or a title.
            following = re.split(r"\.\s+", stripped[run.end():].strip(), maxsplit=1)
            if len(following) == 2 and _NAME_LIST.match(following[0]) and len(following[0].split()) <= 14:
                continue
        authors = run.group(1).strip(" .,;&")
        rest = _LEADING_ETAL.sub("", stripped[run.end():]).strip(" .,;:")
        rest = _LEADING_YEAR.sub("", rest)
        if len(rest) >= 8:
            title, venue = _first_sentence(rest)
            return authors, title, venue

    # Style O -- "Organisation, Title of the document, Report type, 2023."
    # Standards and reports are authored by a body and printed with no quotes
    # and no full stops. Nothing above matches, and the comma rule below then
    # takes the body's own name for the title: every ISO standard in a list
    # becomes "International Organization for Standardization", resolves to the
    # same encyclopedia entry, and is reported as a duplicate of the others.
    commas = [p.strip() for p in stripped.split(",")]
    if len(commas) >= 3 and _ORGANISATION.search(commas[0]) and len(commas[1]) >= 12:
        return commas[0], commas[1], ", ".join(commas[2:])[:200]

    # Style C -- period-delimited, no recognisable author run.
    parts = [p.strip() for p in re.split(r"\.\s+", stripped) if p.strip()]
    # The stop after a middle initial is not the end of the author list:
    # "Diederik P. Kingma and Jimmy Ba. Adam", "Movshon, J. Anthony, and
    # Simoncelli, Eero. Title". Cut there, the entry is keyed on a given name
    # and no citation of it can find it.
    while (
        len(parts) >= 3
        and re.search(rf"(?:^|[\s,])[{_U}]$", parts[0])
        and _NAME_LIST.match(f"{parts[0]}. {parts[1]}")
    ):
        parts[:2] = [f"{parts[0]}. {parts[1]}"]
    if len(parts) >= 2:
        head = parts[0]
        # A long head with no initials is far more likely to be the title —
        # unless it is nothing but names, which is an author list with the
        # given names spelled out ("Duchi, John, Hazan, Elad, and Singer,
        # Yoram"). Read as a title, that sends the search out for a paper
        # called after its own authors.
        names_only = ("," in head or " and " in head) and _NAME_LIST.match(head)
        if not names_only and not re.search(r"[A-Z]\.", head) and len(head.split()) > 6:
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
    # Only before a capital, though: "Good Question! statistical ranking for
    # question generation" carries on in lowercase and is still one title.
    m = re.match(r"^(.{5,300}?)(\.|[?!](?!\s+[a-z]))\s+(.*)$", text)
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


# The same idea for the year-as-a-sentence styles. Here nothing brackets the
# year, so the author list has to be held tighter: the only periods it may
# contain are the ones after an initial, "et al." or "Jr.". Any other period
# ends a sentence, which means the text before it was a title or a venue.
_YEAR_SENTENCE_AUTHORS = (
    rf"(?:[{_L}\s,;&'’{_DASH}]"
    rf"|(?<=\b[{_U}])\.|(?<=\bal)\.|(?<=\b[JS]r)\.){{0,400}}?"
)
_YEAR_SENTENCE = r"\.\s+(?:19|20)\d{2}[a-z]?\.\s"
_YEAR_SENTENCE_HEAD = re.compile(
    rf"(?<=[.])(?<!\b[{_U}]\.)\s+"
    rf"(?={_AUTHOR_HEAD}{_YEAR_SENTENCE_AUTHORS}{_YEAR_SENTENCE})"
)
_YEAR_SENTENCE_ENTRY = re.compile(
    rf"^\s*({_AUTHOR_HEAD}{_YEAR_SENTENCE_AUTHORS}){_YEAR_SENTENCE}\s*(.+)$"
)


# After an entry that closes on its year, "Surname, Given" or "Surname, I." is
# enough to open the next. After any other full stop ("... Oral Presentation.")
# only the initials form is trusted: "Acoustics, Speech and Signal Processing"
# is a journal, and it follows a full stop too.
# Words that follow a year and a full stop without opening a new entry: the
# publisher, a note, the editors of the volume.
_NOT_AN_AUTHOR = (
    r"(?!(?:In|URL|Proceedings|Proc|Curran|Springer|MIT|Morgan|Elsevier|IEEE|ACM|"
    r"Technical|Tech|Available|Oral|Note|Also|Online|Association|Advances|Preprint|"
    r"Unpublished|Submitted|To|Software|Version|Retrieved|Accessed|Cambridge|Oxford)\b)"
)
_YEAR_TERMINATED_HEAD = re.compile(
    # after "...2011." — any run of name words, given names first or surname
    # first, that goes on to a comma, an "and", or straight into a title
    rf"(?:(?<=\d{{4}}\.)|(?<=\d{{4}}[a-z]\.))\s+(?:\d{{1,3}}\s+)?"
    rf"(?={_NOT_AN_AUTHOR}(?:[{_U}]\.\s*){{0,3}}{_NAME}(?:\s+(?:{_NAME}|[{_U}]\.?(?=[\s,]))){{0,3}}"
    rf"(?:,|\s+and\s|\.\s+[{_U}]))"
    # after any other full stop — only an unmistakable author list
    rf"|(?<=[{_L})\d]\.)(?<!\b[{_U}]\.)\s+(?={_NOT_AN_AUTHOR}{_NAME}(?:\s+{_NAME})?,\s+"
    rf"(?:[{_U}]\.,?\s|{_NAME},\s+{_NAME},\s+{_NAME}))"
)
# "Duchi, John, Hazan, Elad, and Singer, Yoram": nothing but names.
_NAME_LIST = re.compile(
    rf"^(?:{_NAME}|[{_U}]\.?|and|&|et|al\.?)"
    rf"(?:[\s,]+(?:{_NAME}|[{_U}]\.?(?:-[{_U}]\.?)?|[a-z]{{2,4}}|and|&|et|al\.?))*$"
)


# "Bates DM, Watts DG (1988)" -- Vancouver initials in an author-year list.
# Without this the capitals read as a second name word and the entry is keyed
# "dm1988".
_VANCOUVER_FIRST = re.compile(
    # No full stop after the capitals: "Samuel R. Bowman" is a given name and
    # a middle initial, not a surname and its initials.
    rf"^\s*({_NAME}(?:\s+{_NAME})??)\s+[A-Z]{{1,3}}(?=\s*[,(]|\s+and\b|\s*$)"
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
    for pattern in (_SURNAME_FIRST, _INITIALS_FIRST, _VANCOUVER_FIRST, _BARE_NAME):
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
    suffix = ref.key[-1] if re.search(r'\d{4}[a-z]$', ref.key) else ''
    candidates = [normalise_key(w, ref.year) + suffix for w in words]
    # ...and without the letter, for the paper that prints "2013a" in its list
    # and plain "2013" in its text. Two entries answering to that drop out as
    # ambiguous, like any other shared alias.
    candidates += [normalise_key(w, ref.year) for w in words]
    candidates.append(normalise_key("".join(words), ref.year) + suffix)
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


def _author_total(ref: Reference) -> int:
    """1, 2, or 3 for "more than two", as a marker would have to print them."""
    text = ref.authors or ref.raw[:200]
    if re.search(r"\bet\s+al\b", text):
        return 3
    people = [p.strip() for p in text.split(",") if p.strip()]
    if people and all(re.fullmatch(rf"{_NAME}(?:\s+{_NAME})?\s+[A-Z]{{1,3}}", p) for p in people):
        return min(len(people), 3)      # "Bates D, Maechler M": Vancouver, no "and"
    parts = [p for p in re.split(r",?\s+and\s+|\s*&\s*", text) if p.strip()]
    if len(parts) == 1:
        return 1 if text.count(",") <= 1 else 3
    if len(parts) > 2:
        return 3
    first = parts[0].strip()
    if "," not in first:
        return 2
    # "Kingma, D. P." and "Kingma, Diederik" are one person; "Kevin Clark,
    # Minh-Thang Luong" are two.
    surname, _, rest = first.partition(",")
    one_person = "," not in rest and len(surname.split()) <= 2 and (
        re.fullmatch(r"\s*(?:[A-Z]\.?\s*-?\s*){1,4}", rest) or len(rest.split()) <= 2
    ) and len(surname.split()) == 1
    return 2 if one_person else 3


def _label_total(label: str) -> int:
    if re.search(r"\bet\s+al\b", label):
        return 3
    return 2 if re.search(r"\band\b|&", label) else 1


def _split_shared_key(key: str, grouped: dict[str, list], references: list[Reference]) -> dict[str, Reference]:
    """Tell entries with one key apart by how each marker names its authors.

    "(Graves, 2013)" and "(Graves et al., 2013)" key alike, and an index that
    drops shared keys leaves both entries unreachable. The markers have not
    lost the distinction: one names a sole author, one a pair, one "et al.".
    Each group of markers that fits exactly one entry is moved under a key of
    its own ("graves2013/1", "graves2013/3"), along with that entry.
    """
    candidates = [r for r in references if r.key == key]
    if len(candidates) < 2:
        return {}
    by_total: dict[int, list] = {}
    for cite in grouped[key]:
        by_total.setdefault(_label_total(getattr(cite, "label", "")), []).append(cite)

    resolved: dict[str, Reference] = {}
    left = []
    for total, cites in by_total.items():
        fitting = [r for r in candidates if _author_total(r) == total]
        if len(fitting) != 1:
            left.extend(cites)
            continue
        new_key = f"{key}/{total}"
        fitting[0].key = new_key
        for cite in cites:
            cite.key = new_key
        grouped[new_key] = cites
        resolved[new_key] = fitting[0]
    if resolved:
        if left:
            grouped[key] = left
        else:
            del grouped[key]
    return resolved


def _drop_number_lists(grouped: dict[str, list], references: list[Reference]) -> None:
    """Discard the in-range numbers of a bracket that also holds impossible ones.

    "temperatures of [1, 2, 5, 10]" in a paper with nine references is a list
    of values. The 10 is reported as out of range either way; the 1, 2 and 5
    would be recorded as citations of the first, second and fifth references,
    each then judged against a sentence about temperatures.
    """
    numbers = [r.number for r in references if r.number is not None]
    if not numbers:
        return
    highest = max(numbers)
    suspect = {
        (cite.char_offset, cite.label)
        for key, cites in grouped.items() if key.isdigit() and int(key) > highest
        for cite in cites
    }
    if not suspect:
        return
    for key in list(grouped):
        if key.isdigit() and int(key) > highest:
            continue
        kept = [c for c in grouped[key] if (c.char_offset, c.label) not in suspect]
        if kept:
            grouped[key] = kept
        else:
            del grouped[key]


def link_citations(
    grouped: dict[str, list],
    ref_index: dict[str, Reference],
    all_references: list[Reference] | None = None,
) -> tuple[dict[str, Reference], list[str]]:
    """Return the references that are actually cited, plus unmatched keys."""
    matched: dict[str, Reference] = {}
    orphans: list[str] = []
    _drop_number_lists(grouped, all_references or [])
    aliases = _alias_index(list(ref_index.values()))
    for key in list(grouped):
        ref = ref_index.get(key) or aliases.get(key)
        if ref is None:
            split = _split_shared_key(key, grouped, all_references or [])
            matched.update(split)
            if key not in grouped:
                continue
        if ref is None and re.search(r"\d{4}[a-z]$", key):
            # The text says "2019a", the list just "2019".
            ref = ref_index.get(key[:-1]) or aliases.get(key[:-1])
        if ref:
            matched[key] = ref
        elif not all(getattr(c, "tentative", False) for c in grouped[key]):
            orphans.append(key)
    return matched, orphans
