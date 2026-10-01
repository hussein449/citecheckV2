"""Structural checks on a citation that need no network access.

These catch a different class of error from the content check. When a paper
says "Pinto et al. [29]" but entry [29] is by Kim et al., the numbering has
slipped — and that is worth flagging loudly even if, by coincidence, the cited
paper is topically plausible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher

# "Pinto et al. [29]" — unambiguous attribution to a person.
_CREDIT_ETAL = re.compile(
    r"\b([A-Z][a-zA-Z'’\-]{2,})\s+et\s+al\.?\s*(?:\(\d{4}\)\s*)?\[\s*(\d{1,3})"
)

# "Kim and Cho [3]" — the same shape as any two-item list, so on its own this
# matches table rows like "Windows, Red Hat and OPNET [131]". Only trusted when
# an attribution verb follows the marker.
_CREDIT_AND = re.compile(
    r"\b([A-Z][a-zA-Z'’\-]{2,})"
    r"\s+(?:and|&)\s+[A-Z][a-zA-Z'’\-]{2,}"
    r"\s*(?:\(\d{4}\)\s*)?\[\s*(\d{1,3})\s*\]\s*([^.]{0,40})"
)

_ATTRIBUTION_VERB = re.compile(
    r"\b(?:propos|present|show|develop|introduc|argu|demonstrat|suggest|found|"
    r"report|describ|design|investigat|examin|analys|analyz|studi|evaluat|"
    r"compar|deriv|characteris|characteriz|outlin|discuss|conclud|observ|"
    r"not|appli|implement|extend|survey|review|explor|address|consider)",
    re.IGNORECASE,
)

# Words that look like surnames but never are, in this position.
_NOT_A_NAME = {
    "the", "in", "of", "and", "for", "with", "table", "figure", "fig", "section",
    "authors", "work", "works", "study", "studies", "paper", "papers", "model",
    "method", "approach", "algorithm", "protocol", "network", "system", "using",
    "see", "refs", "ref", "reference", "references", "e.g", "i.e", "as", "by",
    "however", "moreover", "finally", "furthermore", "similarly", "likewise",
    "recently", "additionally", "also", "these", "this", "those", "both", "an",
}


@dataclass
class Flag:
    kind: str
    severity: str          # "high" | "medium" | "low"
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _fold(text: str) -> str:
    """Strip accents and case so 'Osório' matches 'Osorio'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def surnames_of(authors: str) -> set[str]:
    """Pull plausible surnames out of a bibliography author string.

    Handles both conventions — "Kim, G.-H." and "G.-H. Kim" — by simply taking
    every token that is not an initial.
    """
    return set(_name_words(authors))


def _name_words(authors: str) -> list[str]:
    """Every name word in an author string, folded, in order, initials dropped."""
    names: list[str] = []
    for token in re.split(r"[,;&]|\band\b", authors or ""):
        for word in token.split():
            letters = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿĀ-ſ]", "", word)
            if len(letters) < 2:
                continue
            # Initials carry dots and almost no letters: "G.", "G.-H.", "J.M.".
            # Two-letter surnames (Wu, Li, Xu) have no dots, so they survive.
            if "." in word and len(letters) <= 3:
                continue
            if _fold(letters) in {"et", "al", "jr", "sr", "eds", "ed", "the"}:
                continue
            names.append(_fold(letters))
    return names


def credited_surname(sentence: str, key: str) -> str:
    """The surname the prose credits for this marker, if it names one."""
    sentence = sentence or ""

    for match in _CREDIT_ETAL.finditer(sentence):
        if match.group(2) == key and _fold(match.group(1)) not in _NOT_A_NAME:
            return match.group(1)

    for match in _CREDIT_AND.finditer(sentence):
        if match.group(2) != key:
            continue
        name = match.group(1)
        if _fold(name) in _NOT_A_NAME:
            continue
        # Without a verb of attribution this is a list, not a credit.
        if not _ATTRIBUTION_VERB.search(match.group(3) or ""):
            continue
        return name
    return ""


def check(key: str, reference, citations, duplicate_of: str = "") -> list[Flag]:
    """Structural flags for one reference and the places it is cited."""
    flags: list[Flag] = []

    if duplicate_of:
        flags.append(
            Flag(
                kind="duplicate-entry",
                severity="low",
                message=(
                    f"This bibliography entry is a duplicate of [{duplicate_of}] — "
                    "the same work is listed twice under different numbers."
                ),
            )
        )

    known = surnames_of(reference.authors)
    title_words = {_fold(w) for w in re.findall(r"[A-Za-z'’\-]{3,}", reference.title or "")}

    if known:
        for cite in citations:
            named = credited_surname(cite.sentence, key)
            if not named:
                continue
            folded = _fold(named)
            if folded in known:
                continue
            # The credited word is in the title — it names the work itself
            # (a tool, system, or dataset), not an author.
            if folded in title_words:
                continue
            flags.append(
                Flag(
                    kind="author-mismatch",
                    severity="high",
                    message=(
                        f"The text credits “{named}” for [{key}], but that entry is "
                        f"by {reference.authors[:70]}. The reference numbering may "
                        "have slipped."
                    ),
                )
            )
            break

    return flags


# How sure the title match has to be before an index record is taken to be the
# cited work, and so worth comparing an entry's authors and year against. Title
# search returns near-misses built from the same vocabulary, and a record for
# some other paper disagrees on everything.
_SAME_WORK = 0.85
# Spellings this close are one name transliterated two ways ("Müller" and
# "Mueller"), not two people. "Zheng"/"Zhang" and "Langmead"/"Longmead" fall
# well below it.
_SAME_NAME = 0.9

_LETTERS = r"[^A-Za-zÀ-ÖØ-öø-ÿĀ-ſ]"


def _alike(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _display(name: str) -> str:
    """Crossref stores some family names in capitals: "DAGAN"."""
    return name.title() if name.isupper() else name


def _printed_authors(reference) -> str:
    """The author part of the entry as printed, not as parsed.

    The parsed author field can stop short — at a middle initial, at "et al." —
    and an author it dropped would be reported as missing from the entry.
    Everything before the title is the safer text to search.
    """
    raw = reference.raw or ""
    at = raw.find(reference.title[:40]) if len(reference.title or "") >= 12 else -1
    return raw[:at] if at > 0 else (reference.authors or "")


def _year_gap(reference, records) -> dict | None:
    """The record whose year the entry contradicts, if every record does."""
    printed = reference.year or ""
    if not printed or printed in (reference.title or ""):
        # A year inside the title is part of the title, not the date.
        return None
    # Books are reissued for decades and each index dates a different edition.
    dated = [r for r in records if r.get("year") and not r.get("book")]
    # An entry that cites the preprint is dated by the preprint server. The
    # other indices give the year of the journal version, often years later.
    if re.search(r"arxiv|preprint|biorxiv|medrxiv", reference.raw or "", re.I):
        dated = [r for r in dated if r.get("index") == "arXiv"]
    # A year or two apart is the gap between a preprint and its publication, or
    # between online-first and print. It is not an error in the entry, and
    # neither is a year that any one index agrees with.
    if not dated or any(abs(int(r["year"]) - int(printed)) < 3 for r in dated):
        return None
    return dated[0]


def _missing_author(reference, records) -> tuple[dict, str, str] | None:
    """(record, the author it lists, what the entry prints instead or "")."""
    printed_text = _printed_authors(reference)
    printed = _name_words(printed_text)
    if not printed:
        return None
    truncated = bool(re.search(r"\bet\s+al\b|\band\s+others\b", reference.raw or "", re.I))

    def is_printed(surname: str) -> bool:
        words = _name_words(surname)
        return any(
            _alike(candidate, name) >= _SAME_NAME
            for candidate in words + ["".join(words)]
            for name in printed
        )

    for record in records:
        surnames = [s for s in record.get("surnames") or [] if _name_words(s)]
        if not surnames:
            continue
        # The first author is always printed. The rest are only checked when
        # the entry lists everyone, which "et al." or a short list rules out.
        expected = surnames[:1]
        if not truncated and len(printed) >= len(surnames) <= 12:
            expected = surnames
        missing = [s for s in expected if not is_printed(s)]
        if not missing:
            return None
        wanted = _name_words(missing[0])[-1]
        # The misspelling is a name the record does not account for, about as
        # long as the one it replaced: "Dragon" for "Dagan", not the "Dan" of a
        # co-author's given name.
        spare = [
            name for name in printed
            if not any(_alike(name, w) >= _SAME_NAME for s in surnames for w in _name_words(s))
        ]
        closest = max(
            spare,
            key=lambda name: _alike(wanted, name) - 0.1 * (abs(len(name) - len(wanted)) > 1),
            default="",
        )
        shown = ""
        if closest and _alike(wanted, closest) >= 0.6:
            shown = next(
                (w.strip(".,;") for w in printed_text.split()
                 if _fold(re.sub(_LETTERS, "", w)) == closest),
                closest,
            )
        return record, _display(missing[0]), shown
    return None


def metadata_flags(source, reference) -> list[Flag]:
    """Where the entry's authors or year disagree with the published record."""
    if source is None or reference is None:
        return []
    if getattr(source, "existence", "") != "confirmed":
        return []
    records = [
        r for r in getattr(source, "records", None) or []
        if r.get("title_agreement", 0) >= _SAME_WORK
    ]
    author = _missing_author(reference, records)
    dated = _year_gap(reference, records)

    # Wrong first author *and* wrong year under a matching title is not two
    # slips in one entry. It is a different work that shares the title, which
    # is what a mistyped or invented title looks like from here.
    if author and dated and not author[2]:
        record, name, _ = author
        return [Flag(
            kind="record-mismatch",
            severity="medium",
            message=(
                f"The published work with this title is by {name} "
                f"({dated['year']}, per {record['index']}), which matches neither the "
                f"authors nor the year ({reference.year}) printed in the entry. The "
                "entry may have the wrong title, or mix up two works."
            ),
        )]

    flags: list[Flag] = []
    if author:
        record, name, shown = author
        flags.append(Flag(
            kind="author-name-mismatch",
            severity="medium",
            message=(
                f"{record['index']} lists “{name}” as an author of this work, "
                + (f"but the entry prints “{shown}”. " if shown
                   else "who does not appear in the entry. ")
                + "Check the author names against the published record."
            ),
        ))
    if dated:
        flags.append(Flag(
            kind="year-mismatch",
            severity="medium",
            message=(
                f"The entry gives the year as {reference.year}, but {dated['index']} "
                f"records this work as published in {dated['year']}. Check the year, "
                "or that the entry cites the edition it means to."
            ),
        ))
    return flags


def source_flags(source, reference=None) -> list[Flag]:
    """Flags that come from what the indices said about the cited work.

    Separate from `check` because these need the network stage to have run,
    whereas everything above is decided from the PDF alone.
    """
    flags: list[Flag] = []
    if source is None:
        return flags
    flags.extend(metadata_flags(source, reference))

    if getattr(source, "retracted", False):
        where = ", ".join(sorted({i.get("source", "") for i in source.integrity if i.get("source")}))
        detail = next(
            (i for i in source.integrity if i.get("kind") in {"retraction", "withdrawal", "removal"}),
            {},
        )
        year = f" in {detail['date']}" if detail.get("date") else ""
        flags.append(
            Flag(
                kind="retracted-source",
                severity="high",
                message=(
                    f"This work was RETRACTED{year}. It cannot support the claim made "
                    f"about it, and citing it uncritically is itself a finding. "
                    f"(Recorded by: {where or 'an index'}.)"
                ),
            )
        )
    # A published erratum or corrigendum is deliberately *not* flagged. It says
    # the cited work was amended, not that the citation misrepresents it, and
    # this report answers only the latter. Retractions stay: a retracted work
    # cannot support any claim, whatever the citing sentence says.

    wrong = getattr(source, "identifier_wrong", "")
    other = getattr(source, "identifier_points_to", "")
    if wrong and other:
        found = wrong.lower() != (getattr(source, "doi", "") or "").lower()
        flags.append(Flag(
            kind="identifier-mismatch",
            severity="high",
            message=(
                f"The identifier printed for this reference ({wrong}) belongs to a "
                f"different work: “{other[:110]}”. "
                + (f"The work the entry names exists as {source.doi}. Correct the "
                   "identifier in the entry." if found else
                   "Check which of the two the entry means to cite.")
            ),
        ))
    elif wrong:
        flags.append(Flag(
            kind="identifier-mismatch",
            severity="high",
            message=(
                f"The DOI printed for this reference ({wrong}) is not registered. "
                f"The work itself exists, as {source.doi}. Correct the DOI in the entry."
            ),
        ))

    existence = getattr(source, "existence", "")
    if existence == "not_found":
        if getattr(source, "identifier_printed", "") == "url":
            flags.append(Flag(kind="reference-not-found", severity="high", message=(
                "The web address printed for this reference does not load, and no "
                "bibliographic index has a record of it. Check the address."
            )))
            return flags
        if getattr(source, "identifier_printed", "") in ("doi", "arxiv"):
            message = (
                f"The {source.identifier_printed.upper()} printed for this reference is "
                "not registered anywhere. Identifiers are issued, not invented, so one "
                "that resolves nowhere usually means the reference was fabricated."
            )
        else:
            message = (
                "No bibliographic index has any record of this reference, and no DOI "
                "or arXiv id was printed for it. Verify it exists before accepting it."
            )
        flags.append(Flag(kind="reference-not-found", severity="high", message=message))

    return flags


def find_duplicates(references: dict) -> dict[str, str]:
    """Map each duplicated reference key to the first key holding that work."""
    seen: dict[str, str] = {}
    duplicates: dict[str, str] = {}
    for key in sorted(references, key=lambda k: (0, int(k)) if k.isdigit() else (1, k)):
        ref = references[key]
        fingerprint = ref.doi.lower() if ref.doi else _fold(ref.title)[:80]
        if len(fingerprint) < 8:
            continue
        if fingerprint in seen:
            duplicates[key] = seen[fingerprint]
        else:
            seen[fingerprint] = key
    return duplicates
