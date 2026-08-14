"""Parse the bibliography block into individual, structured entries.

Each entry ends up with whatever identifiers we can salvage — DOI, arXiv id,
bare URL, title, authors, year — which is what makes the source retrievable in
the next stage.
"""

from __future__ import annotations

import re
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

    chunks = _split_numbered(refs_text)
    if len(chunks) < 2:
        chunks = _split_author_year(refs_text)

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
    # break that and means we picked the wrong pattern.
    numbers = [int(m.group(1) or m.group(2) or m.group(3)) for m in matches]
    ascending = sum(1 for a, b in zip(numbers, numbers[1:]) if b > a)
    if ascending < len(numbers) * 0.6:
        return []

    chunks: list[tuple[int | None, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(refs_text)
        chunks.append((numbers[idx], refs_text[start:end]))
    return chunks


def _split_author_year(refs_text: str) -> list[tuple[int | None, str]]:
    """Split an unnumbered bibliography on entry-initial surnames."""
    lines = [l for l in refs_text.splitlines()]
    starter = re.compile(r"^\s*[A-Z][A-Za-z'’\-]+,\s*(?:[A-Z]\.\s*)+")
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

    if len(chunks) < 2:
        # Last resort: blank-line separated blocks.
        blocks = [b for b in re.split(r"\n\s*\n", refs_text) if b.strip()]
        chunks = [(None, b) for b in blocks]
    return chunks


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
        surname = re.match(r"\s*([A-Z][A-Za-z'’\-]+)", raw)
        if not (surname and year):
            return None
        key = normalise_key(surname.group(1), year)

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
_NAME = rf"{_PARTICLE}[{_U}][{_L}'’\-]+"           # a capitalised name word

# A run of "Surname, A. B.," entries — the author block of a numeric-style entry.
# Matching this explicitly avoids mistaking the final initial's period ("…,
# Polosukhin, I. Attention is all you need") for the end of a sentence.
_AUTHOR_RUN = re.compile(
    rf"^\s*((?:"
    rf"(?:and\s+|&\s+)?"                   # conjunction before the last author
    rf"{_NAME}"                            # surname
    rf"(?:\s+{_NAME})?"                    # optional second surname word
    rf",\s*(?:[{_L}]\.\s*)+"               # , A. B. (initials may be lowercase)
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
_LEADING_ETAL = re.compile(r"^(?:et\s+al\.?\s*,?\s*)+", re.IGNORECASE)


def _split_fields(raw: str) -> tuple[str, str, str]:
    """Best-effort author / title / venue split across common styles."""
    stripped = _URL.sub("", raw)
    stripped = re.sub(r"\bdoi:\s*\S+", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\barXiv:\s*\S+", "", stripped, flags=re.IGNORECASE).strip()

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
    for pattern in (_AUTHOR_RUN, _INITIALS_RUN):
        run = pattern.match(stripped)
        if not (run and len(run.group(1)) >= 6):
            continue
        authors = run.group(1).strip(" .,;&")
        rest = _LEADING_ETAL.sub("", stripped[run.end():]).strip(" .,;")
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
        return head, parts[1], ". ".join(parts[2:])[:200]

    return "", _first_sentence(stripped)[0], ""


def _first_sentence(text: str) -> tuple[str, str]:
    m = re.match(r"^(.{5,300}?)\.\s+(.*)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()[:200]
    return text.strip(" .")[:300], ""


def index_references(references: list[Reference]) -> dict[str, Reference]:
    return {ref.key: ref for ref in references}


def _leading_surname(ref: Reference) -> str:
    """The surname phrase an author-year marker would print for this entry."""
    match = re.match(rf"\s*({_NAME}(?:\s+{_NAME})?)", ref.authors or ref.raw)
    return match.group(1).strip() if match else ""


def _author_year_aliases(ref: Reference) -> set[str]:
    """Every author-year key that could plausibly point at *ref*."""
    if not ref.year:
        return set()
    words = _leading_surname(ref).split()
    if not words:
        return set()
    # A two-word surname may be cited by either word or by both ("Betti
    # Sorbelli, 2024" vs "Sorbelli, 2024"), and which one the marker uses is not
    # recoverable from the bibliography, so index all three spellings.
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
