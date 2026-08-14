"""End-to-end parsing checks against real papers.

These are the papers that have actually broken the parser, kept as a corpus so
the next layout fix does not silently undo an earlier one. Nothing here touches
the network: every assertion is about what we can read out of the PDF itself.

The numbers are lower bounds, not exact counts. A parser improvement that finds
*more* references is not a regression and should not have to edit this file;
losing references is the failure these tests exist to catch.

The papers themselves are published articles and are not committed; see
`tests/corpus/README.md`. Anything missing is skipped rather than failed, so a
fresh checkout still runs green — the style coverage that must hold everywhere
lives in `test_styles.py`, which needs no files at all.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from citecheck import intext, pdf_parse, refs

_ROOT = Path(__file__).resolve().parent.parent
# `tests/corpus` is the durable home; `uploads` is where the running app leaves
# papers, and is searched too so a working install needs no extra copying.
CORPUS_DIRS = (_ROOT / "tests" / "corpus", _ROOT / "uploads")

# name -> (filename suffix, style, min references, min cited, max orphans)
CORPUS = {
    # Author-year review whose summary tables carry a "Study ID" column of
    # "[126]" cells. Those cells used to outvote the real citations and blank
    # the entire run: 0 references checked, 71 orphaned markers.
    "drone_logistics": ("Drone Logistics (2).pdf", "author-year", 140, 90, 5),
    # Two-column Elsevier paper. Column interleaving used to shred the
    # reference list into alternating halves, parsing none of it.
    "two_column_vancouver": ("j.jclinepi.2022.03.004.pdf", "numeric", 18, 18, 0),
    # Straightforward numeric papers — the cases that already worked, kept here
    # so a fix aimed at author-year styles cannot quietly cost us them.
    "springer_numeric": ("s13638-024-02373-5.pdf", "numeric", 34, 34, 0),
    "long_numeric": ("Flying_ad_hoc_paper_1.pdf", "numeric", 145, 145, 0),
    "ieee_numeric": ("2017STOPSpeedRadar.pdf", "numeric", 22, 22, 0),
    "minimal": ("test_paper.pdf", "numeric", 5, 5, 0),
}


def _find(suffix: str) -> Path | None:
    """Locate a corpus paper. Uploaded copies are prefixed with their run id."""
    for directory in CORPUS_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.pdf")):
            if path.name.endswith(suffix):
                return path
    return None


class CorpusTest(unittest.TestCase):
    """Every corpus paper must parse, link and keep its citation style."""

    def _parse(self, suffix: str):
        path = _find(suffix)
        if path is None:
            self.skipTest(f"corpus paper not present: {suffix}")
        parsed = pdf_parse.parse_pdf(str(path))
        citations = intext.extract_citations(parsed.body_text, parsed.page_of_offset)
        grouped = intext.group_by_reference(citations)
        reference_list = refs.parse_references(parsed.references_text)
        matched, orphans = refs.link_citations(
            grouped, refs.index_references(reference_list)
        )
        return citations, reference_list, matched, orphans


def _make(name, suffix, style, min_refs, min_cited, max_orphans):
    def test(self):
        citations, reference_list, matched, orphans = self._parse(suffix)

        self.assertTrue(citations, f"{name}: no in-text citations found at all")
        # One paper, one scheme. Getting this wrong is not a partial failure:
        # the losing style's markers are discarded outright.
        styles = {c.style for c in citations}
        self.assertEqual(
            styles, {style}, f"{name}: expected {style} citations, got {styles}"
        )
        self.assertGreaterEqual(
            len(reference_list), min_refs,
            f"{name}: bibliography parsed down to {len(reference_list)} entries",
        )
        self.assertGreaterEqual(
            len(matched), min_cited,
            f"{name}: only {len(matched)} references linked to a marker",
        )
        self.assertLessEqual(
            len(orphans), max_orphans,
            f"{name}: {len(orphans)} markers matched nothing: {sorted(orphans)[:10]}",
        )
        # Every linked reference has to be checkable: something to search for
        # and a year to check it against.
        for key, ref in matched.items():
            self.assertTrue(
                ref.title or ref.doi or ref.url,
                f"{name}: reference {key} has no title, DOI or URL to resolve",
            )

    test.__name__ = f"test_{name}"
    return test


for _name, _args in CORPUS.items():
    setattr(CorpusTest, f"test_{_name}", _make(_name, *_args))


class PageStructureTest(unittest.TestCase):
    """The bibliography has to be split off from the body, not left inside it."""

    def test_two_column_reference_list_is_not_interleaved(self):
        path = _find("j.jclinepi.2022.03.004.pdf")
        if path is None:
            self.skipTest("corpus paper not present")
        parsed = pdf_parse.parse_pdf(str(path))
        reference_list = refs.parse_references(parsed.references_text)

        # Interleaved columns showed up as entries numbered out of order, with
        # one entry's text spliced into another's. Consecutive numbering is the
        # cheapest proof the columns were read one at a time.
        numbers = [r.number for r in reference_list if r.number is not None]
        self.assertEqual(numbers, sorted(numbers), "reference numbers out of order")
        self.assertEqual(len(numbers), len(set(numbers)), "duplicate entry numbers")

    def test_body_text_excludes_the_bibliography(self):
        path = _find("Drone Logistics (2).pdf")
        if path is None:
            self.skipTest("corpus paper not present")
        parsed = pdf_parse.parse_pdf(str(path))
        self.assertTrue(parsed.references_text.strip(), "no bibliography found")
        # The split point is a real heading, so the last body page precedes it.
        self.assertIsNotNone(parsed.references_page)
        self.assertLess(len(parsed.body_text), len(parsed.full_text))


if __name__ == "__main__":
    unittest.main()
