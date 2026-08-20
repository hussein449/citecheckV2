"""What each reference is actually asked to support, and re-checking one of them.

Two things are tested here, and they are the same thing seen twice: a verdict is
only as good as the claim it was measured against.

  * A sentence that cites five sources makes five claims, not one. Handing the
    whole sentence to each source asks every one of them to support the other
    four's content, and each comes back "weak" for failing to.
  * When a verdict is wrong anyway — the wrong paper resolved, a paywall read
    instead of an article — the reader can re-run that one reference against a
    document of their own, and the report's headline has to move with it.

Nothing here touches the network or a browser.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citecheck import intext, match, pipeline, refs


class ClaimScopeTest(unittest.TestCase):
    """A marker answers for its own clause, not for the whole sentence."""

    SHARED = (
        "By applying ML techniques to real-time data gathered by UAVs, parameters "
        "such as plant disease detection and soil moisture [37], minimum and "
        "maximum temperatures at field level [38], and the level of phosphorus in "
        "the soil [39] can be predicted."
    )

    def claims(self, text):
        return {c.key: c.claim for c in intext.extract_citations(text)}

    def test_each_marker_takes_only_its_own_clause(self):
        claims = self.claims(self.SHARED)
        self.assertIn("soil moisture", claims["37"])
        self.assertNotIn("phosphorus", claims["37"])

        self.assertIn("temperatures", claims["38"])
        self.assertNotIn("soil moisture", claims["38"])
        self.assertNotIn("phosphorus", claims["38"])

        # The last marker keeps the trailing predicate the whole list depends on.
        self.assertIn("phosphorus", claims["39"])
        self.assertIn("can be predicted", claims["39"])

    def test_a_lone_marker_keeps_the_whole_sentence(self):
        text = "Drones cut delivery time in dense urban areas by a third [7]."
        citation = intext.extract_citations(text)[0]
        self.assertEqual(citation.claim, citation.sentence)

    def test_a_clause_too_thin_to_stand_alone_falls_back(self):
        """Half a claim is worse evidence than a shared one."""
        text = "Several surveys [1] and reviews [2] describe the same trend."
        claims = self.claims(text)
        self.assertIn("describe the same trend", claims["1"])

    def test_the_full_sentence_is_kept_as_context(self):
        citations = intext.extract_citations(self.SHARED)
        for citation in citations:
            self.assertIn("By applying ML techniques", citation.sentence)

    def test_group_citations_record_how_many_share_the_claim(self):
        text = "Table 1 classifies UAVs by size, altitude [23-28], weight and range."
        citations = intext.extract_citations(text)
        self.assertEqual(len(citations), 6)
        for citation in citations:
            self.assertEqual(citation.group_size, 6)


class MarkerPrecisionTest(unittest.TestCase):
    """Bracketed numbers and parenthesised years that are not citations."""

    def keys(self, text):
        return sorted({c.key for c in intext.extract_citations(text)})

    def test_entry_zero_is_arithmetic_not_a_citation(self):
        text = "Scores were normalised to the range [0,1] before training [4]."
        self.assertEqual(self.keys(text), ["1", "4"])

    def test_a_label_is_never_a_surname(self):
        text = (
            "Table 3 (2019) lists the fleet. Figure 2 (2021) plots the same data. "
            "Bosona (2020) reports the underlying survey."
        )
        self.assertEqual(self.keys(text), ["bosona2020"])

    def test_table_rows_are_kept_but_marked_as_non_prose(self):
        """A reference cited only from a table is still worth checking."""
        row = "[131] Windows Red Hat OPNET Genetic Algorithm 2019"
        citations = intext.extract_citations(row)
        self.assertEqual([c.key for c in citations], ["131"])
        self.assertFalse(citations[0].prose)

    def test_prose_survives_a_dense_run_of_markers(self):
        text = "Several independent trials of the same protocol agree [1], [2], [3], [4]."
        citations = intext.extract_citations(text)
        self.assertTrue(citations)
        self.assertTrue(all(c.prose for c in citations), "prose misread as a table row")


class SentenceOffsetTest(unittest.TestCase):
    """Offsets have to index the text as given, or page numbers drift."""

    def test_offsets_survive_line_breaks(self):
        text = "First line.\n\nSecond line here.\n\nThe drone flew far [3]."
        for offset, sentence in intext.split_sentences(text):
            self.assertEqual(
                text[offset : offset + 5].strip(), sentence[:5].strip(),
                f"offset {offset} does not point at {sentence[:20]!r}",
            )

    def test_page_lookup_gets_the_later_page(self):
        """A flattened offset lands short, and lands shorter the further in it is."""
        page_one = "Filler sentence. " * 40
        page_two = "The trial found a clear benefit [9]."
        # Two pages joined the way `pdf_parse` joins them: blank lines between
        # blocks, which is exactly what a flattening splitter loses.
        text = "\n\n".join([page_one] * 6) + "\n\n" + page_two
        boundary = len(text) - len(page_two)

        def page_of(offset):
            return 1 if offset < boundary else 2

        citations = intext.extract_citations(text, page_of)
        self.assertEqual([c.page for c in citations], [2])


class OutOfRangeMarkerTest(unittest.TestCase):
    """A number past the end of the bibliography cites nothing."""

    def test_row_labels_are_reported_apart_from_real_orphans(self):
        bibliography = [
            refs.Reference(key=str(n), raw=f"Entry {n}", number=n) for n in (1, 2, 3, 4)
        ]
        unmatched, out_of_range = pipeline._split_out_of_range(["3", "126", "131"], bibliography)
        self.assertEqual(unmatched, ["3"])
        self.assertEqual(out_of_range, ["126", "131"])

    def test_an_unnumbered_bibliography_gives_no_ceiling_to_guess_at(self):
        bibliography = [refs.Reference(key="bosona2020", raw="Bosona, T. (2020).")]
        unmatched, out_of_range = pipeline._split_out_of_range(["999"], bibliography)
        self.assertEqual(unmatched, ["999"])
        self.assertEqual(out_of_range, [])

    def test_a_half_parsed_bibliography_discards_nothing(self):
        """Otherwise a parsing failure silently becomes a loss of coverage."""
        # 40 entries recovered out of a list that plainly runs to 150.
        recovered = [
            refs.Reference(key=str(n), raw=f"Entry {n}", number=n)
            for n in range(1, 150, 4)
        ]
        unmatched, out_of_range = pipeline._split_out_of_range(["61", "148"], recovered)
        self.assertEqual(unmatched, ["61", "148"])
        self.assertEqual(out_of_range, [])


class ClaimsForTest(unittest.TestCase):
    """Prose is what gets judged where there is any."""

    def cite(self, sentence, claim="", prose=True, group=1, page=1):
        return intext.Citation(
            key="12", label="[12]", style="numeric", sentence=sentence,
            claim=claim or sentence, line=sentence, page=page, char_offset=0,
            group_size=group, prose=prose,
        )

    def test_a_table_row_is_ignored_when_prose_exists(self):
        claims = pipeline._claims_for([
            self.cite("[12] OPNET Genetic Algorithm 2019", prose=False),
            self.cite("Genetic algorithms shorten the route by a fifth [12]."),
        ])
        self.assertEqual(len(claims), 1)
        self.assertIn("shorten the route", claims[0].text)

    def test_a_table_row_is_used_when_it_is_all_there_is(self):
        claims = pipeline._claims_for([
            self.cite("[12] OPNET Genetic Algorithm 2019", prose=False),
        ])
        self.assertEqual(len(claims), 1)

    def test_context_and_co_citation_reach_the_judge(self):
        claims = pipeline._claims_for([
            self.cite("A and B and C [12].", claim="C [12]", group=3, page=7),
        ])
        self.assertEqual(claims[0].text, "C [12]")
        self.assertEqual(claims[0].context, "A and B and C [12].")
        self.assertEqual(claims[0].co_cited, 3)
        self.assertEqual(claims[0].page, 7)


class ClaimNormalisationTest(unittest.TestCase):
    """`judge` still accepts the plain strings it always did."""

    def test_strings_and_claims_both_work(self):
        source = (
            "Genetic algorithms were applied to drone routing across sixty urban "
            "delivery scenarios. Route length fell by a fifth against the baseline "
            "heuristic, and computation time was unchanged."
        )
        as_string = match.judge("Genetic algorithms shorten drone routes.",
                                source, use_model=False)
        as_claim = match.judge([match.Claim(text="Genetic algorithms shorten drone routes.",
                                            context="In dense cities, genetic algorithms "
                                                    "shorten drone routes [12].", page=4)],
                               source, use_model=False)
        self.assertEqual(as_string.verdict, as_claim.verdict)
        self.assertEqual(as_claim.claim_verdicts[0].page, 4)
        self.assertTrue(as_claim.claim_verdicts[0].context)


# --------------------------------------------------------------------------- #
# Re-checking one reference
# --------------------------------------------------------------------------- #

def _report_with_one_reference() -> dict:
    entry = {
        "key": "12",
        "reference": {
            "key": "12", "raw": "T. Bosona, Urban freight last mile logistics, 2020.",
            "number": 12, "authors": "T. Bosona", "title": "Urban freight last mile logistics",
            "year": "2020", "venue": "Logistics", "doi": "", "arxiv": "", "pmid": "", "url": "",
        },
        "citations": [{
            "key": "12", "label": "[12]", "style": "numeric",
            "sentence": "Consolidation centres cut last-mile emissions in dense cities [12].",
            "claim": "Consolidation centres cut last-mile emissions in dense cities [12].",
            "line": "…", "page": 4, "char_offset": 10, "group_size": 1, "prose": True,
        }],
        "citation_count": 1,
        "verdict": "unrelated",
        "score": 0.1,
        "reason": "The retrieved page was a login form.",
        "engine": "openai",
        "source": {"title": "Urban freight last mile logistics", "retracted": True,
                   "integrity": [{"kind": "retraction", "source": "crossref"}]},
        "fetched": {"kind": "html"},
        "shots": {},
        "notes": ["Landing page looks paywalled or bot-gated."],
        "flags": [{"kind": "retracted-source", "severity": "high",
                   "message": "This work was RETRACTED."}],
        "timed_out": True,
    }
    return {
        "run_id": "20260101-000000-abcdef",
        "source_pdf": "paper.pdf",
        "paper_title": "A paper",
        "stats": {"references_checked": 1, "citations_found": 1, "references_parsed": 12,
                  "engine_planned": "lexical"},
        "references": [entry],
        "orphan_keys": [],
        "out_of_range_keys": [],
        "base_warnings": ["No bibliography heading was found."],
        "warnings": ["No bibliography heading was found."],
    }


class SuppliedSourceTest(unittest.TestCase):
    def test_plain_text_is_read(self):
        supplied = pipeline.read_supplied("source.txt", "Consolidation centres cut "
                                          "last-mile emissions by 28% in dense cities.".encode())
        self.assertEqual(supplied.kind, "text")
        self.assertIn("Consolidation centres", supplied.text)

    def test_an_unreadable_type_says_so(self):
        with self.assertRaises(ValueError):
            pipeline.read_supplied("source.docx", b"PK\x03\x04 not a pdf")

    def test_an_empty_file_says_so(self):
        with self.assertRaises(ValueError):
            pipeline.read_supplied("source.txt", b"   ")


class EvidenceCardTest(unittest.TestCase):
    """The card's caption says where the evidence came from, so it has to be right."""

    def render(self, **kwargs):
        """Every string the card actually draws.

        The card is rasterised to a PNG, so it carries no text layer to read
        back. Recording what was handed to the text writer is the only way to
        see what a reader will end up looking at.
        """
        import fitz
        from citecheck import shots

        drawn: list[str] = []
        original = fitz.Page.insert_textbox

        def spy(page, rect, text, *args, **kwargs_):
            drawn.append(str(text))
            return original(page, rect, text, *args, **kwargs_)

        fitz.Page.insert_textbox = spy
        try:
            with tempfile.TemporaryDirectory() as tmp:
                shots.render_abstract_card(
                    out_dir=Path(tmp), stem="ref", title="A cited work",
                    abstract="We measure soil moisture at two depths.",
                    quote="soil moisture", source_label="crossref", **kwargs,
                )
        finally:
            fitz.Page.insert_textbox = original
        return "\n".join(drawn)

    def test_the_default_caption_is_the_indexed_abstract_wording(self):
        self.assertIn("INDEXED ABSTRACT", self.render())

    def test_a_custom_caption_reaches_the_card(self):
        """It is a lie about provenance if it does not — and it printed a Rect once."""
        text = self.render(
            banner="SUPPLIED DOCUMENT - text you provided",
            footnote="Re-checked against a document you uploaded.",
        )
        self.assertIn("SUPPLIED DOCUMENT", text)
        self.assertIn("uploaded", text)
        self.assertNotIn("Rect(", text)


class SummariseTest(unittest.TestCase):
    """The headline is derived, so it can never disagree with the entries."""

    def test_a_report_document_arrives_already_summarised(self):
        """Serving `to_dict()` raw once meant flag warnings vanished from the API."""
        report = pipeline.Report(run_id="r", source_pdf="p.pdf")
        report.warnings.append("No bibliography heading was found.")
        report.references = _report_with_one_reference()["references"]
        document = report.to_dict()
        self.assertIn("verdicts", document["stats"])
        self.assertTrue(any("RETRACTED" in w for w in document["warnings"]))
        self.assertEqual(document["stats"]["retracted"], 1)

    def test_warnings_are_rebuilt_rather_than_appended_to(self):
        report = _report_with_one_reference()
        pipeline.summarise(report)
        first = list(report["warnings"])
        pipeline.summarise(report)
        self.assertEqual(report["warnings"], first, "summarising twice duplicated warnings")

    def test_a_reference_that_no_longer_times_out_stops_being_warned_about(self):
        report = _report_with_one_reference()
        report["references"][0]["notes"] = ["Not checked: it ran out of time."]
        pipeline.summarise(report)
        self.assertTrue(any("ran out of time" in w for w in report["warnings"]))

        report["references"][0].pop("timed_out")
        report["references"][0]["notes"] = []
        pipeline.summarise(report)
        self.assertFalse(any("ran out of time" in w for w in report["warnings"]))


class ManualVerdictTest(unittest.TestCase):
    """The tool screens; a person decides — and the report must say which."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report_with_one_reference())

    def tearDown(self):
        self._tmp.cleanup()

    def set(self, **kwargs):
        return pipeline.set_verdict(self.run_dir, "12", **kwargs)

    def entry(self, report):
        return report["references"][0]

    def test_a_hand_set_verdict_replaces_the_headline(self):
        entry = self.entry(self.set(verdict="supported", note="Read it; p.7 says so."))
        self.assertEqual(entry["verdict"], "supported")
        self.assertEqual(entry["reviewed"]["verdict"], "supported")
        self.assertIn("Set by hand", entry["reason"])
        self.assertIn("p.7", entry["reason"])

    def test_what_the_tool_found_is_never_destroyed(self):
        entry = self.entry(self.set(verdict="supported"))
        self.assertEqual(entry["reviewed"]["machine_verdict"], "unrelated")
        self.assertIn("login form", entry["reviewed"]["machine_reason"])

    def test_marking_twice_does_not_rewrite_what_the_tool_found(self):
        """Otherwise the reader's own first answer becomes 'what the tool said'."""
        self.set(verdict="supported")
        entry = self.entry(self.set(verdict="related"))
        self.assertEqual(entry["verdict"], "related")
        self.assertEqual(entry["reviewed"]["machine_verdict"], "unrelated")

    def test_clearing_restores_the_tool_exactly(self):
        before = self.entry(json.loads((self.run_dir / "report.json").read_text(encoding="utf-8")))
        self.set(verdict="supported", note="a note")
        entry = self.entry(self.set(clear=True))
        self.assertEqual(entry["verdict"], before["verdict"])
        self.assertEqual(entry["reason"], before["reason"])
        self.assertNotIn("reviewed", entry)

    def test_the_banner_admits_the_verdict_was_set_by_hand(self):
        """A report reading 'clear' because someone marked it clear is a
        different document, and the reader of the PDF was not in the room."""
        report = self.set(verdict="supported")
        headlines = " ".join(report["stats"]["risk"]["headlines"]).lower()
        self.assertIn("set by hand", headlines)
        self.assertEqual(report["stats"]["reviewed"], 1)

    def test_an_invented_verdict_is_refused(self):
        with self.assertRaises(ValueError):
            self.set(verdict="probably fine")

    def test_an_unknown_reference_is_refused(self):
        with self.assertRaises(KeyError):
            pipeline.set_verdict(self.run_dir, "999", verdict="supported")


class RecheckTest(unittest.TestCase):
    """Re-running one reference has to move the report, not just the card."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report_with_one_reference())
        self.options = pipeline.Options(use_model=False, take_screenshots=False)

    def tearDown(self):
        self._tmp.cleanup()

    def recheck(self, text):
        return pipeline.recheck_one(
            self.run_dir, "12", self.options,
            supplied=pipeline.SuppliedSource(name="bosona-2020.txt", text=text),
        )

    def test_a_supplied_document_replaces_the_verdict(self):
        report = self.recheck(
            "Urban consolidation centres for last-mile freight. This study measures "
            "emissions from last-mile delivery in dense cities and finds that "
            "consolidation centres cut them by 28 percent across four European "
            "cities over an eighteen month period."
        )
        entry = report["references"][0]
        self.assertNotEqual(entry["verdict"], "unrelated")
        self.assertEqual(entry["rechecked"]["previous_verdict"], "unrelated")
        self.assertEqual(entry["rechecked"]["against"], "supplied")
        self.assertEqual(entry["rechecked"]["filename"], "bosona-2020.txt")
        self.assertEqual(entry["fetched"]["kind"], "supplied file")
        self.assertNotIn("timed_out", entry)

    def test_the_run_level_tally_moves_with_it(self):
        report = self.recheck("Consolidation centres cut last-mile delivery emissions "
                              "in dense European cities by twenty-eight percent.")
        self.assertEqual(report["stats"]["verdicts"].get("unrelated", 0), 0)
        self.assertEqual(report["stats"]["rechecked"], 1)
        # Still critical: the work is retracted, and nothing here re-tested that.
        self.assertEqual(report["stats"]["risk"]["level"], "critical")

    def test_index_findings_are_carried_over_not_discarded(self):
        """The reader supplied content, not new evidence about existence."""
        report = self.recheck("Consolidation centres and last-mile emissions in dense cities.")
        entry = report["references"][0]
        self.assertTrue(entry["source"]["retracted"])
        self.assertTrue(any(f["kind"] == "retracted-source" for f in entry["flags"]))
        self.assertEqual(report["stats"]["retracted"], 1)

    def test_the_result_is_written_back_to_disk(self):
        self.recheck("Consolidation centres cut last-mile emissions in dense cities.")
        saved = json.loads((self.run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["references"][0].get("rechecked"))

    def test_an_unknown_reference_is_refused(self):
        with self.assertRaises(KeyError):
            pipeline.recheck_one(
                self.run_dir, "999", self.options,
                supplied=pipeline.SuppliedSource(name="x.txt", text="anything at all"),
            )

    def test_a_recheck_drops_a_hand_set_verdict_and_says_so(self):
        """It judged evidence this re-check just replaced."""
        pipeline.set_verdict(self.run_dir, "12", verdict="supported", note="read it")
        report = self.recheck("Consolidation centres cut last-mile emissions in dense cities.")
        entry = report["references"][0]
        self.assertNotIn("reviewed", entry)
        self.assertEqual(entry["rechecked"]["cleared_review"], "supported")
        self.assertEqual(report["stats"]["reviewed"], 0)


if __name__ == "__main__":
    unittest.main()
