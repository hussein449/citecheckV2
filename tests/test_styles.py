"""Citation and bibliography styles the parser has to cope with.

Every case here is written out in full rather than loaded from a PDF, so these
run on any checkout with no files, no network and no keys. They are the standing
answer to "does this work on a paper formatted some other way?".

Three things have to survive for a reference to be checkable at all:

  * the bibliography splits into the right number of entries,
  * each entry yields a *title* — the string every later "is this the work the
    author cited?" test is measured against, so a title that is really an author
    list means the reference resolves to nothing and is reported as not found,
  * in-text markers resolve to the keys those entries are indexed under.
"""

from __future__ import annotations

import unittest

from citecheck import intext, pdf_parse, refs

# ── Bibliography styles ──────────────────────────────────────────────────────
# style -> (bibliography text, [(expected key, expected title fragment), …])
BIBLIOGRAPHIES = {
    "apa": (
        """
        Bosona, T. (2020). Urban freight last mile logistics. Logistics, 4(4), 24.
        Eissfeldt, H., & Biella, M. (2022). Drone acceptance in Germany. Aviation, 26(1), 1-9.
        """,
        [("bosona2020", "Urban freight last mile"),
         ("eissfeldt2022", "Drone acceptance in Germany")],
    ),
    "harvard": (
        """
        Bosona, T., 2020. Urban freight last mile logistics. Logistics, 4(4), pp.24-38.
        Sah, B., Gupta, R. and Bani-Hani, D., 2021. Analysis of barriers. Journal, 12, pp.1-20.
        """,
        [("bosona2020", "Urban freight last mile"),
         ("sah2021", "Analysis of barriers")],
    ),
    "vancouver": (
        """
        1. Bosona T. Urban freight last mile logistics. Logistics. 2020;4(4):24-38.
        2. Eissfeldt H, Biella M, Schmidt A, et al. Drone acceptance. Aviation. 2022;26(1):1-9.
        3. Munn Z, Stern C. What kind of review should I conduct? BMC Med Res Methodol. 2018;18:5.
        """,
        [("1", "Urban freight last mile"),
         ("2", "Drone acceptance"),
         ("3", "What kind of review should I conduct?")],
    ),
    "ieee": (
        """
        [1] T. Bosona, "Urban freight last mile logistics," Logistics, vol. 4, no. 4, pp. 24-38, 2020.
        [2] J.-M. Sullivan and H.-Y. Kim, "Truck-drone routing," in Proc. ICRA, 2019, pp. 45-52.
        """,
        [("1", "Urban freight last mile"), ("2", "Truck-drone routing")],
    ),
    "acm": (
        """
        [1] Tesfaye Bosona. 2020. Urban freight last mile logistics. Logistics 4, 4 (2020), 24-38.
        [2] Hinnerk Eissfeldt and Marcus Biella. 2022. Drone acceptance. Aviation 26, 1 (2022), 1-9.
        """,
        [("1", "Urban freight last mile"), ("2", "Drone acceptance")],
    ),
    "nature": (
        """
        1. Bosona, T. Urban freight last mile logistics. Logistics 4, 24-38 (2020).
        2. Sullivan, J. M., Xiaoning, Z. & Kim, H.-Y. Truck-drone routing. Nature 512, 45-52 (2019).
        """,
        [("1", "Urban freight last mile"), ("2", "Truck-drone routing")],
    ),
    "chicago": (
        """
        Bosona, Tesfaye. "Urban Freight Last Mile Logistics." Logistics 4, no. 4 (2020): 24-38.
        Eissfeldt, Hinnerk, and Marcus Biella. "Drone Acceptance." Aviation 26, no. 1 (2022): 1-9.
        """,
        [("bosona2020", "Urban Freight Last Mile"), ("eissfeldt2022", "Drone Acceptance")],
    ),
    "mla": (
        """
        Bosona, Tesfaye. "Urban Freight Last Mile Logistics." Logistics, vol. 4, 2020, pp. 24-38.
        Eissfeldt, Hinnerk, and Marcus Biella. "Drone Acceptance." Aviation, vol. 26, 2022, pp. 1-9.
        """,
        [("bosona2020", "Urban Freight Last Mile"), ("eissfeldt2022", "Drone Acceptance")],
    ),
    "elsevier_numbered": (
        """
        [1] T. Bosona, Urban freight last mile logistics, Logistics 4 (2020) 24-38.
        [2] H. Eissfeldt, M. Biella, Drone acceptance in Germany, Aviation 26 (2022) 1-9.
        """,
        [("1", "Urban freight last mile"), ("2", "Drone acceptance in Germany")],
    ),
    "springer_numbered": (
        """
        1. Bosona, T.: Urban freight last mile logistics. Logistics 4(4), 24-38 (2020)
        2. Eissfeldt, H., Biella, M.: Drone acceptance. Aviation 26(1), 1-9 (2022)
        """,
        [("1", "Urban freight last mile"), ("2", "Drone acceptance")],
    ),
    # The style that broke the drone review: no entry numbers, initials before
    # the surname, and no period after the initials.
    "initials_first": (
        """
        N Agatz, P Bouman & M Schmidt (2018) Optimization approaches for the TSP. Transp Sci.
        KW Chen, MR Xie, YM Chen, et al. (2022) DroneTalk: an IoT drone system. IEEE IoT J.
        """,
        [("agatz2018", "Optimization approaches"), ("chen2022", "DroneTalk")],
    ),
    # Given names spelled out in full, so the surname is not the first word.
    "spelled_out_given_names": (
        """
        Seyed Mahdi Shavarani, M. G. Nejad & G. Izbirak (2018) Hierarchical facility location. Springer.
        David C. Edwards, N. Subramanian & W. Zeng (2023) Drones for humanitarian aid. IJPDLM.
        """,
        [("seyedmahdishavarani2018", "Hierarchical facility location"),
         ("davidedwards2023", "Drones for humanitarian aid")],
    ),
}


class BibliographyStyleTest(unittest.TestCase):
    """Each style must split into entries that carry a real title."""


def _make_bibliography_test(style, text, expected):
    def test(self):
        parsed = refs.parse_references(text)
        self.assertEqual(
            len(parsed), len(expected),
            f"{style}: split into {len(parsed)} entries, expected {len(expected)}\n"
            + "\n".join(f"  - {r.raw[:80]}" for r in parsed),
        )
        for ref, (key, title_fragment) in zip(parsed, expected):
            self.assertEqual(ref.key, key, f"{style}: wrong key for {ref.raw[:60]!r}")
            # The title is what the reference is looked up by. An author list
            # here resolves to nothing and is reported as "not found".
            self.assertIn(
                title_fragment.lower(), (ref.title or "").lower(),
                f"{style}: title came out as {ref.title!r}",
            )
            self.assertTrue(ref.year, f"{style}: no year for {ref.raw[:60]!r}")

    test.__name__ = f"test_{style}"
    return test


for _style, (_text, _expected) in BIBLIOGRAPHIES.items():
    setattr(BibliographyStyleTest, f"test_{_style}",
            _make_bibliography_test(_style, _text, _expected))


# ── In-text marker styles ────────────────────────────────────────────────────

class InTextMarkerTest(unittest.TestCase):
    """Markers have to be found, expanded, and reduced to one scheme."""

    def keys(self, text):
        return sorted({c.key for c in intext.extract_citations(text)})

    def test_numeric_forms(self):
        text = (
            "Drones cut delivery time [1]. Costs fall too [2, 3]. "
            "Several trials agree [5-8]. Others disagree [10,12-14]. "
            "A further study concurs [20]. And another [21]."
        )
        self.assertEqual(
            self.keys(text),
            sorted(["1", "2", "3", "5", "6", "7", "8", "10", "12", "13", "14", "20", "21"]),
        )

    def test_author_year_forms(self):
        text = (
            "Drones cut delivery time (Bosona, 2020). Costs fall too "
            "(Sorbelli, 2024; Li et al., 2022a). Acceptance varies "
            "(Eissfeldt & Biella, 2022). Shavarani et al. (2018) agree. "
            "A hybrid model was proposed (Rojas Viloria et al., 2021)."
        )
        self.assertEqual(
            self.keys(text),
            sorted(["bosona2020", "sorbelli2024", "li2022",
                    "eissfeldt2022", "shavarani2018", "rojas2021"]),
        )

    def test_accented_surnames_survive(self):
        # An ASCII-only name class stops at the first accent and loses the
        # marker outright rather than merely truncating it.
        text = ("Acceptance differs (Eißfeldt & Biella, 2022) and so does cost "
                "(Muñoz-Villamizar, 2021), while Osório et al. (2019) disagree.")
        self.assertEqual(
            self.keys(text),
            sorted(["eifeldt2022", "muozvillamizar2021", "osrio2019"]),
        )

    def test_table_row_ids_do_not_beat_real_citations(self):
        """The drone review's failure, reduced to its essentials.

        A summary table numbering its rows "[126]" must not convince the parser
        the paper is numeric and discard every author-year citation in it.
        """
        table = " ".join(f"[{n}] Hub location model Heuristic" for n in range(101, 130))
        prose = " ".join(
            f"A trial found a benefit (Smith{chr(65 + n)}, 20{10 + n % 15})."
            for n in range(40)
        )
        styles = {c.style for c in intext.extract_citations(table + " " + prose)}
        self.assertEqual(styles, {"author-year"})

    def test_numeric_paper_discards_year_asides(self):
        """The mirror case: a numeric paper's parenthetical years are noise."""
        text = (
            "Delivery improved [1]. Trials ran [2, 3]. Costs fell [4]. "
            "Emissions dropped [5]. Adoption grew [6]. "
            "The programme (running 2019) and its successor (see 2021) helped."
        )
        styles = {c.style for c in intext.extract_citations(text)}
        self.assertEqual(styles, {"numeric"})

    def test_sentence_capture_is_not_broken_by_abbreviations(self):
        text = "Drones fly approx. 20 km, per Fig. 3 and e.g. Smith et al. [7], which is far."
        citations = intext.extract_citations(text)
        self.assertEqual(len(citations), 1)
        self.assertIn("approx. 20 km", citations[0].sentence)


# ── Marker ↔ bibliography linking ────────────────────────────────────────────

class LinkingTest(unittest.TestCase):
    """Markers must reach their entry however the name is printed."""

    def link(self, bibliography, markers):
        reference_list = refs.parse_references(bibliography)
        grouped = intext.group_by_reference(intext.extract_citations(markers))
        return refs.link_citations(grouped, refs.index_references(reference_list))

    def test_author_year_marker_finds_numbered_entry(self):
        """Word's numbered-list styling numbers a bibliography cited by name."""
        matched, orphans = self.link(
            """
            1. Bosona, T. Urban freight last mile logistics. Logistics 4, 24-38 (2020).
            2. Eissfeldt, H. & Biella, M. Drone acceptance. Aviation 26, 1-9 (2022).
            """,
            "Costs fall (Bosona, 2020) and acceptance varies (Eissfeldt & Biella, 2022).",
        )
        self.assertEqual(orphans, [])
        self.assertEqual(sorted(matched), ["bosona2020", "eissfeldt2022"])

    def test_surname_reaches_entry_whatever_the_author_order(self):
        for entry in (
            "Shavarani, S. M., Nejad, M. G. (2018). Hierarchical facility location. Springer.",
            "SM Shavarani, MG Nejad (2018) Hierarchical facility location. Springer.",
            "Seyed Mahdi Shavarani, M. G. Nejad (2018) Hierarchical facility location. Springer.",
        ):
            with self.subTest(entry=entry[:40]):
                matched, orphans = self.link(
                    entry + "\nOther, A. (1999) Something else entirely. Journal.",
                    "A model was proposed (Shavarani et al., 2018).",
                )
                self.assertEqual(orphans, [], f"unmatched for {entry[:40]!r}")
                self.assertEqual(len(matched), 1)

    def test_ambiguous_surname_year_is_reported_not_guessed(self):
        """Two different Li 2022 papers: unmatched beats matched-to-the-wrong-one."""
        matched, orphans = self.link(
            """
            X Li, Y Wang (2022) Truck and drone routing with synchronization. Transp Res.
            Q Li, R Zhao (2022) Application of UAVs in logistics: a review. Drones.
            """,
            "Routing has been studied (Li et al., 2022).",
        )
        self.assertEqual(matched, {})
        self.assertEqual(orphans, ["li2022"])

    def test_double_barrelled_surname_matches_either_half(self):
        bibliography = (
            "D Rojas Viloria, EL Solano-Charris (2021) UAVs in vehicle routing. Networks.\n"
            "Other, A. (1999) Something else entirely. Journal.\n"
        )
        for marker in ("(Rojas Viloria et al., 2021)", "(Viloria et al., 2021)"):
            with self.subTest(marker=marker):
                matched, orphans = self.link(bibliography, f"A survey exists {marker}.")
                self.assertEqual(orphans, [], f"unmatched for {marker}")
                self.assertEqual(len(matched), 1)

    def test_given_name_first_with_year_sentence(self):
        """ACL: given names spelled out, the year a sentence of its own.

        Written the way a justified column extracts — author lists wrapped and
        broken by blank lines — so neither line starts nor blocks mark entries.
        """
        bibliography = """
            Gabor Angeli and Christopher D. Manning. 2014. NaturalLI: Natural logic inference for common sense
            reasoning. In Proceedings of the 2014 Conference
            on Empirical Methods in Natural Language Processing. pages 534-545.

            Danqi Chen, Adam Fisch, Jason Weston, and Antoine

            Bordes. 2017. Reading wikipedia to answer open-domain questions. In Proceedings of ACL (Volume 1:
            Long Papers). https://doi.org/10.18653/v1/P17-1171.

            Joseph L Fleiss. 1971. Measuring nominal scale agreement among many raters. Psychological bulletin
            76(5):378.

            P. Rajpurkar, J. Zhang, K. Lopyrev, and P. Liang. 2016.
            SQuAD: 100,000+ questions for machine comprehension of text. In Empirical Methods in Natural
            Language Processing (EMNLP).

            Tim Rocktäschel and Sebastian Riedel. 2017. End-to-end differentiable proving. CoRR abs/1705.11040.
        """
        reference_list = refs.parse_references(bibliography)
        self.assertEqual(
            [r.title for r in reference_list],
            [
                "NaturalLI: Natural logic inference for common sense reasoning",
                "Reading wikipedia to answer open-domain questions",
                "Measuring nominal scale agreement among many raters",
                "SQuAD: 100,000+ questions for machine comprehension of text",
                "End-to-end differentiable proving",
            ],
        )
        # Keyed on the surname a marker prints, not on the given name before it.
        self.assertEqual(
            [r.key for r in reference_list][:4],
            ["angeli2014", "chen2017", "fleiss1971", "rajpurkar2016"],
        )
        matched, orphans = self.link(
            bibliography,
            "Shown before (Angeli and Manning, 2014; Chen et al., 2017; Fleiss, 1971). "
            "Rajpurkar et al. (2016) and others (Rocktäschel and Riedel, 2017) agree.",
        )
        self.assertEqual(orphans, [])
        self.assertEqual(len(matched), 5)


    def test_given_name_is_not_read_as_an_initial(self):
        """ "Ido Dagan" is a name, not the initial "I" and a surname "do Dagan"."""
        (ref,) = refs.parse_references(
            "Ido Dagan and Dan Roth. 2009. Recognizing textual entailment. Venue 15(4)."
        )
        self.assertEqual(ref.key, "dagan2009")

    def test_exclamation_inside_a_title_does_not_end_it(self):
        (ref,) = refs.parse_references(
            "Michael Heilman and Noah A. Smith. 2010. Good Question! statistical "
            "ranking for question generation. In Proceedings of NAACL. pages 609-617."
        )
        self.assertEqual(
            ref.title, "Good Question! statistical ranking for question generation"
        )


class ExtractionCleanupTest(unittest.TestCase):
    """Repairs made to raw PDF text before anything reads citations out of it."""

    def test_loose_latex_accents_are_composed(self):
        text = pdf_parse._normalise_whitespace("Tim Rockt¨aschel and ´Alvaro Rodrigo")
        self.assertEqual(text, "Tim Rocktäschel and Álvaro Rodrigo")

    def test_lettered_appendix_ends_the_bibliography(self):
        entries = [f"Author{c} Name. 201{i}. A title of some kind. Venue." for i, c in enumerate("abcdefgh")]
        appendix = ["A", "Annotation Guidelines", "A.1", "Task Definitions",
                    "1. Rephrase the claim.", "2. Negate the claim."]
        body = [f"Body line {i}." for i in range(20)]
        page = pdf_parse.Page(1, "\n".join(body + ["References"] + entries + appendix))
        _, references, _ = pdf_parse._split_references([page])
        self.assertEqual(references.splitlines(), entries)

    def test_table_column_headed_reference_is_not_the_bibliography(self):
        """A "Reference" column in a table after the list must not win."""
        entries = [f"[{i}] Smith, A. B. A title of some kind. Journal {i}, 1-9 (201{i})." for i in range(1, 8)]
        table = ["Acknowledgements", "We thank everyone.", "Method", "Type", "Reference",
                 "RuleSet1", "On-target", "7", "Azimuth", "On-target", "6"]
        body = [f"Body line {i}." for i in range(20)]
        page = pdf_parse.Page(1, "\n".join(body + ["References"] + entries + table))
        _, references, _ = pdf_parse._split_references([page])
        self.assertEqual(references.splitlines(), entries)

    def test_superscript_citations_are_bracketed(self):
        def span(text, size=10.0, flags=4):
            return {"text": text, "size": size, "flags": flags}

        raised = lambda text: span(text, 7.0, 5)
        cited = {"spans": [span("statistical inference"), raised("1"), raised("–"),
                           raised("5"), span(". Next", flags=5)]}
        self.assertEqual(pdf_parse._line_text(cited, 7.0), "statistical inference[1–5]. Next")
        # A name ending in a digit keeps it; only the raised number is a marker.
        named = {"spans": [span("RuleSet1"), raised("7"), span(" and Cas9")]}
        self.assertEqual(pdf_parse._line_text(named, 7.0), "RuleSet1[7] and Cas9")
        # Exponents, primes and affiliation marks set at another size are not.
        exponent = {"spans": [span("m", flags=6), raised("2")]}
        self.assertEqual(pdf_parse._line_text(exponent, 7.0), "m2")
        prime = {"spans": [span("the 5"), raised("′"), span(" end")]}
        self.assertEqual(pdf_parse._line_text(prime, 7.0), "the 5′ end")
        affiliation = {"spans": [span("Hoberecht"), span("1", 8.0, 5)]}
        self.assertEqual(pdf_parse._line_text(affiliation, 7.0), "Hoberecht1")

    def test_title_opening_with_a_is_not_an_appendix(self):
        entries = [f"Author{c} Name. 201{i}. A title of some kind. Venue." for i, c in enumerate("abcdefgh")]
        entries[6:7] = ["Authorg Name. 2016.", "A", "Simple baseline. Venue."]
        body = [f"Body line {i}." for i in range(20)]
        page = pdf_parse.Page(1, "\n".join(body + ["References"] + entries))
        _, references, _ = pdf_parse._split_references([page])
        self.assertEqual(references.splitlines(), entries)


if __name__ == "__main__":
    unittest.main()
