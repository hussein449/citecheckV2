"""Layouts that hid a bibliography, or part of one, from the parser.

Found by auditing papers the tool had never been run on: every one of seven
came back with a short or empty reference list while the eleven it had been
fixed against all parsed. Each case below is one of those layouts, written out
so it runs with no PDF.
"""

from __future__ import annotations

import unittest

from citecheck import intext, pdf_parse, refs


def _split(lines):
    return pdf_parse._split_references([pdf_parse.Page(1, "\n".join(lines))])


def _numbered(count, start=1, label="[{}]"):
    out = []
    for n in range(start, start + count):
        out += [f"{label.format(n)} Author{n}, A. B. A title of some kind number {n}.",
                f"Journal of Things {n}, 1-9 (20{n % 20:02d})."]
    return out


BODY = [f"Body sentence number {i} goes here." for i in range(30)]


class WhereTheListIsTest(unittest.TestCase):
    def test_heading_before_the_halfway_point(self):
        """Long methods and appendices put "References" early in the text."""
        appendix = ["Supplementary Materials"] + [f"Appendix prose line {i}." for i in range(200)]
        body, references, _ = _split(BODY + ["References"] + _numbered(8) + appendix)
        self.assertEqual(len(refs.parse_references(references)), 8)
        self.assertNotIn("Appendix prose", references)

    def test_no_heading_at_all(self):
        """Physics templates start the list without announcing it."""
        steps = ["1. Load the atoms.", "2. Cool them.", "3. Image them."]
        _, references, _ = _split(BODY + steps + BODY + _numbered(9))
        parsed = refs.parse_references(references)
        self.assertEqual([r.number for r in parsed], list(range(1, 10)))

    def test_bare_number_labels(self):
        """revtex under superscript citations: "12 Cross, M. C. & Hohenberg, P. C."."""
        entries = []
        for n, name in enumerate(["Adler, R.", "Cross, M. C. & Hohenberg, P. C.", "von Keyserlingk, C.",
                                  "de Lange, G., Wang, Z. H.", "Schreiber, M.", "Choi, J."], start=1):
            entries += [f"{n} {name} A title of some kind number {n}.", f"Phys. Rev. Lett. {n}, 1-9 (2016)."]
        _, references, _ = _split(BODY + entries)
        self.assertEqual([r.number for r in refs.parse_references(references)], [1, 2, 3, 4, 5, 6])

    def test_list_resumes_after_the_methods(self):
        """Nature format: 1-6 after the main text, 7-9 after the Methods."""
        methods = ["METHODS", "", "We did things carefully. " * 3, "And then some more."]
        lines = BODY + ["References"] + _numbered(6) + methods + _numbered(3, start=7)
        body, references, _ = _split(lines)
        self.assertEqual([r.number for r in refs.parse_references(references)], list(range(1, 10)))
        self.assertIn("We did things carefully", body)

    def test_text_after_the_list_is_still_body(self):
        """An appendix cites references too; its text must not be dropped."""
        lines = BODY + ["References"] + _numbered(6) + ["Appendix", "As shown before [3], it holds."]
        body, references, _ = _split(lines)
        self.assertIn("As shown before [3]", body)
        self.assertNotIn("Author3", body)
        # Blanked, not cut: offsets after the list still map to their page.
        self.assertEqual(len(body), len("\n".join(lines)))
        keys = {c.key for c in intext.extract_citations(body)}
        self.assertIn("3", keys)

    def test_capitals_appendix_ends_an_unnumbered_list(self):
        entries = [f"Smith{c}, John. A title of some kind. Venue, 201{i}." for i, c in enumerate("abcdefg")]
        lines = BODY + ["REFERENCES"] + entries + ["A", "RUBBISH CLASS EXAMPLES", "Prose about rubbish. " * 5]
        _, references, _ = _split(lines)
        self.assertEqual(references.splitlines(), entries)


class LastEntryTest(unittest.TestCase):
    def test_what_follows_the_list_is_not_part_of_its_last_entry(self):
        text = (
            "[1] A. One, \"First title of a paper,\" J. Things, 2019.\n"
            "[2] B. Two, \"Second title of a paper,\" J. Things, 2020.\n\n"
            + "Rola Naja earned a Ph.D. in Computer Networking. " * 20
        )
        parsed = refs.parse_references(text)
        self.assertEqual(len(parsed), 2)
        self.assertLess(len(parsed[-1].raw), 120)


class SurnameGivenStyleTest(unittest.TestCase):
    """ICLR/ICML: "Duchi, John, Hazan, Elad, and Singer, Yoram. Title. Venue, 2011."."""

    TEXT = """
        Duchi, John, Hazan, Elad, and Singer, Yoram. Adaptive subgradient methods for online learning and stochastic

        optimization. The Journal of Machine Learning Research, 12:2121-2159, 2011.

        Graves, Alex. Generating sequences with recurrent neural networks. arXiv preprint arXiv:1308.0850, 2013.

        Graves, Alex, Mohamed, Abdel-rahman, and Hinton, Geoffrey. Speech recognition with deep recurrent neural
        networks. In Acoustics, Speech and Signal Processing (ICASSP), pp. 6645-6649. IEEE, 2013.
        10
        Roux, Nicolas L and Fitzgibbon, Andrew W. A fast natural newton method. In Proceedings of ICML, 2010.

        Srivastava, Nitish, Hinton, Geoffrey, and Salakhutdinov, Ruslan. Dropout: A simple way to prevent neural
        networks from overfitting. Journal of Machine Learning Research, 15(1):1929-1958, 2014.
    """

    def setUp(self):
        self.parsed = refs.parse_references(self.TEXT)

    def test_every_entry_is_found_across_the_page_break(self):
        self.assertEqual(
            [r.key for r in self.parsed],
            ["duchi2011", "graves2013", "graves2013", "roux2010", "srivastava2014"],
        )

    def test_the_author_list_is_not_taken_for_the_title(self):
        first = self.parsed[0]
        self.assertTrue(first.authors.startswith("Duchi, John"))
        self.assertTrue(first.title.startswith("Adaptive subgradient methods"))

    def test_a_page_number_is_not_the_year(self):
        self.assertEqual(self.parsed[-1].year, "2014")

    def test_marker_says_which_of_two_same_year_entries(self):
        grouped = intext.group_by_reference(intext.extract_citations(
            "Sequences were generated (Graves, 2013). Speech followed (Graves et al., 2013)."
        ))
        matched, orphans = refs.link_citations(
            grouped, refs.index_references(self.parsed), self.parsed
        )
        self.assertEqual(orphans, [])
        # One key in the text, two entries in the list: each marker reaches its own.
        self.assertEqual(
            sorted(r.title for r in matched.values()),
            ["Generating sequences with recurrent neural networks",
             "Speech recognition with deep recurrent neural networks"],
        )


class YearSuffixTest(unittest.TestCase):
    TEXT = """
        Goodfellow, Ian J., Mirza, Mehdi, and Bengio, Yoshua. Multi-prediction deep Boltzmann machines. In NIPS, 2013a.

        Goodfellow, Ian J., Warde-Farley, David, and Bengio, Yoshua. Maxout networks. In ICML, 2013b.

        Hinton, Geoffrey. A lone paper of that year. In ICML, 2012.
    """

    def link(self, text):
        parsed = refs.parse_references(self.TEXT)
        grouped = intext.group_by_reference(intext.extract_citations(text))
        return refs.link_citations(grouped, refs.index_references(parsed), parsed)

    def test_letters_keep_same_year_entries_apart(self):
        matched, orphans = self.link(
            "Shown twice (Goodfellow et al., 2013a) and again (Goodfellow et al., 2013b)."
        )
        self.assertEqual(orphans, [])
        self.assertEqual({r.title for r in matched.values()},
                         {"Multi-prediction deep Boltzmann machines", "Maxout networks"})

    def test_a_letter_in_the_text_still_reaches_an_entry_without_one(self):
        matched, orphans = self.link("As argued (Hinton, 2012a).")
        self.assertEqual(orphans, [])
        self.assertEqual(len(matched), 1)


class TableCellCitationTest(unittest.TestCase):
    PROSE = " ".join(f"Finding {i} was reported (Agatz, 2018)." for i in range(6))
    TABLE = "\nStudy\nMethod\nEuchi & Sadok, 2021\nHybrid genetic-sweep algorithm\nHam, 2018\nConstraint programming\n"
    LIST = (
        "N Agatz (2018) Optimization approaches for the travelling salesman problem with drone. Transp Sci.\n"
        "J Euchi, A Sadok (2021) Hybrid genetic-sweep algorithm to solve the vehicle routing problem. Phys Comm.\n"
    )

    def test_a_study_named_in_a_table_cell_counts_as_cited(self):
        citations = intext.extract_citations(self.PROSE + self.TABLE)
        self.assertIn("euchi2021", {c.key for c in citations})
        cell = next(c for c in citations if c.key == "euchi2021")
        self.assertFalse(cell.prose)
        self.assertIn("Hybrid genetic-sweep", cell.claim)

    def test_a_cell_that_names_no_entry_is_not_reported_unmatched(self):
        parsed = refs.parse_references(self.LIST)
        grouped = intext.group_by_reference(intext.extract_citations(self.PROSE + self.TABLE))
        matched, orphans = refs.link_citations(grouped, refs.index_references(parsed), parsed)
        self.assertIn("euchi2021", matched)
        self.assertEqual(orphans, [])      # "Ham, 2018" has no entry and was only a cell


class IntervalTest(unittest.TestCase):
    def test_bracketed_interval_with_a_unit_is_not_a_citation(self):
        text = " ".join(f"Claim {i} holds [{i}]." for i in range(1, 8))
        text += " The region lies [75,150]bp upstream of the peak."
        keys = {c.key for c in intext.extract_citations(text)}
        self.assertNotIn("75", keys)
        self.assertNotIn("150", keys)

    def test_marker_glued_to_the_next_sentence_still_counts(self):
        text = " ".join(f"Claim {i} holds [{i}]." for i in range(1, 8))
        text += " It was predicted in graphene.[9]Among these possibilities one stands out."
        self.assertIn("9", {c.key for c in intext.extract_citations(text)})


class SquareBracketAuthorYearTest(unittest.TestCase):
    """natbib's "square" option: "[Ioffe and Szegedy, 2015]"."""

    TEXT = (
        "Batch normalization [Ioffe and Szegedy, 2015] helps. It was extended to recurrent "
        "networks [Laurent et al., 2015, Amodei et al., 2015, Cooijmans et al., 2016]. "
        "Kingma and Ba [2014] proposed Adam. Others agree [see, e.g., Graves, 2013]. "
        "It is used widely [Peters et al., 2017, 2018a]."
    )

    def test_every_marker_is_read(self):
        citations = intext.extract_citations(self.TEXT)
        self.assertEqual({c.style for c in citations}, {"author-year"})
        self.assertEqual(
            sorted({c.key for c in citations}),
            sorted(["ioffe2015", "laurent2015", "amodei2015", "cooijmans2016", "kingma2014",
                    "graves2013", "peters2017", "peters2018a"]),
        )

    def test_lead_in_inside_round_brackets(self):
        text = self.TEXT + " Phrase-based systems (see, e.g., Koehn et al., 2003) differ."
        self.assertIn("koehn2003", {c.key for c in intext.extract_citations(text)})


class AlphaLabelTest(unittest.TestCase):
    """The alpha style: "[BJP12]" in the text and on the entry."""

    LIST = """
        [BCV13]
        Yoshua Bengio, Aaron Courville, and Pascal Vincent. Representation learning: A review and new perspectives. 2013.

        [BJP12]
        David M Blei, Michael I Jordan, and John W Paisley. Variational Bayesian inference
        with Stochastic Search. In Proceedings of ICML, 2012.

        [RMW+14]
        Danilo J Rezende, Shakir Mohamed, and Daan Wierstra. Stochastic back-propagation and variational inference. 2014.
    """
    TEXT = " ".join(f"Point {i} was made in [BJP12]." for i in range(4)) + " See also [BCV13, RMW+14]."

    def test_labels_link_text_to_entries(self):
        parsed = refs.parse_references(self.LIST)
        self.assertEqual([r.key for r in parsed], ["BCV13", "BJP12", "RMW+14"])
        self.assertTrue(parsed[1].title.startswith("Variational Bayesian inference"))
        citations = intext.extract_citations(self.TEXT)
        self.assertEqual({c.style for c in citations}, {"alpha"})
        matched, orphans = refs.link_citations(
            intext.group_by_reference(citations), refs.index_references(parsed), parsed
        )
        self.assertEqual(sorted(matched), ["BCV13", "BJP12", "RMW+14"])
        self.assertEqual(orphans, [])


class InitialsFirstUnquotedTest(unittest.TestCase):
    """CVPR: "Y. Bengio, P. Simard, and P. Frasconi. Title. Venue, 1994."."""

    def fields(self, raw):
        return refs._split_fields(raw)[:2]

    def test_the_last_author_is_an_author(self):
        for raw, authors in (
            ("Y. Bengio, P. Simard, and P. Frasconi. Learning long-term dependencies with gradient "
             "descent is difficult. IEEE Transactions on Neural Networks, 5(2):157-166, 1994",
             "Y. Bengio, P. Simard, and P. Frasconi"),
            ("M. D. Zeiler and R. Fergus. Learning long-term dependencies with gradient descent "
             "is difficult. In ECCV, 2014", "M. D. Zeiler and R. Fergus"),
            ("C. M. Bishop. Learning long-term dependencies with gradient descent is difficult. "
             "Oxford university press, 1995", "C. M. Bishop"),
        ):
            with self.subTest(raw=raw[:30]):
                self.assertEqual(
                    self.fields(raw),
                    (authors, "Learning long-term dependencies with gradient descent is difficult"),
                )


class WrappedYearTest(unittest.TestCase):
    def test_years_on_their_own_line_are_not_running_headers(self):
        """ "(1916)." and "(1918)." on one page are not a header seen twice."""
        pages = []
        for n in range(4):
            lines = [f"Body text of page {n} goes on for a while here."] * 6
            if n == 3:
                lines = ["[1] A. Einstein, Sitzungsber. K. Preuss. Akad. Wiss. 1, 688", "(1916).",
                         "[2] A. Einstein, Sitzungsber. K. Preuss. Akad. Wiss. 1, 154", "(1918).",
                         "[3] P. R. Saulson, Gen. Relativ. Gravit. 43, 3289", "(2011).",
                         "[4] D. Finkelstein, Phys. Rev. 110, 965", "(1958)."]
            pages.append("\n".join(lines))
        assembled, _, _, _ = pdf_parse._assemble(pages)
        self.assertIn("(1916).", assembled[3].text)
        self.assertIn("(1958).", assembled[3].text)


if __name__ == "__main__":
    unittest.main()
