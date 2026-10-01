"""What a reference is reported as when the lookup does not go cleanly.

Each case here was a wrong report on a real manuscript before it was a rule:
a mistyped DOI reported as an invented paper, a company's product page
"checked" against an unrelated book chapter, a site's bot check reported as
there being nothing to read.

No network: the index and site replies are canned.
"""

from __future__ import annotations

import unittest
from unittest import mock

from citecheck import crosscheck, fetch, match, refs, resolve


class _Reply:
    def __init__(self, status=200, payload=None, text="", headers=None, url="https://example.org/x"):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = headers or {"Content-Type": "text/html"}
        self.url = url
        self.encoding = "utf-8"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, _size):
        yield self.text.encode("utf-8")

    def close(self):
        pass


def _crossref_item(doi, title, family, year):
    return {"DOI": doi, "title": [title], "author": [{"given": "A.", "family": family}],
            "issued": {"date-parts": [[year]]}}


def _reference(raw):
    return refs.parse_references("[1] " + raw + '\n[2] A. Other, "Padding entry title here," J., 2001.')[0]


def _indexes(by_doi=None, by_title=None, handles=None):
    """A stand-in for `resolve._get` covering Crossref and the DOI registry."""
    by_doi, by_title, handles = by_doi or {}, by_title or [], handles or {}

    def get(url, **kwargs):
        if "doi.org/api/handles/" in url:
            doi = url.split("/handles/", 1)[1].replace("%2F", "/")
            return _Reply(payload={"responseCode": 1 if handles.get(doi) else 100})
        if "api.crossref.org/works/" in url:
            doi = url.split("/works/", 1)[1].replace("%2F", "/")
            item = by_doi.get(doi)
            return _Reply(payload={"message": item}) if item else _Reply(status=404)
        if "api.crossref.org/works" in url:
            return _Reply(payload={"message": {"items": by_title}})
        return _Reply(status=404)

    return get


class WrongIdentifierTest(unittest.TestCase):
    RAW = ('K. Almeman, "Automatically building VoIP speech parallel corpora for Arabic dialects," '
           "ACM Trans. Asian Low-Resour. Lang. Inf. Process., vol. 16, no. 4, 2017, doi: 10.1145/3067280")
    REAL = _crossref_item("10.1145/3132708",
                          "Automatically Building VoIP Speech Parallel Corpora for Arabic Dialects",
                          "Almeman", 2017)

    def resolve(self, raw, **indexes):
        with mock.patch.object(resolve, "_get", _indexes(**indexes)):
            return resolve.resolve(_reference(raw))

    def test_unregistered_doi_on_a_real_paper_is_a_wrong_doi_not_a_missing_paper(self):
        source = self.resolve(self.RAW, by_title=[self.REAL])
        self.assertEqual(source.existence, "confirmed")
        self.assertEqual(source.doi, "10.1145/3132708")
        self.assertEqual(source.identifier_wrong, "10.1145/3067280")
        self.assertIs(source.identifier_resolved, False)
        kinds = [f.kind for f in crosscheck.source_flags(source)]
        self.assertEqual(kinds, ["identifier-mismatch"])

    def test_unregistered_doi_with_no_such_title_is_still_not_found(self):
        source = self.resolve(self.RAW, by_title=[
            _crossref_item("10.1/zzz", "Soil moisture retrieval over croplands", "Nobody", 2011)])
        self.assertEqual(source.existence, "not_found")
        self.assertEqual(source.identifier_wrong, "")

    def test_doi_registered_outside_crossref_is_confirmed(self):
        source = self.resolve(self.RAW, handles={"10.1145/3067280": True})
        self.assertEqual(source.existence, "confirmed")
        self.assertIn("DOI registry", source.indices_hit)

    def test_doi_of_a_different_paper_is_reported_and_the_right_one_found(self):
        other = _crossref_item("10.1145/3067280", "An Arabic event coreference dataset and benchmarks",
                               "Almeman", 2025)
        source = self.resolve(self.RAW, by_doi={"10.1145/3067280": other}, by_title=[self.REAL])
        self.assertEqual(source.doi, "10.1145/3132708")
        self.assertIn("event coreference", source.identifier_points_to)
        (flag,) = crosscheck.source_flags(source)
        self.assertEqual(flag.kind, "identifier-mismatch")
        self.assertIn("different work", flag.message)

    def test_matching_doi_raises_nothing(self):
        mine = dict(self.REAL, DOI="10.1145/3067280")
        source = self.resolve(self.RAW, by_doi={"10.1145/3067280": mine})
        self.assertEqual(source.identifier_wrong, "")
        self.assertEqual(crosscheck.source_flags(source), [])


class WeakMatchIsNotTheSourceTest(unittest.TestCase):
    def resolve(self, raw, items):
        with mock.patch.object(resolve, "_get", _indexes(by_title=items)):
            return resolve.resolve(_reference(raw))

    def test_web_reference_keeps_its_own_address(self):
        source = self.resolve(
            'Sembly AI, "Agentic augmentation platform for professional services," 2026. '
            "[Online]. Available: https://www.sembly.ai",
            [_crossref_item("10.1007/x", "Databricks as an agentic platform", "Someone", 2025)],
        )
        self.assertEqual(source.url, "https://www.sembly.ai")
        self.assertEqual(source.doi, "")
        # No index lists a product page, so the indexes cannot say it is missing.
        self.assertEqual(source.existence, "unconfirmed")

    def test_web_reference_exists_if_its_address_loads(self):
        source = resolve.ResolvedSource(identifier_printed="url", url="https://www.sembly.ai")
        resolve.mark_web_source(source, loaded=True)
        self.assertEqual(source.existence, "confirmed")
        gone = resolve.ResolvedSource(identifier_printed="url", url="https://nope.invalid")
        resolve.mark_web_source(gone, loaded=False, dead="no such host")
        self.assertEqual(gone.existence, "not_found")
        unreachable = resolve.ResolvedSource(identifier_printed="url")
        resolve.mark_web_source(unreachable, loaded=False)
        self.assertEqual(unreachable.existence, "unconfirmed")

    def test_same_vocabulary_by_other_authors_is_not_the_cited_paper(self):
        source = self.resolve(
            'E. J. Hu et al., "LoRA: Low-rank adaptation of large language models," in Proc. ICLR, 2022',
            [_crossref_item("10.1049/x", "ST-LoRA: SVD-guided sparse low-rank adaptation of large language models",
                            "Zhang", 2025)],
        )
        self.assertEqual(source.doi, "")
        self.assertNotEqual(source.existence, "confirmed")
        # ...but something that close is not grounds for calling it invented.
        self.assertNotEqual(source.existence, "not_found")

    def test_shared_surname_and_year_cannot_rescue_a_different_title(self):
        source = self.resolve(
            'A. Ali and S. Aldarmaki, "Mixat: A benchmark corpus for Emirati Arabic-English '
            'code-switched speech recognition," in Proc. EMNLP, 2024',
            [_crossref_item("10.1016/x", "ArzEn-LLM: Code-switched Egyptian Arabic-English translation "
                            "and speech recognition using LLMs", "Ali", 2024)],
        )
        self.assertEqual(source.doi, "")

    def test_a_standard_absent_from_the_indexes_is_not_reported_missing(self):
        source = self.resolve(
            "International Organization for Standardization, Information Technology — Artificial "
            "Intelligence — Management System (ISO/IEC 42001:2023), ISO Standard, 2023",
            [_crossref_item("10.1/x", "Soil moisture retrieval over croplands", "Nobody", 2011)],
        )
        self.assertEqual(source.existence, "unconfirmed")


class OrganisationAuthoredEntryTest(unittest.TestCase):
    def test_the_body_is_the_author_and_the_document_is_the_title(self):
        ref = _reference(
            "International Organization for Standardization, Information Security Management "
            "Systems — Requirements (ISO/IEC 27001:2022), ISO Standard, 2022"
        )
        self.assertEqual(ref.authors, "International Organization for Standardization")
        self.assertTrue(ref.title.startswith("Information Security Management Systems"))


class BlockedTest(unittest.TestCase):
    def fetch(self, reply):
        with mock.patch.object(fetch.requests, "get", lambda *a, **k: reply):
            return fetch._fetch_one("https://publisher.example/paper")

    def test_refusal_is_blocked(self):
        got = self.fetch(_Reply(status=403, text="Forbidden"))
        self.assertTrue(got.blocked)
        self.assertFalse(got.ok)

    def test_challenge_page_is_blocked(self):
        got = self.fetch(_Reply(text="<html><body>Making sure you're not a bot! Loading...</body></html>"))
        self.assertTrue(got.blocked)
        got = self.fetch(_Reply(status=202, text=""))
        self.assertTrue(got.blocked)

    def test_missing_page_is_dead_not_blocked(self):
        got = self.fetch(_Reply(status=404, text="Not found"))
        self.assertFalse(got.blocked)
        self.assertEqual(got.dead, "HTTP 404")

    def test_ordinary_page_is_neither(self):
        got = self.fetch(_Reply(text="<html><body><article>" + "Real prose. " * 200 + "</article></body></html>"))
        self.assertFalse(got.blocked)
        self.assertTrue(got.ok)

    def test_blocked_with_an_abstract_is_still_judged_on_the_abstract(self):
        source = resolve.ResolvedSource(url="https://publisher.example/paper", abstract="An abstract. " * 40)
        with mock.patch.object(fetch.requests, "get", lambda *a, **k: _Reply(status=403)):
            got = fetch.fetch_source(source)
        self.assertTrue(got.blocked)
        self.assertEqual(got.kind, "abstract")
        self.assertTrue(got.ok)

    def test_blocked_is_a_verdict_and_ranks_with_unverified(self):
        self.assertIn("blocked", match.VERDICTS)
        self.assertEqual(match.concern("blocked"), match.concern("unverified"))


if __name__ == "__main__":
    unittest.main()
