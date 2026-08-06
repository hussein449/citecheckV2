"""Resolve a bibliography entry to a live URL and canonical metadata.

Order of preference:
  1. identifiers printed in the entry itself (arXiv, DOI, bare URL),
  2. a Crossref bibliographic lookup on the raw string,
  3. OpenAlex, which additionally hands back an open-access link and abstract,
  4. Semantic Scholar, Unpaywall and Europe PMC for an open-access copy.

The open-access link matters a lot: publisher landing pages are often paywalled,
and a screenshot of a paywall proves nothing.

Two things beyond a link come out of this stage:

  * **Existence.** Every index that answers is recorded. A reference no index has
    heard of is very different from one that is merely paywalled, and the
    difference is what separates a fabricated citation from an inaccessible one.
    Transport failures are recorded separately from clean misses, so an index
    being unreachable can never be mistaken for the index saying "no".
  * **Integrity.** Crossref and OpenAlex both report whether a work has been
    retracted or corrected. Citing retracted work is a desk-reject trigger, so
    it is worth a network call to find out.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict

import requests

from .refs import Reference

CONTACT = "citecheck-tool"
HEADERS = {
    "User-Agent": (
        f"CiteCheck/1.0 (academic citation verification; {CONTACT}) "
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
TIMEOUT = 25

# Crossref and OpenAlex give faster, more reliable service to callers who
# identify themselves, and Unpaywall refuses to answer without an address.
# Deliberately no default: a made-up address is worse than none, because it
# pollutes someone else's inbox and still loses the polite pool.
_CONTACT_EMAIL = (os.environ.get("CITECHECK_CONTACT_EMAIL") or "").strip()


def contact_email() -> str:
    return _CONTACT_EMAIL


def _polite(params: dict | None = None) -> dict:
    """Add the contact address the open scholarly APIs ask for."""
    out = dict(params or {})
    if _CONTACT_EMAIL:
        out["mailto"] = _CONTACT_EMAIL
    return out


# How confident a title match has to be before an index counts as having
# *confirmed* a reference exists. Below this the index found something, but not
# convincingly the thing that was cited.
_CONFIRM_AGREEMENT = 0.55

# …and how bad the *best* match anywhere has to be before we are willing to say
# the work does not exist. The gap between the two is deliberate and load-
# bearing: "we could not confirm this" and "you cited something imaginary" are
# very different statements, and a real paper that merely indexes badly must
# land in the silent zone between them. Wrongly accusing an author of
# fabrication is the worst mistake this tool can make, so the accusation is
# reserved for references nothing anywhere came close to.
_FABRICATION_CEILING = 0.35


@dataclass
class ResolvedSource:
    url: str = ""
    landing_url: str = ""
    oa_url: str = ""
    title: str = ""
    # The title as the citing paper printed it. Every "is this really the same
    # work?" test measures against this and never against `title`, which by then
    # may be some other index's guess.
    claimed_title: str = ""
    authors: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    arxiv_id: str = ""
    pmcid: str = ""
    abstract: str = ""
    resolver: str = ""            # which strategy produced the hit
    confidence: float = 0.0       # 0..1 that this is the right paper

    # Existence evidence. Kept as three separate lists because "no index has
    # this" and "no index answered" are opposite conclusions.
    indices_hit: list[str] = field(default_factory=list)
    indices_missed: list[str] = field(default_factory=list)
    indices_errored: list[str] = field(default_factory=list)
    identifier_printed: str = ""      # "doi" | "arxiv" | "url" | ""
    identifier_resolved: bool | None = None
    existence: str = "unconfirmed"    # "confirmed" | "unconfirmed" | "not_found"
    # How well the title of whatever a search returned agrees with the title the
    # citing paper printed. Only meaningful when no identifier was printed.
    search_agreement: float = 0.0
    # Best agreement seen at *any* index — the evidence the fabrication call
    # is weighed against.
    best_agreement: float = 0.0

    # Retractions and corrections found on the cited work.
    retracted: bool = False
    integrity: list[dict] = field(default_factory=list)

    notes: list[str] = field(default_factory=list)

    def hit(self, index: str) -> None:
        if index not in self.indices_hit:
            self.indices_hit.append(index)
        for other in (self.indices_missed, self.indices_errored):
            if index in other:
                other.remove(index)

    def missed(self, index: str) -> None:
        if index not in self.indices_hit and index not in self.indices_missed:
            self.indices_missed.append(index)

    def errored(self, index: str) -> None:
        if index not in self.indices_hit and index not in self.indices_errored:
            self.indices_errored.append(index)

    def to_dict(self) -> dict:
        return asdict(self)


def _get(url: str, **kwargs) -> requests.Response | None:
    """GET *url*, or None if the request never completed.

    None means transport failure. A returned response — including a 404 — means
    the index answered, which is what lets a clean miss be told apart from an
    unreachable service.
    """
    try:
        return requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
    except requests.RequestException:
        return None


def _record(src: ResolvedSource, index: str, resp, found: bool) -> None:
    """Log how one index answered for this reference."""
    if resp is None:
        src.errored(index)
    elif found:
        src.hit(index)
    else:
        src.missed(index)


def _agreement(
    src: ResolvedSource,
    ref: Reference | None,
    cand_title: str,
    cand_authors: str = "",
    cand_year: str = "",
) -> float:
    """How strongly a candidate record matches what the citing paper printed.

    Title overlap alone is brittle — real papers get indexed under subtitles,
    translated titles, and preprint titles that share few tokens with the
    published one. A matching first-author surname or publication year is weak
    evidence on its own but decisive alongside a middling title score, so both
    are allowed to lift a borderline match over the line.
    """
    score = _title_agreement(src.claimed_title, cand_title)

    if ref is not None and cand_authors:
        from .crosscheck import surnames_of

        printed = surnames_of(ref.authors or ref.raw[:120])
        found = surnames_of(cand_authors)
        if printed and found and (printed & found):
            score += 0.12

    if ref is not None and ref.year and cand_year and ref.year == cand_year:
        score += 0.08

    best = min(1.0, score)
    src.best_agreement = max(src.best_agreement, round(best, 3))
    return best


def _doi_is_anchored(src: ResolvedSource) -> bool:
    """Is ``src.doi`` traceable to something the citing paper actually printed?

    When no identifier was printed, the DOI here was inferred by title search —
    and every later index is then asked about *that* DOI, not about the cited
    work. If the search was a poor match, those lookups all succeed and three
    indices appear to independently confirm a paper the author never cited.
    A weak search result therefore cannot anchor anything downstream.
    """
    if src.identifier_printed in ("doi", "arxiv"):
        return True
    return src.search_agreement >= _CONFIRM_AGREEMENT


def resolve(ref: Reference) -> ResolvedSource:
    """Find the best URL + metadata for *ref*."""
    src = ResolvedSource()
    # Pinned before any lookup runs. Once an index has written a guess into
    # src.title, searching for *that* would just confirm the guess: a wrong
    # Crossref hit becomes an exact OpenAlex match, and two indices appear to
    # agree on a paper the author never cited.
    src.claimed_title = (ref.title or ref.raw or "")[:300]

    if ref.arxiv:
        arxiv_id = ref.arxiv.strip()
        src.arxiv_id = arxiv_id
        src.identifier_printed = "arxiv"
        src.url = f"https://arxiv.org/abs/{arxiv_id}"
        src.oa_url = f"https://arxiv.org/pdf/{arxiv_id}"
        src.resolver = "arxiv"
        src.confidence = 0.95
        _enrich_arxiv(src, arxiv_id)
    elif ref.doi:
        src.doi = ref.doi
        src.identifier_printed = "doi"
        src.url = f"https://doi.org/{ref.doi}"
        src.resolver = "doi"
        src.confidence = 0.95
    elif ref.url:
        src.url = ref.url
        src.identifier_printed = "url"
        src.resolver = "explicit-url"
        src.confidence = 0.8

    if not src.doi and ref.doi:
        src.doi = ref.doi

    # Crossref: fills metadata for a known DOI, or finds the DOI from the string.
    if src.doi:
        _enrich_crossref_by_doi(src, src.doi)
    elif not src.url or src.resolver == "explicit-url":
        _lookup_crossref(src, ref)

    # OpenAlex and Semantic Scholar each add an abstract and, crucially, an
    # open-access mirror. The abstract is the one thing that is nearly always
    # obtainable even when the publisher page is a paywall, so it is worth
    # asking more than one index before giving up on it.
    _enrich_openalex(src, ref)
    if not src.abstract or not src.oa_url:
        _enrich_semantic_scholar(src)

    # Unpaywall and Europe PMC exist purely to turn an abstract-only reference
    # into a full-text one. Europe PMC in particular hands back machine-readable
    # full text for anything deposited in PMC, which no other index here does.
    if src.doi and not src.oa_url:
        _enrich_unpaywall(src)
    if src.doi or src.title:
        _enrich_europepmc(src)

    if not src.url and src.oa_url:
        src.url = src.oa_url
    if not src.title:
        src.title = ref.title
    if not src.year:
        src.year = ref.year
    if not src.authors:
        src.authors = ref.authors

    src.landing_url = src.url
    if not src.url:
        src.notes.append("No resolvable link found for this reference.")

    _settle_existence(src)
    _describe_integrity(src)
    return src


def _settle_existence(src: ResolvedSource) -> None:
    """Decide whether this reference is a real, findable work.

    The accusation being guarded against is severe — "you cited something that
    does not exist" — so it is only ever made when an index actually answered
    and said no. If every index we asked failed to respond, the honest answer is
    that we do not know, and `existence` stays "unconfirmed".
    """
    if src.indices_hit:
        src.existence = "confirmed"
        if src.identifier_printed in ("doi", "arxiv"):
            src.identifier_resolved = True
        return

    if src.identifier_printed in ("doi", "arxiv"):
        # A printed identifier that no index recognises is a much stronger
        # signal than a title nobody matched: identifiers are registered, not
        # guessed, so a well-formed one that resolves nowhere is usually invented.
        if src.indices_missed:
            src.identifier_resolved = False
            src.existence = "not_found"
            src.notes.append(
                f"The {src.identifier_printed.upper()} printed in this entry is not "
                f"registered in {_and_list(src.indices_missed)}. A well-formed "
                "identifier that resolves nowhere is usually fabricated."
            )
        else:
            src.notes.append(
                "None of the bibliographic indices could be reached, so this "
                "reference could not be confirmed either way."
            )
        return

    if src.indices_missed and src.best_agreement < _FABRICATION_CEILING:
        src.existence = "not_found"
        src.notes.append(
            f"No record of this reference was found in {_and_list(src.indices_missed)}, "
            "and no DOI or arXiv id was printed for it. The closest thing any index "
            f"could offer scored {src.best_agreement:.2f} against the title as "
            "printed. Either the entry is too garbled to match, or the work does "
            "not exist."
        )
    elif src.indices_missed:
        # Something plausible turned up everywhere, just never convincingly.
        # Not enough to confirm, nowhere near enough to accuse.
        src.notes.append(
            "This reference could not be confidently matched to an indexed record "
            f"(best agreement {src.best_agreement:.2f}). It was not possible to "
            "confirm which work was cited — check the entry by hand."
        )
    elif src.indices_errored:
        src.notes.append(
            "None of the bibliographic indices could be reached, so this "
            "reference could not be confirmed either way."
        )


def _describe_integrity(src: ResolvedSource) -> None:
    if src.retracted:
        src.notes.append(
            "This cited work has been RETRACTED. Anything it was cited for no "
            "longer stands on its authority."
        )
    elif src.integrity:
        kinds = sorted({item["kind"] for item in src.integrity})
        src.notes.append(
            "The cited work carries a published " + _and_list(kinds, "and") + "."
        )


def _and_list(items, conjunction: str = "or") -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + f" {conjunction} " + items[-1]


def _enrich_arxiv(src: ResolvedSource, arxiv_id: str) -> None:
    resp = _get(f"http://export.arxiv.org/api/query?id_list={arxiv_id}")
    if not resp or resp.status_code != 200:
        _record(src, "arXiv", resp, found=False)
        return
    text = resp.text
    title = re.search(r"<title>(.*?)</title>", text, re.S)
    summary = re.search(r"<summary>(.*?)</summary>", text, re.S)
    # First <title> is the feed title; the entry title is the second.
    titles = re.findall(r"<title>(.*?)</title>", text, re.S)
    if len(titles) > 1:
        src.title = re.sub(r"\s+", " ", titles[1]).strip()
    elif title:
        src.title = re.sub(r"\s+", " ", title.group(1)).strip()
    if summary:
        src.abstract = re.sub(r"\s+", " ", summary.group(1)).strip()
    doi = re.search(r"<arxiv:doi[^>]*>(.*?)</arxiv:doi>", text, re.S)
    if doi and not src.doi:
        src.doi = doi.group(1).strip()

    # A withdrawn id still returns a feed entry, just an empty one, so presence
    # of the query alone proves nothing — the title is what confirms the paper.
    _record(src, "arXiv", resp, found=bool(src.title))


def _enrich_crossref_by_doi(src: ResolvedSource, doi: str) -> None:
    resp = _get(f"https://api.crossref.org/works/{requests.utils.quote(doi)}",
                params=_polite())
    if not resp or resp.status_code != 200:
        # 404 here is meaningful: Crossref is the DOI registry for scholarly
        # works, so a DOI it does not hold is very likely not a real DOI.
        _record(src, "Crossref", resp, found=False)
        return
    try:
        item = resp.json().get("message", {})
    except ValueError:
        _record(src, "Crossref", resp, found=False)
        return
    _apply_crossref_item(src, item)
    _record(src, "Crossref", resp, found=bool(item))
    src.resolver = src.resolver or "crossref-doi"


def _lookup_crossref(src: ResolvedSource, ref: Reference) -> None:
    query = (ref.title or ref.raw)[:350]
    if len(query) < 12:
        return
    resp = _get(
        "https://api.crossref.org/works",
        params=_polite({
            "query.bibliographic": query,
            "rows": 5,
            "select": "DOI,title,author,issued,container-title,abstract,URL,score,"
                      "update-to,updated-by",
        }),
    )
    if not resp or resp.status_code != 200:
        _record(src, "Crossref", resp, found=False)
        return
    try:
        items = resp.json().get("message", {}).get("items", [])
    except ValueError:
        _record(src, "Crossref", resp, found=False)
        return
    if not items:
        _record(src, "Crossref", resp, found=False)
        return

    # Crossref ranks by its own relevance score, which is not the same thing as
    # title agreement — the row that actually matches what was cited is often
    # second or third. Taking row 0 on faith both resolves to the wrong paper
    # and, worse, makes a real reference look unfindable.
    best, score = None, -1.0
    for item in items:
        candidate = _agreement(
            src, ref,
            " ".join(item.get("title") or []),
            _crossref_authors(item),
            _year_from_parts((item.get("issued") or {}).get("date-parts")),
        )
        if candidate > score:
            best, score = item, candidate
    title = " ".join((best or {}).get("title") or [])

    # Crossref returns *something* for almost any query, so a returned row is
    # not evidence the cited work exists — only a convincing title match is.
    src.search_agreement = round(score, 3)
    _record(src, "Crossref", resp, found=score >= _CONFIRM_AGREEMENT)

    # No identifier was printed, so this is a search hit, not a lookup. Say so
    # every time — a plausible-but-wrong paper is the failure mode here, and it
    # is invisible unless the report admits how the source was found.
    src.notes.append(
        f"No DOI or arXiv id was printed for this reference, so it was matched "
        f"to “{title[:70]}” by title search (agreement {score:.2f}). "
        "Check the title above matches what you cited."
    )
    if score < 0.6:
        src.notes.append(
            "That title match is weak — the retrieved source may be a different "
            "paper, in which case the verdict below is about the wrong work."
        )

    _apply_crossref_item(src, best)
    src.resolver = "crossref-search"
    src.confidence = round(min(0.9, 0.2 + score * 0.75), 2)
    if src.doi:
        src.url = f"https://doi.org/{src.doi}"


def _year_from_parts(date_parts) -> str:
    """First element of a Crossref/DataCite date-parts array, as a clean year.

    Crossref legitimately emits ``[[None]]`` for works with no known date.
    ``str(None)`` turns that into the string "None", which is truthy and then
    poisons every later ``src.year or …`` fallback — so the entry's own printed
    year never gets a chance. Validate the shape instead of trusting it.
    """
    try:
        value = date_parts[0][0]
    except (TypeError, IndexError, KeyError):
        return ""
    text = str(value).strip() if value is not None else ""
    return text if re.fullmatch(r"(?:19|20)\d{2}", text) else ""


# Crossref "updated-by" entries describe what has happened *to* this work.
# Retractions and withdrawals invalidate it outright; corrections and
# expressions of concern qualify it.
_FATAL_UPDATES = {"retraction", "withdrawal", "removal"}


def _crossref_authors(item: dict) -> str:
    names = [
        " ".join(filter(None, [a.get("given"), a.get("family")]))
        for a in (item.get("author") or [])[:6]
    ]
    return ", ".join(n for n in names if n)


def _apply_crossref_item(src: ResolvedSource, item: dict) -> None:
    if not item:
        return
    src.doi = src.doi or (item.get("DOI") or "")
    src.title = src.title or " ".join(item.get("title") or [])
    src.venue = src.venue or " ".join(item.get("container-title") or [])
    src.year = src.year or _year_from_parts((item.get("issued") or {}).get("date-parts"))
    _apply_crossref_integrity(src, item)
    authors = item.get("author") or []
    if authors and not src.authors:
        names = [
            " ".join(filter(None, [a.get("given"), a.get("family")]))
            for a in authors[:4]
        ]
        src.authors = ", ".join(n for n in names if n)
        if len(authors) > 4:
            src.authors += " et al."
    abstract = item.get("abstract") or ""
    if abstract and not src.abstract:
        src.abstract = re.sub(r"<[^>]+>", " ", abstract)
        src.abstract = re.sub(r"\s+", " ", src.abstract).strip()
    if not src.url and item.get("URL"):
        src.url = item["URL"]


def _apply_crossref_integrity(src: ResolvedSource, item: dict) -> None:
    """Record retractions and corrections published against the cited work.

    ``updated-by`` is the direction that matters: it lists notices published
    *about* this work. (``update-to`` is the mirror image — it would mean the
    cited item is itself the retraction notice, which is a legitimate thing to
    cite and so is only worth a note.)
    """
    for update in item.get("updated-by") or []:
        kind = (update.get("type") or "").replace("_", " ").strip().lower()
        if not kind:
            continue
        src.integrity.append({
            "kind": kind,
            "doi": update.get("DOI") or "",
            "label": update.get("label") or kind,
            "date": _year_from_parts((update.get("updated") or {}).get("date-parts")),
            "source": "crossref",
        })
        if kind in _FATAL_UPDATES:
            src.retracted = True

    for update in item.get("update-to") or []:
        kind = (update.get("type") or "").replace("_", " ").strip().lower()
        if kind in _FATAL_UPDATES:
            src.notes.append(
                f"This reference is itself a {kind} notice, not a research article."
            )
            break


def _enrich_openalex(src: ResolvedSource, ref: Reference) -> None:
    if src.doi:
        url = f"https://api.openalex.org/works/doi:{src.doi}"
        resp = _get(url, params=_polite())
        item = _openalex_single(resp)
        confirmed = bool(item) and _doi_is_anchored(src)
    else:
        query = (src.claimed_title or ref.title or ref.raw)[:250]
        if len(query) < 12:
            return
        resp = _get(
            "https://api.openalex.org/works",
            params=_polite({"search": query, "per-page": 3}),
        )
        item, agreement = None, -1.0
        for row in _openalex_results(resp):
            score = _agreement(
                src, ref, row.get("title") or "",
                ", ".join(
                    (a.get("author") or {}).get("display_name") or ""
                    for a in (row.get("authorships") or [])[:6]
                ),
                str(row.get("publication_year") or ""),
            )
            if score > agreement:
                item, agreement = row, score
        confirmed = bool(item) and agreement >= _CONFIRM_AGREEMENT
        if item and agreement < 0.45:
            _record(src, "OpenAlex", resp, found=False)
            return

    _record(src, "OpenAlex", resp, found=confirmed)
    if not item:
        return

    # OpenAlex flags retractions directly, and is often faster to update than
    # the Crossref record is.
    if item.get("is_retracted"):
        src.retracted = True
        if not any(i["source"] == "openalex" for i in src.integrity):
            src.integrity.append({
                "kind": "retraction",
                "doi": (item.get("doi") or "").replace("https://doi.org/", ""),
                "label": "Marked as retracted in OpenAlex",
                "date": str(item.get("publication_year") or ""),
                "source": "openalex",
            })

    src.title = src.title or (item.get("title") or "")
    src.doi = src.doi or (item.get("doi") or "").replace("https://doi.org/", "")
    if not src.year and item.get("publication_year"):
        src.year = str(item["publication_year"])

    best_oa = item.get("best_oa_location") or item.get("primary_location") or {}
    oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url") or ""
    if oa_url and not src.oa_url:
        src.oa_url = oa_url
    if not src.url and oa_url:
        src.url = oa_url

    if not src.abstract:
        src.abstract = _inverted_index_to_text(item.get("abstract_inverted_index"))
    src.resolver = src.resolver or "openalex"


def _enrich_semantic_scholar(src: ResolvedSource) -> None:
    """Last stop for an abstract and an open-access PDF link."""
    if src.doi:
        key = f"DOI:{src.doi}"
    elif src.arxiv_id:
        key = f"ARXIV:{src.arxiv_id}"
    elif src.title:
        key = ""
    else:
        return

    fields = "title,abstract,openAccessPdf,year,authors,venue,externalIds"
    if key:
        resp = _get(f"https://api.semanticscholar.org/graph/v1/paper/{key}",
                    params={"fields": fields})
        item = _json_or_none(resp)
        confirmed = bool(item) and _doi_is_anchored(src)
    else:
        resp = _get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": (src.claimed_title or src.title)[:200], "limit": 1,
                    "fields": fields},
        )
        payload = _json_or_none(resp) or {}
        item, agreement = None, -1.0
        for row in payload.get("data") or []:
            score = _agreement(
                src, None, row.get("title") or "",
                ", ".join(a.get("name") or "" for a in (row.get("authors") or [])[:6]),
                str(row.get("year") or ""),
            )
            if score > agreement:
                item, agreement = row, score
        confirmed = bool(item) and agreement >= _CONFIRM_AGREEMENT
        if item and agreement < 0.45:
            _record(src, "Semantic Scholar", resp, found=False)
            return

    _record(src, "Semantic Scholar", resp, found=confirmed)
    if not item:
        return

    if not src.abstract and item.get("abstract"):
        src.abstract = item["abstract"].strip()
    oa = (item.get("openAccessPdf") or {}).get("url")
    if oa and not src.oa_url:
        src.oa_url = oa
        src.notes.append("Found an open-access copy via Semantic Scholar.")
    if not src.title:
        src.title = item.get("title") or ""
    if not src.year and item.get("year"):
        src.year = str(item["year"])
    pmcid = (item.get("externalIds") or {}).get("PubMedCentral")
    if pmcid and not src.pmcid:
        src.pmcid = f"PMC{pmcid}" if not str(pmcid).upper().startswith("PMC") else str(pmcid)


def _enrich_unpaywall(src: ResolvedSource) -> None:
    """Ask Unpaywall for a legal open-access copy of a DOI.

    Unpaywall has the broadest open-access coverage of anything here, but it
    refuses to answer without a contact address, so this is a no-op until
    CITECHECK_CONTACT_EMAIL is set.
    """
    if not _CONTACT_EMAIL or not src.doi:
        return

    resp = _get(
        f"https://api.unpaywall.org/v2/{requests.utils.quote(src.doi)}",
        params={"email": _CONTACT_EMAIL},
    )
    item = _json_or_none(resp)
    _record(src, "Unpaywall", resp, found=bool(item))
    if not item:
        return

    location = item.get("best_oa_location") or {}
    oa_url = location.get("url_for_pdf") or location.get("url") or ""
    if oa_url and not src.oa_url:
        src.oa_url = oa_url
        src.notes.append(
            f"Found an open-access copy via Unpaywall ({item.get('oa_status') or 'oa'})."
        )
    if not src.title:
        src.title = item.get("title") or ""


def _enrich_europepmc(src: ResolvedSource) -> None:
    """Europe PMC — the only index here that serves machine-readable full text.

    For anything deposited in PMC this replaces an abstract-only check with the
    complete article, which is the difference between judging a paper by its
    blurb and actually reading it.
    """
    claimed = src.claimed_title or src.title
    if src.doi:
        query = f'DOI:"{src.doi}"'
    elif claimed and len(claimed) > 15:
        query = f'TITLE:"{claimed[:180]}"'
    else:
        return

    resp = _get(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={"query": query, "format": "json", "resultType": "core", "pageSize": 1},
    )
    payload = _json_or_none(resp) or {}
    results = (payload.get("resultList") or {}).get("result") or []
    item = results[0] if results else None

    if item and not src.doi:
        # Title-matched rather than looked up, so it still has to be the right paper.
        if _agreement(src, None, item.get("title") or "",
                      item.get("authorString") or "",
                      str(item.get("pubYear") or "")) < _CONFIRM_AGREEMENT:
            _record(src, "Europe PMC", resp, found=False)
            return

    _record(src, "Europe PMC", resp, found=bool(item) and (not src.doi or _doi_is_anchored(src)))
    if not item:
        return

    if not src.abstract and item.get("abstractText"):
        src.abstract = re.sub(r"<[^>]+>", " ", item["abstractText"])
        src.abstract = re.sub(r"\s+", " ", src.abstract).strip()

    pmcid = item.get("pmcid") or ""
    if pmcid and not src.pmcid:
        src.pmcid = pmcid

    # Open-access subset only: the full-text endpoint 403s for everything else.
    if pmcid and (item.get("isOpenAccess") == "Y" or item.get("inEPMC") == "Y"):
        full_text = (
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
        )
        if not src.oa_url or "fullTextXML" not in src.oa_url:
            src.oa_url = full_text
            src.notes.append("Full text retrieved from Europe PMC (open-access subset).")

    if item.get("commentCorrectionList"):
        for correction in item["commentCorrectionList"].get("commentCorrection") or []:
            kind = (correction.get("type") or "").strip().lower()
            if "retraction" not in kind:
                continue
            src.retracted = True
            src.integrity.append({
                "kind": "retraction",
                "doi": "",
                "label": correction.get("type") or "Retraction in PubMed",
                "date": str(correction.get("year") or ""),
                "source": "europepmc",
            })


def _json_or_none(resp) -> dict | None:
    if not resp or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _openalex_single(resp) -> dict | None:
    if not resp or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _openalex_results(resp) -> list[dict]:
    data = _openalex_single(resp)
    return (data or {}).get("results") or []


def _inverted_index_to_text(index: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the prose."""
    if not index:
        return ""
    slots: list[tuple[int, str]] = []
    for word, positions in index.items():
        for pos in positions:
            slots.append((pos, word))
    slots.sort()
    return " ".join(word for _, word in slots)[:4000]


def _title_agreement(a: str, b: str) -> float:
    """Symmetric token overlap (Jaccard) between two titles.

    Deliberately symmetric. Dividing by the *smaller* token set — the obvious
    first instinct — scores "A Scalable Location Service for Geographic Ad Hoc
    Routing" against "A Scalable Security Service for Geographic Ad-Hoc Routing"
    at 0.83 and accepts a completely different paper. Counting the tokens each
    side has that the other lacks makes that single decisive word count.
    """
    def toks(text: str) -> set[str]:
        return {
            t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
            if t not in {"the", "and", "for", "with", "from", "into", "using"}
        }

    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
