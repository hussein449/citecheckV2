"""Re-checking one citation of a reference, and nothing else.

A reference cited five times carries five judgements, and a reader who doubts
one of them has no business spending five model calls to answer it — nor should
the four they were content with be thrown away to re-answer the fifth. The
guarantee tested here is narrowness: one claim goes to the judge, one verdict
comes back, and everything else on the card is exactly as it was.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citecheck import match, pipeline


SOURCE = (
    "Urban consolidation centres and last-mile freight. We measure emissions from "
    "last-mile delivery across four European cities and find that consolidation "
    "centres reduce them by twenty-eight percent over an eighteen month period. "
    "Cargo bicycles are compared against light goods vehicles on short routes."
)


def _report_with_three_citations() -> dict:
    sentences = [
        "Consolidation centres cut last-mile emissions in dense cities [12].",
        "Drone delivery reduces urban congestion [12].",
        "Cargo bikes outperform vans below three kilometres [12].",
    ]
    entry = {
        "key": "12",
        "reference": {
            "key": "12", "raw": "T. Bosona, Urban freight last mile logistics, 2020.",
            "number": 12, "authors": "T. Bosona",
            "title": "Urban freight last mile logistics", "year": "2020",
            "venue": "Logistics", "doi": "", "arxiv": "", "pmid": "", "url": "",
        },
        "citations": [
            {
                "key": "12", "label": "[12]", "style": "numeric",
                "sentence": s, "claim": s, "line": "…", "page": 4 + i,
                "char_offset": 10 * i, "group_size": 1, "prose": True,
            }
            for i, s in enumerate(sentences)
        ],
        "citation_count": 3,
        "verdict": "unrelated",
        "score": 0.2,
        "reason": "Most serious: the second citation is unsupported.",
        "engine": "openai",
        "source": {"title": "Urban freight last mile logistics", "retracted": True,
                   "integrity": [{"kind": "retraction", "source": "crossref"}]},
        "fetched": {"kind": "html"},
        "shots": {"evidence": "ref12.png", "matched_text": "consolidation centres"},
        "notes": ["Landing page looks paywalled."],
        "flags": [{"kind": "retracted-source", "severity": "high",
                   "message": "This work was RETRACTED."}],
        "claim_verdicts": [
            {"claim": sentences[0], "verdict": "supported", "score": 0.9,
             "reason": "Directly stated.", "evidence_quote": "reduce them by twenty-eight percent",
             "page": 4, "context": "", "reconsidered": False},
            {"claim": sentences[1], "verdict": "unrelated", "score": 0.8,
             "reason": "The source says nothing about drones.", "evidence_quote": "",
             "page": 5, "context": "", "reconsidered": False},
            {"claim": sentences[2], "verdict": "related", "score": 0.6,
             "reason": "Cargo bikes are discussed.", "evidence_quote": "Cargo bicycles are compared",
             "page": 6, "context": "", "reconsidered": False},
        ],
    }
    return {
        "run_id": "20260101-000000-abcdef",
        "source_pdf": "paper.pdf",
        "paper_title": "A paper",
        "stats": {"references_checked": 1, "citations_found": 3,
                  "references_parsed": 12, "engine_planned": "openai"},
        "references": [entry],
        "orphan_keys": [],
        "out_of_range_keys": [],
        "base_warnings": [],
        "warnings": [],
    }


class RecheckOneCitationTest(unittest.TestCase):
    """One citation is re-judged; its siblings are not touched at all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report_with_three_citations())
        self.options = pipeline.Options(use_model=False, take_screenshots=False)

    def tearDown(self):
        self._tmp.cleanup()

    def recheck(self, claim_index, text=SOURCE):
        return pipeline.recheck_one(
            self.run_dir, "12", self.options, claim_index=claim_index,
            supplied=pipeline.SuppliedSource(name="bosona-2020.txt", text=text),
        )

    def entry(self, report=None):
        report = report or json.loads(
            (self.run_dir / "report.json").read_text(encoding="utf-8")
        )
        return report["references"][0]

    def test_only_the_named_citation_changes(self):
        before = self.entry()["claim_verdicts"]
        after = self.entry(self.recheck(1))["claim_verdicts"]
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[2], after[2])
        self.assertNotEqual(before[1]["reason"], after[1]["reason"])

    def test_the_citation_count_is_unchanged(self):
        self.assertEqual(len(self.entry(self.recheck(1))["claim_verdicts"]), 3)

    def test_the_re_judged_citation_records_what_it_was_judged_against(self):
        claim = self.entry(self.recheck(1))["claim_verdicts"][1]
        self.assertEqual(claim["rechecked"]["against"], "supplied")
        self.assertEqual(claim["rechecked"]["filename"], "bosona-2020.txt")
        self.assertEqual(claim["rechecked"]["previous_verdict"], "unrelated")

    def test_the_headline_rolls_up_from_all_three(self):
        """Fixing the worst citation lets the card improve — but only to the
        next worst, never past a sibling that is still a problem."""
        entry = self.entry(self.recheck(1))
        rolled = match.roll_up(
            [c["verdict"] for c in entry["claim_verdicts"]], entry["engine"]
        )
        self.assertEqual(entry["verdict"], rolled)

    def test_the_reason_says_which_citation_moved(self):
        entry = self.entry(self.recheck(1))
        self.assertIn("Citation 2 of 3", entry["reason"])

    def test_the_reference_level_record_says_it_was_scoped(self):
        done = self.entry(self.recheck(1))["rechecked"]
        self.assertEqual(done["scope"], "claim")
        self.assertEqual(done["claim_index"], 1)
        self.assertEqual(done["outcome"], "judged")

    def test_index_findings_are_left_alone(self):
        """A question about one citation must not rewrite the whole card."""
        entry = self.entry(self.recheck(1))
        self.assertTrue(entry["source"]["retracted"])
        self.assertTrue(any(f["kind"] == "retracted-source" for f in entry["flags"]))

    def test_the_cards_screenshots_are_left_alone(self):
        """They describe the reference, not the one citation that was re-judged."""
        self.assertEqual(self.entry(self.recheck(1))["shots"]["evidence"], "ref12.png")

    def test_the_run_tally_is_recomputed(self):
        report = self.recheck(1)
        self.assertEqual(sum(report["stats"]["claim_verdicts"].values()), 3)
        self.assertEqual(report["stats"]["retracted"], 1)

    def test_a_citation_that_does_not_exist_is_refused(self):
        for bad in (3, -1):
            with self.subTest(index=bad), self.assertRaises(KeyError):
                self.recheck(bad)

    def test_it_is_written_back_to_disk(self):
        self.recheck(2)
        saved = self.entry()
        self.assertTrue(saved["claim_verdicts"][2].get("rechecked"))
        self.assertNotIn("rechecked", saved["claim_verdicts"][0])


class HandSetVerdictsAroundItTest(unittest.TestCase):
    """A re-check clears the reader's verdict only where it replaced evidence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report_with_three_citations())
        self.options = pipeline.Options(use_model=False, take_screenshots=False)

    def tearDown(self):
        self._tmp.cleanup()

    def recheck(self, claim_index):
        return pipeline.recheck_one(
            self.run_dir, "12", self.options, claim_index=claim_index,
            supplied=pipeline.SuppliedSource(name="src.txt", text=SOURCE),
        )["references"][0]

    def test_a_verdict_on_a_sibling_survives(self):
        """Its evidence was never touched, so the reader's reading still holds."""
        pipeline.set_verdict(self.run_dir, "12", claim_index=0,
                             verdict="weak", note="overstated")
        entry = self.recheck(1)
        self.assertEqual(entry["claim_verdicts"][0]["verdict"], "weak")
        self.assertTrue(entry["claim_verdicts"][0]["override"])

    def test_a_verdict_on_the_re_checked_citation_is_cleared_and_reported(self):
        pipeline.set_verdict(self.run_dir, "12", claim_index=1,
                             verdict="supported", note="read it myself")
        entry = self.recheck(1)
        self.assertNotIn("override", entry["claim_verdicts"][1])
        self.assertEqual(entry["claim_verdicts"][1]["rechecked"]["cleared_review"],
                         "supported")

    def test_a_surviving_sibling_verdict_still_decides_the_headline(self):
        pipeline.set_verdict(self.run_dir, "12", claim_index=0, verdict="unrelated")
        entry = self.recheck(2)
        self.assertEqual(entry["verdict"], "unrelated")
        self.assertEqual(entry["reviewed"]["source"], "claims")

    def test_the_tools_own_record_ignores_a_sibling_override(self):
        """What the tool concluded must never absorb the reader's answer."""
        pipeline.set_verdict(self.run_dir, "12", claim_index=0, verdict="not_found")
        entry = self.recheck(2)
        self.assertEqual(entry["machine"]["verdict"],
                         match.roll_up(["supported", "unrelated",
                                        entry["claim_verdicts"][2]["verdict"]],
                                       entry["engine"]))


class EngineLabelTest(unittest.TestCase):
    """A scoped re-check must not change how the untouched citations roll up.

    `match.roll_up` reads the card's engine label, and the two tiers roll up in
    opposite directions. Relabelling a model-judged card as lexical because one
    citation was re-judged lexically switches every sibling from worst-case to
    best-case — cancelling two "unrelated" verdicts into a headline of
    "unverified" on the strength of nothing anyone looked at again.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report_with_three_citations())

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_lexical_recheck_does_not_relabel_a_model_judged_card(self):
        entry = pipeline.recheck_one(
            self.run_dir, "12",
            pipeline.Options(use_model=False, take_screenshots=False),
            claim_index=2,
            supplied=pipeline.SuppliedSource(name="s.txt", text=SOURCE),
        )["references"][0]
        self.assertEqual(entry["engine"], "openai")

    def test_the_untouched_siblings_still_decide_the_headline(self):
        entry = pipeline.recheck_one(
            self.run_dir, "12",
            pipeline.Options(use_model=False, take_screenshots=False),
            claim_index=2,
            supplied=pipeline.SuppliedSource(name="s.txt", text=SOURCE),
        )["references"][0]
        # Citation 2 is still "unrelated" and was never re-examined, so it has
        # to keep setting the headline.
        self.assertEqual(entry["claim_verdicts"][1]["verdict"], "unrelated")
        self.assertEqual(entry["verdict"], "unrelated")

    def test_a_model_verdict_still_upgrades_a_lexical_card(self):
        self.assertEqual(pipeline._stronger_engine("lexical", "openai"), "openai")

    def test_an_unjudged_card_takes_whatever_came_back(self):
        self.assertEqual(pipeline._stronger_engine("", "lexical"), "lexical")


class BarrenClaimRecheckTest(unittest.TestCase):
    """A lookup that returned nothing leaves the citation exactly as it was."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report_with_three_citations())
        self.options = pipeline.Options(use_model=False, take_screenshots=False)
        self._resolve = pipeline.resolve.resolve

    def tearDown(self):
        pipeline.resolve.resolve = self._resolve
        self._tmp.cleanup()

    def recheck(self, claim_index=1):
        def dead(_reference):
            raise ConnectionError("publisher unreachable")

        pipeline.resolve.resolve = dead
        return pipeline.recheck_one(
            self.run_dir, "12", self.options, claim_index=claim_index
        )["references"][0]

    def test_the_citation_keeps_its_verdict(self):
        entry = self.recheck()
        self.assertEqual(entry["claim_verdicts"][1]["verdict"], "unrelated")

    def test_the_failure_is_reported_as_a_failure(self):
        done = self.recheck()["rechecked"]
        self.assertEqual(done["outcome"], "nothing_retrieved")
        self.assertEqual(done["scope"], "claim")
        self.assertTrue(done["detail"])

    def test_the_card_headline_does_not_move(self):
        self.assertEqual(self.recheck()["verdict"], "unrelated")

    def test_repeating_it_never_changes_the_answer(self):
        seen = [self.recheck()["claim_verdicts"][1]["verdict"] for _ in range(3)]
        self.assertEqual(seen, ["unrelated"] * 3)


class ClaimLookupTest(unittest.TestCase):
    """Which citation a stored index actually refers to."""

    def setUp(self):
        report = _report_with_three_citations()
        self.entry = report["references"][0]
        self.citations = [
            pipeline._rebuild(pipeline.intext.Citation, c)
            for c in self.entry["citations"]
        ]
        self.stored = self.entry["claim_verdicts"]

    def test_it_matches_on_text_not_position(self):
        """A report whose claim order has shifted still targets the right one."""
        shuffled = list(reversed(self.citations))
        claim = pipeline._claim_at(shuffled, self.stored, 1)
        self.assertEqual(claim.text, self.stored[1]["claim"])

    def test_it_carries_the_group_size_the_judge_needs(self):
        """Rebuilt from the citations because a stored verdict has no co_cited."""
        claim = pipeline._claim_at(self.citations, self.stored, 0)
        self.assertEqual(claim.co_cited, 1)

    def test_it_falls_back_to_the_stored_text_when_citations_are_gone(self):
        claim = pipeline._claim_at([], self.stored, 2)
        self.assertEqual(claim.text, self.stored[2]["claim"])


if __name__ == "__main__":
    unittest.main()
