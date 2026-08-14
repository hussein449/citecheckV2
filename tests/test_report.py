"""The screening headline has to describe what actually happened.

The headline is the one line a reader acts on, and the failure it must never
have is the quiet one: a report that checked nothing at all wearing the same
green banner as a report that checked everything and found it sound.
"""

from __future__ import annotations

import unittest

from citecheck import pipeline


class RiskSummaryTest(unittest.TestCase):
    def test_clean_run_is_clear(self):
        risk = pipeline.risk_summary({
            "references_checked": 10, "citations_found": 40,
            "references_parsed": 12, "verdicts": {"supported": 10},
        })
        self.assertEqual(risk["level"], "clear")

    def test_nothing_checked_is_never_clear(self):
        """The drone review's original report: 0 of 0 checked, banner green."""
        risk = pipeline.risk_summary({
            "references_checked": 0, "citations_found": 94,
            "references_parsed": 60, "verdicts": {},
        })
        self.assertNotEqual(risk["level"], "clear")
        self.assertTrue(
            any("nothing was checked" in h.lower() for h in risk["headlines"]),
            risk["headlines"],
        )

    def test_empty_run_says_which_stage_came_up_empty(self):
        cases = {
            # No markers at all — most often a scanned, image-only PDF.
            (0, 0): "no in-text citation",
            # Markers found, bibliography unreadable.
            (40, 0): "bibliography could not be read",
            # Both found, but nothing linked the two.
            (40, 30): "none of the in-text markers matched",
        }
        for (citations, parsed), expected in cases.items():
            with self.subTest(citations=citations, parsed=parsed):
                risk = pipeline.risk_summary({
                    "references_checked": 0, "citations_found": citations,
                    "references_parsed": parsed, "verdicts": {},
                })
                self.assertIn(expected, " ".join(risk["headlines"]).lower())

    def test_retraction_outranks_everything(self):
        risk = pipeline.risk_summary({
            "references_checked": 5, "citations_found": 20,
            "references_parsed": 5, "retracted": 1, "verdicts": {"supported": 5},
        })
        self.assertEqual(risk["level"], "critical")
        self.assertIn("retracted", " ".join(risk["headlines"]).lower())

    def test_misrepresented_citations_are_a_concern(self):
        risk = pipeline.risk_summary({
            "references_checked": 5, "citations_found": 20,
            "references_parsed": 5, "verdicts": {"unrelated": 2, "supported": 3},
        })
        self.assertEqual(risk["level"], "concern")


if __name__ == "__main__":
    unittest.main()
