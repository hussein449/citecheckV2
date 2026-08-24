"""The same paper, checked twice, has to say the same thing.

Every test here is about a number or a verdict that moved when nothing about the
citation did. Three separate causes, all of which a reader experiences as the
tool changing its mind:

  * the summary counted references while the cards showed citations, so the two
    genuinely disagreed and the tally looked simply wrong;
  * a citing sentence the model failed to return on was dropped from the
    roll-up, so the headline depended on which calls happened to succeed;
  * a re-check that retrieved nothing overwrote a verdict reached on text that
    *had* been retrieved, so a reference flipped between two answers depending
    on whether the publisher was serving that minute.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from citecheck import match, pipeline


def _entry(key: str, verdict: str, claims: list[str], engine: str = "openai") -> dict:
    return {
        "key": key,
        "reference": {"key": key, "raw": f"Ref {key}", "number": int(key)},
        "citations": [],
        "citation_count": len(claims),
        "verdict": verdict,
        "score": 0.5,
        "reason": "",
        "engine": engine,
        "source": {},
        "fetched": {},
        "shots": {},
        "notes": [],
        "flags": [],
        "claim_verdicts": [
            {"claim": f"claim {i}", "verdict": v, "score": 0.5, "reason": ""}
            for i, v in enumerate(claims)
        ],
    }


def _report(entries: list[dict]) -> dict:
    return {
        "run_id": "20260101-000000-abcdef",
        "source_pdf": "paper.pdf",
        "paper_title": "A paper",
        "stats": {
            "references_checked": len(entries),
            "citations_found": sum(e["citation_count"] for e in entries),
            "references_parsed": len(entries),
            "engine_planned": "openai",
        },
        "references": entries,
        "orphan_keys": [],
        "out_of_range_keys": [],
        "base_warnings": [],
        "warnings": [],
    }


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #

class TallyCountsBothLevelsTest(unittest.TestCase):
    """A reference takes its worst citation's verdict, so the two counts differ.

    This is the whole of the "it says 6 supported but it is more" report: the
    tally answered a question about references while the reader was counting
    citations on the cards, and neither number was reachable from the other.
    """

    def setUp(self):
        # Two references, five citations. Only one reference *headlines* as
        # supported, but four of the five citations are supported.
        self.report = pipeline.summarise(_report([
            _entry("1", "unverified", ["supported", "supported", "unverified"]),
            _entry("2", "supported", ["supported", "supported"]),
        ]))
        self.stats = self.report["stats"]

    def test_the_reference_tally_is_unchanged(self):
        self.assertEqual(self.stats["verdicts"], {"unverified": 1, "supported": 1})

    def test_the_citation_tally_is_reported_alongside_it(self):
        self.assertEqual(self.stats["claim_verdicts"], {"supported": 4, "unverified": 1})

    def test_the_two_counts_are_allowed_to_disagree(self):
        """They answer different questions, and both belong on the summary."""
        self.assertEqual(self.stats["verdicts"]["supported"], 1)
        self.assertEqual(self.stats["claim_verdicts"]["supported"], 4)

    def test_citations_are_counted_where_no_reference_headlines_that_way(self):
        """The tile has to appear, or two supported citations vanish entirely."""
        stats = pipeline.summarise(_report([
            _entry("1", "unverified", ["supported", "unverified"]),
            _entry("2", "weak", ["supported", "weak"]),
        ]))["stats"]
        self.assertNotIn("supported", stats["verdicts"])
        self.assertEqual(stats["claim_verdicts"]["supported"], 2)

    def test_it_is_idempotent(self):
        again = pipeline.summarise(json.loads(json.dumps(self.report)))
        self.assertEqual(again["stats"]["claim_verdicts"], self.stats["claim_verdicts"])

    def test_a_report_with_no_citation_verdicts_reports_an_empty_tally(self):
        stats = pipeline.summarise(_report([
            {**_entry("1", "not_found", []), "claim_verdicts": []},
        ]))["stats"]
        self.assertEqual(stats["claim_verdicts"], {})


class SectionsOverlapTest(unittest.TestCase):
    """A reference belongs in every section its citations fall into.

    Filtering on the headline alone is how a reference that supports four claims
    and cannot settle a fifth disappears from the supported section entirely —
    it is filed once, under its worst citation, and its four good ones are
    unreachable.
    """

    def setUp(self):
        self.report = pipeline.summarise(_report([
            _entry("1", "unverified", ["supported", "supported", "unverified"]),
            _entry("2", "supported", ["supported", "supported"]),
            _entry("3", "weak", ["weak", "related"]),
        ]))
        self.stats = self.report["stats"]

    def test_a_mixed_reference_is_counted_in_both_sections(self):
        holding = self.stats["references_with"]
        self.assertEqual(holding["supported"], 2)   # refs 1 and 2
        self.assertEqual(holding["unverified"], 1)  # ref 1, again

    def test_it_differs_from_the_headline_count(self):
        """Ref 1 headlines as unverified but holds supported citations too."""
        self.assertEqual(self.stats["verdicts"]["supported"], 1)
        self.assertEqual(self.stats["references_with"]["supported"], 2)

    def test_the_sections_are_allowed_to_sum_past_the_reference_count(self):
        self.assertGreater(sum(self.stats["references_with"].values()),
                           len(self.report["references"]))

    def test_every_citation_verdict_reaches_a_section(self):
        for verdict in self.stats["claim_verdicts"]:
            self.assertIn(verdict, self.stats["references_with"])

    def test_a_reference_with_no_citations_falls_back_to_its_headline(self):
        stats = pipeline.summarise(_report([
            {**_entry("1", "not_found", []), "claim_verdicts": []},
        ]))["stats"]
        self.assertEqual(stats["references_with"], {"not_found": 1})

    def test_a_hand_set_headline_is_findable_under_its_own_verdict(self):
        entry = _entry("1", "supported", ["weak", "weak"])
        entry["reviewed"] = {"source": "reference", "verdict": "supported"}
        found = pipeline.verdicts_in(entry)
        self.assertEqual(found, {"weak", "supported"})

    def test_a_headline_rolled_up_from_claims_adds_no_section(self):
        """It is a summary of the citations, not a verdict of its own."""
        entry = _entry("1", "weak", ["weak", "supported"])
        self.assertEqual(pipeline.verdicts_in(entry), {"weak", "supported"})


class MixedCardCountsTest(unittest.TestCase):
    """Reference 109: two citations, both unverified, one re-checked to supported.

    The reported case. What made it look like the counts were stuck is that the
    card wore one verdict for two citations that disagreed — and which verdict it
    wore depended on the engine, because the model tier rolls up to the worst
    citation and the lexical tier to the best. The headline count therefore moved
    for one of them and not the other, while the citation counts moved for both.
    """

    def setUp(self):
        self.before = pipeline.summarise(_report([
            _entry("109", "unverified", ["unverified", "unverified"]),
            _entry("7", "supported", ["supported"]),
            _entry("8", "related", ["related"]),
        ]))
        self.after = pipeline.summarise(_report([
            _entry("109", "unverified", ["unverified", "supported"]),
            _entry("7", "supported", ["supported"]),
            _entry("8", "related", ["related"]),
        ]))

    def test_the_supported_citation_count_goes_up(self):
        self.assertEqual(self.before["stats"]["claim_verdicts"]["supported"], 1)
        self.assertEqual(self.after["stats"]["claim_verdicts"]["supported"], 2)

    def test_the_unverified_citation_count_goes_down(self):
        self.assertEqual(self.before["stats"]["claim_verdicts"]["unverified"], 2)
        self.assertEqual(self.after["stats"]["claim_verdicts"]["unverified"], 1)

    def test_the_card_joins_the_supported_section(self):
        self.assertEqual(self.before["stats"]["references_with"]["supported"], 1)
        self.assertEqual(self.after["stats"]["references_with"]["supported"], 2)

    def test_and_stays_in_the_unverified_one(self):
        """It still holds an unverified citation, so it is still a card there."""
        self.assertEqual(self.after["stats"]["references_with"]["unverified"], 1)
        self.assertIn("109", [
            e["key"] for e in self.after["references"]
            if "unverified" in pipeline.verdicts_in(e)
        ])

    def test_the_headline_count_is_the_one_that_does_not_move(self):
        """Which is why it must not be the number the reader is shown."""
        self.assertEqual(self.before["stats"]["verdicts"]["supported"], 1)
        self.assertEqual(self.after["stats"]["verdicts"]["supported"], 1)

    def test_a_mixed_card_is_reachable_from_both_sections(self):
        entry = self.after["references"][0]
        self.assertEqual(pipeline.verdicts_in(entry), {"unverified", "supported"})


class MixedRollUpDependsOnEngineTest(unittest.TestCase):
    """The same mixed card headlines differently under the two tiers.

    Not a bug in either rule — the model tier will not let one bad citation be
    cancelled by good ones, and the lexical tier will not let low word overlap
    accuse anybody. It is a reason the headline cannot be presented as the
    card's verdict when the citations disagree.
    """

    MIXED = ["unverified", "supported"]

    def test_the_model_tier_rolls_up_to_the_worst(self):
        self.assertEqual(match.roll_up(self.MIXED, "openai"), "unverified")

    def test_the_lexical_tier_rolls_up_to_the_best(self):
        self.assertEqual(match.roll_up(self.MIXED, "lexical"), "supported")

    def test_so_the_headline_alone_never_names_both(self):
        headlines = {match.roll_up(self.MIXED, e) for e in ("openai", "lexical")}
        self.assertEqual(len(headlines), 2)
        for headline in headlines:
            self.assertNotEqual(set(self.MIXED), {headline})


# --------------------------------------------------------------------------- #
# Rolling up
# --------------------------------------------------------------------------- #

class UnjudgedClaimsAreKeptTest(unittest.TestCase):
    """A call that did not come back is not the same as a claim that was fine.

    The model tier headlines a reference as its most concerning citation. When
    an unjudgeable citation was silently dropped, the headline moved according
    to which calls succeeded — the identical reference reading "unverified" on
    one run and "related" on the next, with nothing on the card to explain it.
    """

    CLAIMS = [
        match.Claim(text="Consolidation centres cut last-mile emissions."),
        match.Claim(text="Drone delivery reduces urban congestion."),
        match.Claim(text="Cargo bikes outperform vans below three kilometres."),
    ]
    SOURCE = (
        "Urban consolidation centres and last-mile freight. We measure emissions "
        "from last-mile delivery across four European cities and find that "
        "consolidation centres reduce them substantially over eighteen months. "
        "Cargo bicycles are compared against light goods vehicles on short routes."
    )

    def judge_with(self, verdicts):
        """Judge three claims, with `None` standing for a call that failed."""
        calls = iter(verdicts)

        def fake_match(claim, source_text, title="", reference_line="",
                       abstract="", model=None):
            verdict = next(calls, None)
            if verdict is None:
                return None
            return match.MatchResult(
                engine="openai", verdict=verdict, score=0.8,
                reason=f"Judged {verdict}.",
            )

        real_match, real_engine = match.openai_match, match.active_engine
        match.openai_match = fake_match
        match.active_engine = lambda: "openai"
        try:
            return match.judge(self.CLAIMS, self.SOURCE, title="Last mile freight")
        finally:
            match.openai_match, match.active_engine = real_match, real_engine

    def test_every_citation_gets_a_verdict_even_when_a_call_fails(self):
        result = self.judge_with(["supported", None, "supported"])
        self.assertEqual(len(result.claim_verdicts), 3)
        self.assertEqual(
            [c.verdict for c in result.claim_verdicts],
            ["supported", "unverified", "supported"],
        )

    def test_the_dropped_claim_says_why_it_was_not_judged(self):
        result = self.judge_with(["supported", None, "supported"])
        self.assertIn("could not be judged", result.claim_verdicts[1].reason)

    def test_the_headline_does_not_depend_on_which_calls_came_back(self):
        """The failing call used to decide the headline by its absence."""
        both = self.judge_with(["supported", "unverified", "supported"])
        one_failed = self.judge_with(["supported", None, "supported"])
        self.assertEqual(both.verdict, one_failed.verdict)
        self.assertEqual(one_failed.verdict, "unverified")

    def test_a_real_finding_still_sets_the_headline(self):
        result = self.judge_with(["supported", "unrelated", None])
        self.assertEqual(result.verdict, "unrelated")
        self.assertEqual(result.matched_claim, self.CLAIMS[1].text)

    def test_all_calls_failing_still_falls_back_to_lexical(self):
        result = self.judge_with([None, None, None])
        self.assertEqual(result.engine, "lexical")

    def test_the_roll_up_reason_counts_every_citation(self):
        result = self.judge_with(["supported", None, "supported"])
        self.assertIn("each of the 3 places", result.reason)


class DeterministicSamplingTest(unittest.TestCase):
    """The same claim against the same text must not come back two ways."""

    class Recorder:
        """Stands in for an OpenAI client, recording what it was called with."""

        def __init__(self, reject_sampling=False):
            self.calls = []
            self.reject_sampling = reject_sampling
            self.chat = self
            self.completions = self

        def create(self, **body):
            self.calls.append(body)
            if self.reject_sampling and "temperature" in body:
                raise RuntimeError(
                    "Unsupported value: 'temperature' is not supported with this model."
                )
            return "response"

    def test_sampling_is_pinned_off(self):
        client = self.Recorder()
        match._complete(client, "gpt-4o", "prompt")
        self.assertEqual(client.calls[0]["temperature"], 0)
        self.assertIn("seed", client.calls[0])

    def test_a_model_that_refuses_the_parameters_is_still_judged(self):
        """Losing determinism beats failing the reference over it."""
        client = self.Recorder(reject_sampling=True)
        self.assertEqual(match._complete(client, "o3", "prompt"), "response")
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("temperature", client.calls[1])

    def test_a_real_failure_is_not_retried(self):
        class Broken(DeterministicSamplingTest.Recorder):
            def create(self, **body):
                self.calls.append(body)
                raise RuntimeError("invalid_request_error: schema is malformed")

        client = Broken()
        with self.assertRaises(RuntimeError):
            match._complete(client, "gpt-4o", "prompt")
        self.assertEqual(len(client.calls), 1)


# --------------------------------------------------------------------------- #
# Re-checking
# --------------------------------------------------------------------------- #

class BarrenRecheckTest(unittest.TestCase):
    """A lookup that returned nothing did not overturn anything.

    The reported symptom exactly: a reference read one way, a re-check made it
    another, and the next re-check put it back — because the publisher served
    the text on one attempt and nothing on the next, and the verdict followed
    the retrieval rather than the citation.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        pipeline.save(self.run_dir, _report([
            _entry("12", "unrelated", ["unrelated", "supported"]),
        ]))
        self.options = pipeline.Options(use_model=False, take_screenshots=False)
        self._resolve = pipeline.resolve.resolve

    def tearDown(self):
        pipeline.resolve.resolve = self._resolve
        self._tmp.cleanup()

    def recheck_that_retrieves_nothing(self):
        def dead(_reference):
            raise ConnectionError("publisher unreachable")

        pipeline.resolve.resolve = dead
        return pipeline.recheck_one(self.run_dir, "12", self.options)

    def test_the_earlier_verdict_stands(self):
        entry = self.recheck_that_retrieves_nothing()["references"][0]
        self.assertEqual(entry["verdict"], "unrelated")

    def test_the_earlier_citation_verdicts_stand_with_it(self):
        """Restoring the headline alone would contradict the citations below it."""
        entry = self.recheck_that_retrieves_nothing()["references"][0]
        self.assertEqual(
            [c["verdict"] for c in entry["claim_verdicts"]], ["unrelated", "supported"]
        )

    def test_the_failure_is_recorded_as_a_failure(self):
        entry = self.recheck_that_retrieves_nothing()["references"][0]
        self.assertEqual(entry["rechecked"]["outcome"], "nothing_retrieved")
        self.assertTrue(entry["rechecked"]["detail"])
        self.assertEqual(entry["rechecked"]["previous_verdict"], "unrelated")

    def test_a_hand_set_verdict_survives_it(self):
        """Nothing was displaced, so the reader's own reading still applies."""
        pipeline.set_verdict(self.run_dir, "12", verdict="supported", note="read it")
        entry = self.recheck_that_retrieves_nothing()["references"][0]
        self.assertEqual(entry["verdict"], "supported")
        self.assertEqual(entry["reviewed"]["verdict"], "supported")
        self.assertEqual(entry["rechecked"]["cleared_review"], "")

    def test_the_tally_does_not_move(self):
        report = self.recheck_that_retrieves_nothing()
        self.assertEqual(report["stats"]["verdicts"], {"unrelated": 1})
        self.assertEqual(
            report["stats"]["claim_verdicts"], {"unrelated": 1, "supported": 1}
        )

    def test_repeating_it_never_changes_the_answer(self):
        verdicts = [
            self.recheck_that_retrieves_nothing()["references"][0]["verdict"]
            for _ in range(3)
        ]
        self.assertEqual(verdicts, ["unrelated"] * 3)

    def test_a_reference_never_judged_is_still_allowed_to_change(self):
        """Nothing is being protected here — the guard must not freeze it."""
        pipeline.save(self.run_dir, _report([
            {**_entry("12", "unverified", []), "engine": "", "claim_verdicts": []},
        ]))
        entry = self.recheck_that_retrieves_nothing()["references"][0]
        self.assertEqual(entry["rechecked"]["outcome"], "judged")


class RetrievedNothingTest(unittest.TestCase):
    """"No index has heard of this" is about the reference, not about the lookup."""

    def test_not_found_is_a_finding_not_a_failed_retrieval(self):
        fresh = {**_entry("12", "not_found", []), "engine": "", "claim_verdicts": []}
        self.assertFalse(pipeline._retrieved_nothing(fresh))

    def test_an_unjudged_entry_is_a_failed_retrieval(self):
        fresh = {**_entry("12", "unverified", []), "engine": "", "claim_verdicts": []}
        self.assertTrue(pipeline._retrieved_nothing(fresh))

    def test_a_judged_entry_is_not(self):
        self.assertFalse(pipeline._retrieved_nothing(_entry("12", "weak", ["weak"])))


if __name__ == "__main__":
    unittest.main()
