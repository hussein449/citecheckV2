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

from citecheck import intext, refs

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


if __name__ == "__main__":
    unittest.main()
