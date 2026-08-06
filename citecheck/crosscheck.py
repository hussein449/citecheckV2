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
    names: set[str] = set()
    for token in re.split(r"[,;&]|\band\b", authors or ""):
        for word in token.split():
            letters = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ]", "", word)
            if len(letters) < 2:
                continue
            # Initials carry dots and almost no letters: "G.", "G.-H.", "J.M.".
            # Two-letter surnames (Wu, Li, Xu) have no dots, so they survive.
            if "." in word and len(letters) <= 3:
                continue
            if _fold(letters) in {"et", "al", "jr", "sr", "eds", "ed", "the"}:
                continue
            names.add(_fold(letters))
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


def source_flags(source) -> list[Flag]:
    """Flags that come from what the indices said about the cited work.

    Separate from `check` because these need the network stage to have run,
    whereas everything above is decided from the PDF alone.
    """
    flags: list[Flag] = []
    if source is None:
        return flags

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
    elif getattr(source, "integrity", None):
        kinds = sorted({i["kind"] for i in source.integrity})
        flags.append(
            Flag(
                kind="corrected-source",
                severity="medium",
                message=(
                    f"The cited work carries a published {', '.join(kinds)}. Check that "
                    "the claim survives the correction."
                ),
            )
        )

    existence = getattr(source, "existence", "")
    if existence == "not_found":
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
