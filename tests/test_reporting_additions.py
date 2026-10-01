"""Coverage, run settings, retention and the evaluation metrics.

Each of these backs a statement a reader relies on: what a verdict rested on,
under which settings a timing was taken, when a manuscript leaves the server,
and how an accuracy figure was computed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from citecheck import match, pipeline
from tools import evaluate


def _entry(kind: str = "", chars: int = 0) -> dict:
    return {"key": "1", "verdict": "supported", "flags": [],
            "fetched": {"kind": kind, "text_chars": chars} if kind else {}}


class CoverageTest(unittest.TestCase):
    def test_each_kind_lands_in_its_category(self):
        cases = {
            ("fulltext-xml", 9000): "full_text",
            ("pdf", 40000): "full_text",
            ("html", 3000): "html_page",
            ("abstract", 1200): "abstract_only",
            ("supplied pdf", 5000): "supplied",
            ("supplied text", 500): "supplied",
            ("html", 0): "none",
            ("", 0): "none",
        }
        for (kind, chars), expected in cases.items():
            with self.subTest(kind=kind, chars=chars):
                self.assertEqual(pipeline.coverage_of(_entry(kind, chars)), expected)

    def test_summary_counts_every_category(self):
        report = pipeline.summarise({"stats": {"references_checked": 3},
                                     "references": [_entry("pdf", 10), _entry("abstract", 10), _entry()]})
        self.assertEqual(report["stats"]["coverage"], {
            "full_text": 1, "html_page": 0, "abstract_only": 1, "supplied": 0, "none": 1,
        })


class RunConfigTest(unittest.TestCase):
    def test_records_settings_without_the_email_itself(self):
        options = pipeline.Options(workers=3, use_model=False, contact_email="a@b.org")
        config = pipeline.run_config(options, time.time())
        self.assertEqual(config["workers"], 3)
        self.assertFalse(config["use_model"])
        self.assertEqual(config["model"], "")
        self.assertTrue(config["contact_email_set"])
        self.assertNotIn("a@b.org", json.dumps(config))

    def test_model_name_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"CITECHECK_OPENAI_MODEL": ""}):
            self.assertEqual(match.model_name(), match.DEFAULT_MODEL)
        with mock.patch.dict(os.environ, {"CITECHECK_OPENAI_MODEL": "gpt-4.1"}):
            self.assertEqual(match.model_name(), "gpt-4.1")


class RetentionTest(unittest.TestCase):
    """Run against temporary directories, never the real run store."""

    @classmethod
    def setUpClass(cls):
        with mock.patch("citecheck.shots.browser_status", return_value=(False, "test")):
            import app
        cls.app = app

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs, self.uploads = root / "runs", root / "uploads"
        self.runs.mkdir()
        self.uploads.mkdir()
        patches = [mock.patch.object(self.app, "RUNS_DIR", self.runs),
                   mock.patch.object(self.app, "UPLOADS_DIR", self.uploads)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _make(self, run_id: str, age_hours: float) -> None:
        run = self.runs / run_id
        run.mkdir()
        (run / "report.json").write_text("{}", encoding="utf-8")
        upload = self.uploads / f"{run_id}_paper.pdf"
        upload.write_bytes(b"%PDF-")
        stamp = time.time() - age_hours * 3600
        for path in (run, run / "report.json", upload):
            os.utime(path, (stamp, stamp))

    def test_only_expired_runs_are_removed(self):
        self._make("20260101-000000-aaaaaa", age_hours=48)
        self._make("20260102-000000-bbbbbb", age_hours=1)
        (self.runs / "keep-me").mkdir()          # not a generated name
        removed = self.app.purge_expired(hours=24)
        self.assertEqual(removed, ["20260101-000000-aaaaaa"])
        self.assertFalse((self.runs / "20260101-000000-aaaaaa").exists())
        self.assertFalse(any(self.uploads.glob("20260101-000000-aaaaaa_*")))
        self.assertTrue((self.runs / "20260102-000000-bbbbbb").exists())
        self.assertTrue((self.runs / "keep-me").exists())

    def test_zero_hours_keeps_everything(self):
        self._make("20260101-000000-aaaaaa", age_hours=1000)
        self.assertEqual(self.app.purge_expired(hours=0), [])

    def test_a_run_in_progress_is_never_purged(self):
        self._make("20260101-000000-aaaaaa", age_hours=48)
        with mock.patch.dict(self.app._RUNS, {"20260101-000000-aaaaaa": {"done": False}}):
            self.assertEqual(self.app.purge_expired(hours=24), [])

    def test_delete_endpoint_removes_the_run(self):
        self._make("20260101-000000-aaaaaa", age_hours=0)
        client = self.app.app.test_client()
        reply = client.delete("/api/run/20260101-000000-aaaaaa")
        self.assertEqual(reply.status_code, 200)
        self.assertFalse((self.runs / "20260101-000000-aaaaaa").exists())
        self.assertEqual(client.delete("/api/run/20260101-000000-aaaaaa").status_code, 404)
        self.assertEqual(client.delete("/api/run/..").status_code, 404)


class EvaluationMetricsTest(unittest.TestCase):
    def test_marker_precision_and_recall(self):
        rows = [
            {"pred_key": "1", "h_is_citation": "y"},
            {"pred_key": "2", "h_is_citation": "y"},
            {"pred_key": "9", "h_is_citation": "n"},     # false positive
            {"pred_key": "", "h_is_citation": "y"},      # missed marker
            {"pred_key": "3", "h_is_citation": ""},      # unlabelled, ignored
        ]
        out = evaluate.marker_metrics(rows)
        self.assertEqual((out["tp"], out["fp"], out["fn"]), (2, 1, 1))
        self.assertAlmostEqual(out["precision"], 2 / 3, places=3)
        self.assertAlmostEqual(out["recall"], 2 / 3, places=3)

    def test_no_labels_means_no_number(self):
        self.assertIsNone(evaluate.marker_metrics([])["precision"])
        self.assertIsNone(evaluate.support_metrics([])["kappa"])

    def test_kappa_known_values(self):
        self.assertEqual(evaluate.cohen_kappa([("a", "a"), ("b", "b")]), 1.0)
        pairs = [("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]
        self.assertEqual(evaluate.cohen_kappa(pairs), 0.0)

    def test_existence_counted_once_per_reference(self):
        rows = [
            {"run_id": "r", "ref_key": "1", "pred_existence": "not_found", "h_exists": "y"},
            {"run_id": "r", "ref_key": "1", "pred_existence": "not_found", "h_exists": "y"},
            {"run_id": "r", "ref_key": "2", "pred_existence": "confirmed", "h_exists": "y"},
            {"run_id": "r", "ref_key": "3", "pred_existence": "not_found", "h_exists": "n"},
        ]
        out = evaluate.existence_metrics(rows)
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["false_no_match_rate"], 0.5)
        self.assertEqual(out["fabrication_detection_rate"], 1.0)



class ThrottledIndexTest(unittest.TestCase):
    """An index that says "not now" has not said "no such work"."""

    def _resp(self, status, headers=None):
        return mock.Mock(status_code=status, headers=headers or {})

    def test_throttled_and_failing_answers_are_not_misses(self):
        from citecheck import resolve
        for status in (429, 500, 503):
            with self.subTest(status=status):
                src = resolve.ResolvedSource()
                resolve._record(src, "Crossref", self._resp(status), found=False)
                self.assertEqual(src.indices_errored, ["Crossref"])
                self.assertEqual(src.indices_missed, [])

    def test_a_clean_404_is_still_a_miss(self):
        from citecheck import resolve
        src = resolve.ResolvedSource()
        resolve._record(src, "Crossref", self._resp(404), found=False)
        self.assertEqual(src.indices_missed, ["Crossref"])

    def test_all_indexes_throttled_leaves_the_reference_unconfirmed(self):
        from citecheck import resolve
        src = resolve.ResolvedSource()
        for index in ("Crossref", "OpenAlex", "Europe PMC"):
            resolve._record(src, index, self._resp(429), found=False)
        resolve._settle_existence(src)
        self.assertEqual(src.existence, "unconfirmed")

    def test_throttled_request_is_retried(self):
        from citecheck import resolve
        replies = [self._resp(429, {"Retry-After": "1"}), self._resp(200)]
        with mock.patch("citecheck.resolve.requests.get", side_effect=replies) as get, \
             mock.patch("citecheck.resolve.time.sleep") as sleep:
            resp = resolve._get("https://api.crossref.org/works")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()


class MetadataMismatchTest(unittest.TestCase):
    """An entry whose authors or year disagree with the published record."""

    def flags(self, raw, records, existence="confirmed"):
        from citecheck import crosscheck, refs, resolve

        (reference,) = refs.parse_references("[1] " + raw + "\n[2] A. Other, \"Padding entry title here,\" J., 2001.")[:1]
        source = resolve.ResolvedSource(existence=existence, records=records)
        return {f.kind: f.message for f in crosscheck.metadata_flags(source, reference)}

    RAW = ('B. Zheng and R. Zhang, "Intelligent reflecting surface-enhanced OFDM," '
           "IEEE Wireless Commun. Lett., vol. 9, no. 4, pp. 518-522, Apr. 2020.")
    RECORD = {"index": "Crossref", "title": "Intelligent reflecting surface-enhanced OFDM",
              "title_agreement": 1.0, "year": "2020", "surnames": ["Zheng", "Zhang"]}

    def test_matching_entry_raises_nothing(self):
        self.assertEqual(self.flags(self.RAW, [self.RECORD]), {})

    def test_wrong_surname_is_reported(self):
        flags = self.flags(self.RAW.replace("B. Zheng", "B. Zhang"), [self.RECORD])
        self.assertIn("author-name-mismatch", flags)
        self.assertIn("Zheng", flags["author-name-mismatch"])

    def test_wrong_year_is_reported(self):
        flags = self.flags(self.RAW.replace("2020", "2016"), [self.RECORD])
        self.assertEqual(list(flags), ["year-mismatch"])
        self.assertIn("2016", flags["year-mismatch"])

    def test_one_year_apart_is_a_preprint_not_an_error(self):
        self.assertEqual(self.flags(self.RAW.replace("2020", "2019"), [self.RECORD]), {})

    def test_transliterated_and_accented_names_match(self):
        raw = 'T. Rocktaschel and J. Mueller, "End-to-end differentiable proving at scale," NeurIPS, 2017.'
        record = dict(self.RECORD, title="End-to-end differentiable proving at scale",
                      year="2017", surnames=["Rocktäschel", "Müller"])
        self.assertEqual(self.flags(raw, [record]), {})

    def test_et_al_entry_is_only_held_to_its_first_author(self):
        raw = 'B. Zheng et al., "Intelligent reflecting surface-enhanced OFDM," IEEE WCL, 2020.'
        record = dict(self.RECORD, surnames=["Zheng", "Zhang", "You", "Wu"])
        self.assertEqual(self.flags(raw, [record]), {})

    def test_record_for_another_work_is_not_compared(self):
        record = dict(self.RECORD, title_agreement=0.3, year="2001", surnames=["Nobody"])
        self.assertEqual(self.flags(self.RAW, [record]), {})

    def test_unconfirmed_reference_is_not_compared(self):
        record = dict(self.RECORD, year="2001")
        self.assertEqual(self.flags(self.RAW, [record], existence="unconfirmed"), {})

    def test_two_years_apart_is_still_a_preprint(self):
        self.assertEqual(self.flags(self.RAW.replace("2020", "2018"), [self.RECORD]), {})

    def test_preprint_entry_is_not_dated_by_the_journal_version(self):
        raw = ('J. Chen and W. Yu, "Channel estimation for reconfigurable intelligent surface '
               'aided multi-user MIMO systems," arXiv preprint arXiv:1912.03619, 2019.')
        record = dict(self.RECORD, index="OpenAlex", year="2023", surnames=["Chen", "Yu"])
        self.assertEqual(self.flags(raw, [record]), {})

    def test_arxiv_id_is_not_read_as_the_year(self):
        from citecheck import refs

        reference = refs.parse_references(
            '[1] B. Zheng and R. Zhang, "Intelligent reflecting surface assisted OFDMA," '
            "arXiv preprint arXiv:2003.00648, 2020.\n"
            '[2] A. Other, "Padding entry title here," J., 2001.'
        )[0]
        self.assertEqual(reference.year, "2020")

    def test_book_editions_are_not_year_errors(self):
        raw = "S. M. Kay, Fundamentals of statistical signal processing. Prentice Hall PTR, 1993."
        record = dict(self.RECORD, year="2006", surnames=["Kay"], book=True)
        self.assertEqual(self.flags(raw, [record]), {})

    def test_other_work_under_the_same_title_is_one_finding(self):
        raw = "S. M. Kay, Fundamentals of digital image processing. Journal of Imaging, 1993."
        record = dict(self.RECORD, index="OpenAlex", year="2009", surnames=["Dougherty"])
        flags = self.flags(raw, [record])
        self.assertEqual(list(flags), ["record-mismatch"])
        self.assertIn("Dougherty", flags["record-mismatch"])
