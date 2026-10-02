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
    # ACL author-year: given-name-first entries with the year as its own
    # sentence, LaTeX accents extracted as loose glyphs, and lettered appendices
    # whose numbered guideline list used to be taken for the bibliography.
    "acl_author_year": ("FEVER_original.pdf", "author-year", 24, 24, 0),
    # Nature style: superscript citation numbers with no brackets, and a table
    # after the bibliography whose "Reference" column header was taken for the
    # bibliography heading.
    "superscript_numeric": ("crisprVerse_original.pdf", "numeric", 105, 100, 0),
    # Seven papers the tool had never been run on, every one of which came back
    # with a short or empty reference list. What each one hid:
    #   "Surname, Given" author lists, and maths full of superscript exponents
    "iclr_surname_given": ("arxiv_1412.6980_adam.pdf", "author-year", 23, 22, 0),
    #   the same style, with 2013a/b/c entries and an appendix after the list
    "iclr_year_suffixes": ("arxiv_1412.6572_adversarial.pdf", "author-year", 19, 18, 0),
    #   "References" 40% of the way through, methods and more citations after it
    "heading_before_halfway": ("arxiv_2006.10256_numpy.pdf", "numeric", 73, 73, 0),
    "long_appendix_after_list": ("arxiv_1911.08265_muzero.pdf", "numeric", 49, 49, 0),
    #   no heading at all, and the list resumes after the Methods
    "no_heading_list_resumes": ("arxiv_1803.02342_graphene.pdf", "numeric", 65, 54, 0),
    "no_heading_two_column": ("arxiv_1707.04344_51atom.pdf", "numeric", 62, 62, 0),
    #   no heading, superscript citations, entries labelled "12 Surname, A."
    "bare_number_labels": ("arxiv_1610.08057_dtc.pdf", "numeric", 34, 33, 0),
    # A second unseen batch, checked against each paper's own LaTeX source.
    "nips_numeric": ("arxiv_1706.03762.pdf", "numeric", 40, 40, 0),
    "acl_three_word_surname": ("arxiv_1810.04805.pdf", "author-year", 56, 56, 0),
    #   unquoted titles after initials-first authors; "[256, 480]" is a range
    "cvpr_unquoted_titles": ("arxiv_1512.03385.pdf", "numeric", 50, 50, 2),
    #   "[BJP12]" labels
    "alpha_labels": ("arxiv_1312.6114.pdf", "alpha", 17, 17, 0),
    "nips_surname_first": ("arxiv_1406.2661.pdf", "numeric", 31, 31, 0),
    #   one entry is undated ("(unpublished)") and cannot be keyed
    "icml_surname_given": ("arxiv_1502.03167.pdf", "author-year", 23, 20, 0),
    "lncs_numbered": ("arxiv_1505.04597.pdf", "numeric", 14, 14, 0),
    #   years wrapped onto their own line, entries that are bare URLs
    "prl_wrapped_years": ("arxiv_1602.03837.pdf", "numeric", 118, 118, 0),
    "iclr_author_year": ("arxiv_1409.1556.pdf", "author-year", 34, 34, 0),
    #   "[Ioffe and Szegedy, 2015]"
    "square_bracket_author_year": ("arxiv_1607.06450.pdf", "author-year", 32, 32, 0),
    # A third unseen batch.
    #   "temperatures of [1, 2, 5, 10]" with nine references
    "values_in_brackets": ("arxiv_1503.02531.pdf", "numeric", 9, 9, 1),
    "iclr_identifier_tail": ("arxiv_1412.6806.pdf", "author-year", 26, 26, 0),
    "cvpr_numeric": ("arxiv_1411.4038.pdf", "numeric", 39, 39, 0),
    "inline_bibitems": ("arxiv_1301.3781.pdf", "numeric", 32, 31, 0),
    #   "Diederik P. Kingma and Jimmy Ba. Adam": middle initials, given names first
    "middle_initials": ("arxiv_1609.02907.pdf", "author-year", 32, 32, 0),
    "iccv_numeric": ("arxiv_1703.06870.pdf", "numeric", 45, 45, 2),
    "appendix_table_after_list": ("arxiv_2010.11929.pdf", "author-year", 58, 58, 0),
    "quantum_numeric": ("arxiv_1801.00862.pdf", "numeric", 57, 57, 0),
    #   Vancouver initials in an author-year list, steps numbered in the appendix;
    #   still imperfect: wrapped author lists are cut at the line break
    "jss_author_year": ("arxiv_1406.5823.pdf", "author-year", 34, 27, 5),
    "numeric_long": ("arxiv_1802.03426.pdf", "numeric", 65, 65, 0),
    # A fourth unseen batch.
    "nips_numeric_2": ("arxiv_1409.3215.pdf", "numeric", 31, 31, 0),
    "cvpr_numeric_2": ("arxiv_1506.02640.pdf", "numeric", 39, 39, 0),
    #   page numbers between "Surname, Given" entries; an entry ending "Accessed: ..."
    "page_numbers_in_list": ("arxiv_1511.06434.pdf", "author-year", 37, 36, 0),
    "alpha_labels_plus": ("arxiv_1707.06347.pdf", "alpha", 14, 14, 0),
    "nips_given_first": ("arxiv_1312.5602.pdf", "numeric", 26, 26, 0),
    "icml_initials": ("arxiv_1905.11946.pdf", "author-year", 52, 52, 0),
    #   entries following a URL or "Software available from ..." tail
    "given_first_after_url": ("arxiv_1710.10903.pdf", "author-year", 44, 44, 0),
    #   Rev. Mod. Phys.: "Fu, L., and C. L. Kane, 2007, Phys. Rev. B 76, 045302."
    #   Still imperfect: same-year entries the list does not letter, and
    #   "Bernevig, Hughes and Zhang (2006)" read from its second author.
    "rmp_author_comma_year": ("arxiv_1002.3895.pdf", "author-year", 181, 150, 20),
    "long_author_year_review": ("arxiv_1206.5538.pdf", "author-year", 227, 220, 0),
    "acl_editors_entry": ("arxiv_1907.11692.pdf", "author-year", 51, 50, 0),
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
            grouped, refs.index_references(reference_list), reference_list
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
        # and a year to check it against. Physics journals print no titles, so
        # there the journal, volume and page are what there is to search by.
        for key, ref in matched.items():
            self.assertTrue(
                ref.title or ref.doi or ref.url or ref.venue,
                f"{name}: reference {key} has no title, DOI, URL or venue to resolve",
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
        # The list is blanked out of the body rather than cut from it, so that
        # offsets after it still map to their pages: same length, less text.
        self.assertEqual(len(parsed.body_text), len(parsed.full_text))
        self.assertLess(len(parsed.body_text.split()), len(parsed.full_text.split()))
        first_entry = parsed.references_text.strip().splitlines()[0]
        self.assertNotIn(first_entry, parsed.body_text)


if __name__ == "__main__":
    unittest.main()
