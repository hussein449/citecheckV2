"""Run the whole check end to end and stream progress as it goes.

parse PDF -> find in-text citations -> parse bibliography -> for each cited
reference: resolve a URL, fetch the source, judge the claim against it, and
screenshot the header plus the matching passage.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import crosscheck, fetch, intext, match, pdf_parse, refs, resolve, shots

Progress = Callable[[dict], None]

# Serial cost of one reference, used only for the up-front estimate.
_SERIAL_SECONDS_PER_REF = 20.0


@dataclass
class Options:
    max_references: int = 40
    use_model: bool = True
    take_screenshots: bool = True
    workers: int = 4
    # Each citing sentence is judged on its own, so this bounds the model spend
    # on a reference that is cited a dozen times.
    max_claims_per_reference: int = 6


@dataclass
class Report:
    run_id: str
    source_pdf: str
    paper_title: str = ""
    stats: dict = field(default_factory=dict)
    references: list[dict] = field(default_factory=list)
    orphan_keys: list[str] = field(default_factory=list)
    # Markers whose number is past the end of the bibliography — "[126]" in a
    # paper with 40 entries. Almost always a table row label rather than a
    # citation, so they are reported apart from genuine unmatched markers.
    out_of_range_keys: list[str] = field(default_factory=list)
    # Warnings raised while reading the PDF. Anything a *reference* is
    # responsible for is derived from the entries by `summarise`, so
    # re-checking one reference can never leave a stale complaint behind.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """The finished report document.

        `summarise` runs here rather than at the one call site that happens to
        need it, so no caller can serve a half-built document: the tally, the
        screening banner and the reference-level warnings are all derived from
        the entries, and a copy of the report without them looks like a clean
        result. It is idempotent, so calling this twice costs nothing.
        """
        return summarise({
            "run_id": self.run_id,
            "source_pdf": self.source_pdf,
            "paper_title": self.paper_title,
            "stats": self.stats,
            "references": self.references,
            "orphan_keys": self.orphan_keys,
            "out_of_range_keys": self.out_of_range_keys,
            "base_warnings": list(self.warnings),
        })


def _noop(_event: dict) -> None:
    return None


def run(pdf_path: str, run_dir: Path, options: Options, progress: Progress = _noop) -> Report:
    run_dir = Path(run_dir)
    shots_dir = run_dir / "shots"
    run_dir.mkdir(parents=True, exist_ok=True)

    report = Report(run_id=run_dir.name, source_pdf=Path(pdf_path).name)
    started = time.time()

    progress({"stage": "parse", "message": "Reading the PDF…", "percent": 3})
    parsed = pdf_parse.parse_pdf(pdf_path)
    report.paper_title = parsed.meta.get("title") or Path(pdf_path).stem

    if not parsed.references_text.strip():
        report.warnings.append(
            "No bibliography heading was found, so references could not be matched "
            "to their in-text markers."
        )

    progress({"stage": "citations", "message": "Finding in-text citations…", "percent": 10})
    citations = intext.extract_citations(parsed.body_text, parsed.page_of_offset)
    grouped = intext.group_by_reference(citations)

    progress({"stage": "references", "message": "Parsing the reference list…", "percent": 16})
    reference_list = refs.parse_references(parsed.references_text)
    ref_index = refs.index_references(reference_list)
    matched, orphans = refs.link_citations(grouped, ref_index)

    # A numeric marker past the last entry in the bibliography is not a citation
    # anyone could follow: it is a summary table's row label, a dataset id or a
    # measurement in square brackets. Counting those as unmatched citations
    # inflates the orphan warning with noise and buries the markers that really
    # are numbering slips.
    unmatched, out_of_range = _split_out_of_range(orphans, reference_list)
    report.orphan_keys = sorted(unmatched, key=_sort_key)
    report.out_of_range_keys = sorted(out_of_range, key=_sort_key)

    if unmatched:
        report.warnings.append(
            f"{len(unmatched)} citation marker(s) had no matching bibliography entry: "
            + ", ".join(sorted(unmatched, key=_sort_key)[:12])
        )
    if out_of_range:
        report.warnings.append(
            f"{len(out_of_range)} bracketed number(s) were ignored as non-citations "
            "— they sit past the end of the bibliography, which is what a table's "
            "row labels look like: "
            + ", ".join(f"[{k}]" for k in sorted(out_of_range, key=_sort_key)[:12])
        )

    ordered_keys = sorted(matched.keys(), key=_sort_key)
    capped = ordered_keys[: options.max_references]
    if len(ordered_keys) > len(capped):
        report.warnings.append(
            f"Checked the first {len(capped)} of {len(ordered_keys)} cited references "
            "(raise the limit to check them all)."
        )

    report.stats = {
        "pages": len(parsed.pages),
        "citations_found": len(citations),
        "references_parsed": len(reference_list),
        "references_cited": len(ordered_keys),
        "references_checked": len(capped),
        # What the model tier *can* do, decided before any call is made. The
        # `engine` key below is overwritten once the run is done with what
        # actually produced the verdicts — the two differ whenever the key is
        # configured but rejected, and that difference is the whole point.
        "engine_planned": match.active_engine() if options.use_model else "lexical",
    }
    report.stats["engine"] = report.stats["engine_planned"]
    # Measured ~5s of wall clock per reference at 4 workers, i.e. roughly 20s of
    # serial work each (resolve, fetch, judge, three screenshots) once network
    # waits are overlapped. Divide that serial cost by the worker count — using
    # the wall-clock figure directly would double-count the parallelism.
    eta = _format_eta(len(capped) * _SERIAL_SECONDS_PER_REF / max(1, options.workers))
    report.stats["eta"] = eta
    progress({
        "stage": "plan",
        "message": (
            f"Found {len(citations)} in-text citations across {len(ordered_keys)} references. "
            f"Checking {len(capped)} — about {eta}."
        ),
        "percent": 20,
        "stats": report.stats,
    })

    total = max(1, len(capped))
    done = 0
    results: dict[str, dict] = {}

    duplicates = crosscheck.find_duplicates(matched)

    # key -> (stage, detail) for whatever each reference is doing right now.
    # Single assignments under the GIL, so no lock is needed.
    stages: dict[str, tuple[str, str]] = {}

    def work(key: str) -> tuple[str, dict]:
        return key, _check_one(
            key=key,
            reference=matched[key],
            citations=grouped[key],
            shots_dir=shots_dir,
            options=options,
            duplicate_of=duplicates.get(key, ""),
            paper_path=pdf_path,
            on_stage=lambda stage, detail: stages.__setitem__(key, (stage, detail)),
        )

    workers = max(1, options.workers)
    budget = _check_budget(len(capped), workers)

    # Not a context manager: its __exit__ joins every worker, and the whole
    # point here is to survive a worker that will never finish.
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {pool.submit(work, key): key for key in capped}
    try:
        # Report each reference as it finishes, not in reference order. `map`
        # yields strictly in submission order, so one slow reference holds back
        # every result queued behind it — the workers keep going, but the
        # progress log sits on the same line for minutes and reads as a hang.
        #
        # The report itself is unaffected: `report.references` is rebuilt in
        # reference order below, from `results`.
        for future in as_completed(pending, timeout=budget):
            key, entry = future.result()
            results[key] = entry
            done += 1
            progress({
                "stage": "check",
                "message": f"[{entry['reference'].get('number') or key}] {entry['verdict']} — "
                           f"{_short(entry['reference'].get('title') or entry['reference'].get('raw', ''))}",
                "percent": 20 + int(74 * done / total),
                "entry": entry,
            })

        # Each worker holds its own browser and only that thread may close it.
        # A barrier occupies every worker at once, so each runs exactly one
        # cleanup task — otherwise a fast thread could take them all and the
        # rest would leak a Chrome process per run. Skipped when the budget
        # blew, because a wedged worker can never reach the barrier.
        if options.take_screenshots:
            _shutdown_browsers(pool, workers)
    except FutureTimeout:
        for future, key in pending.items():
            if future.done() or key in results:
                continue
            entry = _timed_out_entry(
                key, matched[key], grouped[key], duplicates.get(key, ""), budget,
                stages.get(key, ("", "")),
            )
            results[key] = entry
            done += 1
            progress({
                "stage": "check",
                "message": f"[{entry['reference'].get('number') or key}] timed out — "
                           f"{_short(entry['reference'].get('title') or entry['reference'].get('raw', ''))}",
                "percent": 20 + int(74 * done / total),
                "entry": entry,
            })
    finally:
        # A thread blocked in a socket or a browser call cannot be interrupted
        # from here, so never wait on one: cancel what has not started and let
        # the process reap the rest.
        pool.shutdown(wait=False, cancel_futures=True)

    report.references = [results[k] for k in capped if k in results]
    report.stats["elapsed_seconds"] = round(time.time() - started, 1)

    data = report.to_dict()
    save(run_dir, data)
    progress({"stage": "done", "message": "Finished.", "percent": 100, "report": data})
    return report


def _split_out_of_range(
    orphans: list[str], reference_list: list[refs.Reference]
) -> tuple[list[str], list[str]]:
    """Separate unmatched markers from bracketed numbers that cite nothing.

    Only applied to numeric keys, and only when the bibliography both numbers
    its entries and parsed cleanly enough for its last number to mean anything.

    That second condition is what keeps this safe. A bibliography that parsed
    down to a scattered 40 of its 150 entries has a "highest number" of maybe
    148 or maybe 61, and measuring markers against it would quietly reclassify
    real citations as table debris — turning a parsing failure into a silent
    loss of coverage, which is worse than the noisy orphan list it replaces. So
    the ceiling is trusted only when the parsed entries actually run up to it.
    """
    numbers = sorted({ref.number for ref in reference_list if ref.number is not None})
    if len(numbers) < 3:
        return list(orphans), []

    ceiling = numbers[-1]
    if numbers[0] > 2 or len(numbers) < ceiling * 0.8:
        return list(orphans), []
    unmatched: list[str] = []
    out_of_range: list[str] = []
    for key in orphans:
        if key.isdigit() and int(key) > ceiling:
            out_of_range.append(key)
        else:
            unmatched.append(key)
    return unmatched, out_of_range


def save(run_dir: Path, report: dict) -> None:
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def summarise(report: dict) -> dict:
    """Recompute the run-level tally, warnings and headline from the entries.

    Split out of `run` because a reader can re-check one reference on its own,
    and a report whose banner still counts a verdict that has since been
    overturned is worse than one that was never re-checked at all. Everything
    here is derived, so running it twice on the same entries changes nothing.
    """
    stats = report.setdefault("stats", {})
    references = report.get("references") or []

    # Reports written before per-reference re-checking existed kept only the
    # merged warning list; treat that as the base rather than losing it.
    base = report.get("base_warnings")
    if base is None:
        base = list(report.get("warnings") or [])
        report["base_warnings"] = base

    verdicts: dict[str, int] = {}
    # Counted separately from the reference headlines above, and never folded
    # into them. A reference headlines as the *most concerning* of the citations
    # beneath it, so one cited five times that supports four and oversells the
    # fifth is counted once, as the fifth — and its four supported citations
    # appear nowhere in a reference-level tally. Those four are on the card the
    # reader is looking at, which is why a tally that omits them reads as simply
    # wrong. Both numbers are kept so the summary can show both.
    claim_verdicts: dict[str, int] = {}
    # How many references carry at least one citation of each verdict. This is
    # what a reader is actually asking for when they filter to "unverified" on a
    # report whose references are cited several times each: a reference that
    # supports four claims and cannot settle a fifth belongs under *both*
    # headings, and filtering on the headline alone hides its four good
    # citations behind its worst one. Sections built from this overlap, so the
    # counts here deliberately sum to more than the number of references.
    containing: dict[str, int] = {}
    derived: list[str] = []
    flagged = retracted = not_found = claims_judged = rechecked = reviewed = 0
    claims_reviewed = 0

    for entry in references:
        verdicts[entry["verdict"]] = verdicts.get(entry["verdict"], 0) + 1
        for claim in entry.get("claim_verdicts") or []:
            claim_verdicts[claim["verdict"]] = claim_verdicts.get(claim["verdict"], 0) + 1
        for verdict in verdicts_in(entry):
            containing[verdict] = containing.get(verdict, 0) + 1
        claims_judged += len(entry.get("claim_verdicts") or [])
        if (entry.get("source") or {}).get("retracted"):
            retracted += 1
        if entry["verdict"] == "not_found":
            not_found += 1
        if entry.get("rechecked"):
            rechecked += 1
        if entry.get("reviewed"):
            reviewed += 1
        claims_reviewed += sum(
            1 for c in entry.get("claim_verdicts") or [] if c.get("override")
        )
        if entry.get("timed_out") and entry.get("notes"):
            derived.append(f"[{entry['key']}] {entry['notes'][0]}")
        for flag in entry.get("flags", []):
            if flag["severity"] == "high":
                flagged += 1
                derived.append(f"[{entry['key']}] {flag['message']}")
                break

    stats["verdicts"] = verdicts
    stats["claim_verdicts"] = claim_verdicts
    stats["references_with"] = containing
    stats["flagged"] = flagged
    stats["retracted"] = retracted
    stats["not_found"] = not_found
    stats["claims_judged"] = claims_judged
    stats["rechecked"] = rechecked
    stats["reviewed"] = reviewed
    stats["claims_reviewed"] = claims_reviewed
    stats.update(_engine_outcome(references, stats, derived))
    stats["risk"] = risk_summary(stats)

    report["warnings"] = list(dict.fromkeys(base + derived))
    return report


def verdicts_in(entry: dict) -> set[str]:
    """Every verdict a reference carries, not just the one on its headline.

    A reference cited five times holds five judgements, and the headline is only
    the most concerning of them. Asking "which sections does this belong in"
    with the headline gives one answer where there are several, which is how a
    card supporting four claims disappears from the supported section entirely.

    A reference with no citation-level verdicts has nothing but its headline, so
    that is what it carries. A headline the reader set by hand is included even
    when the citations disagree with it, because it is their answer for the card
    and it has to be findable under the verdict they chose.
    """
    claims = entry.get("claim_verdicts") or []
    found = {c["verdict"] for c in claims}
    if not claims or (entry.get("reviewed") or {}).get("source") == "reference":
        found.add(entry.get("verdict", ""))
    return {v for v in found if v}


# How each engine is named in prose written for the reader.
_ENGINE_DISPLAY = {"openai": "OpenAI", "lexical": "lexical word overlap"}


def _engine_outcome(references: list[dict], stats: dict, warnings: list[str]) -> dict:
    """Report the engine that actually produced the verdicts, not the one configured.

    Selecting an engine up front and printing that as the run's headline is how a
    report ends up saying "judged by <model>" when the API key was rejected and
    every verdict below it is word overlap. The reader has no way to tell those
    two runs apart, and the failure hides in the per-reference small print.

    So the headline is derived after the fact: a reference only counts as judged
    by the model if a model verdict came back for it. References that were never
    resolved never reached the judge at all, and are excluded from the tally
    rather than counted as failures.
    """
    eligible = [entry for entry in references if entry.get("engine")]
    judged = [entry for entry in eligible if entry["engine"] != "lexical"]
    planned = stats.get("engine_planned", "lexical")
    named = _ENGINE_DISPLAY.get(planned, planned)

    outcome = {
        "engine": judged[0]["engine"] if judged else "lexical",
        "references_judged_by_model": len(judged),
        "references_judgeable": len(eligible),
        "engine_note": "",
    }

    if planned == "lexical" or not eligible:
        return outcome

    if not judged:
        outcome["engine_note"] = (
            f"{named} judging was enabled but produced no verdicts — every result "
            "below is lexical word overlap, which can show that two texts discuss "
            "the same thing but never that a citation is wrong. The per-reference "
            "reasons say why each call failed."
        )
        warnings.append(outcome["engine_note"])
    elif len(judged) < len(eligible):
        outcome["engine_note"] = (
            f"{named} judged {len(judged)} of {len(eligible)} references with "
            "retrievable text; the rest fell back to lexical word overlap."
        )

    return outcome


# Findings that decide the screening headline, worst first. Each is
# (key in stats, level, singular template, plural template).
_RISK_RULES = (
    ("retracted", "critical",
     "{n} reference is retracted", "{n} references are retracted"),
    ("not_found", "critical",
     "{n} reference could not be found in any index",
     "{n} references could not be found in any index"),
)


def risk_summary(stats: dict) -> dict:
    """A one-line screening judgement for the top of the report.

    Deliberately built from findings a reader can check, not from a weighted
    score — an editor needs to know *what* is wrong, and an opaque number out of
    100 invites arguments no one can settle.

    Takes a plain stats dict rather than a Report so the document renderer can
    derive a summary for a run that predates this field, instead of defaulting
    to "clear" and telling a client everything is fine when it is not.
    """
    # Counted from the citations, not from the reference headlines. A headline
    # is a roll-up of several citations and the two tiers roll it up in opposite
    # directions, so a reference cited twice — once unrelated, once supported —
    # headlines "supported" under lexical judging and the unrelated citation
    # disappears from this banner entirely. That put a green "No integrity
    # problems were found" on top of a report holding citations that do not say
    # what they are cited for, which is the one sentence a reader acts on and
    # the one failure they cannot detect. It also meant the banner never moved
    # when a re-check changed a citation, because it was not reading citations.
    #
    # Reports written before per-citation verdicts existed have no such data, so
    # they keep the old reference-level reading rather than silently reporting
    # zero — and the prose below names whichever one it is actually counting.
    citations = stats.get("claim_verdicts") or {}
    verdicts = citations or stats.get("verdicts", {})
    noun = "citation" if citations else "reference"
    checked = max(1, (stats.get("claims_judged", 0) if citations
                      else stats.get("references_checked", 0)))

    # A run that checked nothing has found nothing, which is not the same as
    # having found nothing wrong. Falling through to "clear" here puts a green
    # "No integrity problems found" on top of a report whose own subtitle reads
    # "0 of 0 cited references checked" — the one outcome a reader is most
    # likely to act on and least able to detect. Say which stage came up empty,
    # because that is what tells them whether to trust the paper or re-run it.
    if not stats.get("references_checked", 0):
        if not stats.get("citations_found", 0):
            why = ("no in-text citation markers were recognised in the body "
                   "text — the paper may be scanned images rather than text")
        elif not stats.get("references_parsed", 0):
            why = ("the bibliography could not be read, so no marker had "
                   "anything to match against")
        else:
            why = ("none of the in-text markers matched an entry in the "
                   "bibliography, so no reference could be traced to a source")
        return {
            "level": "review",
            "headlines": [f"Nothing was checked: {why}.",
                          "This is not a clean result — the paper was not screened."],
        }

    headlines: list[str] = []
    level = "clear"

    for key, rule_level, singular, plural in _RISK_RULES:
        count = stats.get(key, 0)
        if not count:
            continue
        level = rule_level
        headlines.append((singular if count == 1 else plural).format(n=count))

    misrepresented = verdicts.get("unrelated", 0) + verdicts.get("weak", 0)
    if misrepresented:
        level = "critical" if level == "critical" else "concern"
        headlines.append(
            f"{misrepresented} {noun}{'s' if misrepresented != 1 else ''} "
            "may not say what they are cited for"
        )

    high_flags = stats.get("flagged", 0)
    if high_flags and level == "clear":
        level = "concern"
    if high_flags:
        headlines.append(
            f"{high_flags} reference carries a high-severity flag" if high_flags == 1
            else f"{high_flags} references carry a high-severity flag"
        )

    unverified = verdicts.get("unverified", 0)
    if unverified > checked * 0.4 and level == "clear":
        level = "review"
        headlines.append(
            f"{unverified} of {checked} {noun}s could not be verified — mostly paywalls"
        )

    if not headlines:
        headlines.append(
            f"No integrity problems were found in the {checked} {noun}s checked"
        )

    # Said last, and said whatever the outcome. The banner is the one line a
    # reader acts on, and a report that reads "clear" because someone marked
    # three references clear by hand is a different document from one that
    # reads clear on its own findings. The reader of an exported PDF was not
    # in the room when that call was made and has no other way to know.
    reviewed = stats.get("reviewed", 0)
    if reviewed:
        headlines.append(
            f"{reviewed} verdict{'s were' if reviewed != 1 else ' was'} set by hand "
            "after review, not by the tool"
        )

    return {"level": level, "headlines": headlines}


def _format_eta(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} seconds"
    minutes = seconds / 60
    if minutes < 60:
        return f"{round(minutes)} minute{'s' if round(minutes) != 1 else ''}"
    return f"{minutes / 60:.1f} hours"


def _check_budget(count: int, workers: int) -> float:
    """Wall-clock ceiling for the whole checking phase.

    A publisher that accepts a connection and then sends nothing can wedge a
    worker indefinitely: `requests` and `page.goto` take timeouts, but
    `page.evaluate` takes none, and a thread blocked in either cannot be
    interrupted from outside. Without a ceiling one such reference means the
    run never finishes and the reader never sees the twenty that did.

    Three times the up-front estimate, floor of three minutes. Loose enough
    that a slow bibliography finishes normally, tight enough that a wedged
    reference costs minutes rather than the whole report.
    """
    return max(180.0, 3 * count * _SERIAL_SECONDS_PER_REF / max(1, workers))


def _timed_out_entry(
    key: str,
    reference: refs.Reference,
    citations: list[intext.Citation],
    duplicate_of: str,
    budget: float,
    stage: tuple[str, str],
) -> dict:
    """Stand-in for a reference whose check never came back.

    Deliberately "unverified" rather than a new verdict: nothing was learned
    about this reference either way, which is exactly what unverified means.
    Saying "not found" would accuse the author of citing something that does
    not exist, on the evidence of a publisher being slow.
    """
    doing, detail = stage
    what = f"It was still {doing}" if doing else "It had not finished"
    where = f" at {detail}" if detail else ""
    note = (
        f"Not checked: this reference was still running when the whole run hit "
        f"its time limit of {_format_eta(budget)}. {what}{where}. That host "
        "accepted the connection and then stopped responding, which stalls the "
        "check indefinitely — it says nothing about whether the citation is "
        "sound. Opening the link yourself will usually work, and re-running "
        "often clears it."
    )
    return {
        "key": key,
        "reference": reference.to_dict(),
        "citations": [c.to_dict() for c in citations],
        "citation_count": len(citations),
        "verdict": "unverified",
        "score": 0.0,
        "reason": note,
        "engine": "",
        "source": {},
        "fetched": {},
        "shots": {},
        "notes": [note],
        # `summarise` raises the run-level warning from this, so re-checking the
        # reference on its own clears the complaint along with the problem.
        "timed_out": True,
        "flags": [
            f.to_dict()
            for f in crosscheck.check(key, reference, citations, duplicate_of)
        ],
    }


def _shutdown_browsers(pool: ThreadPoolExecutor, workers: int) -> None:
    """Close every worker's browser from the thread that owns it."""
    gate = threading.Barrier(workers, timeout=30)

    def close_mine() -> None:
        try:
            gate.wait()          # hold every worker here simultaneously
        except threading.BrokenBarrierError:
            pass                 # a worker died; still close what we own
        shots.close_thread_browser()

    futures = [pool.submit(close_mine) for _ in range(workers)]
    for future in futures:
        try:
            future.result(timeout=45)
        except Exception:
            pass


def _check_one(
    key: str,
    reference: refs.Reference,
    citations: list[intext.Citation],
    shots_dir: Path,
    options: Options,
    duplicate_of: str = "",
    paper_path: str = "",
    on_stage: Callable[[str, str], None] = lambda stage, detail: None,
) -> dict:
    """Resolve, fetch, judge and screenshot a single reference.

    `on_stage(stage, detail)` reports what this reference is currently doing.
    A worker that wedges can never report its own failure, so the run records
    each step as it starts — that record is the only evidence left of where a
    stalled reference actually got to.
    """
    entry: dict = {
        "key": key,
        "reference": reference.to_dict(),
        "citations": [c.to_dict() for c in citations],
        "citation_count": len(citations),
        "verdict": "unverified",
        "score": 0.0,
        "reason": "",
        "engine": "",
        "source": {},
        "fetched": {},
        "shots": {},
        "notes": [],
        # Structural problems found without touching the network.
        "flags": [
            f.to_dict()
            for f in crosscheck.check(key, reference, citations, duplicate_of)
        ],
    }

    # Each place the reference is cited is its own claim; scoring them
    # separately keeps a heavily-cited reference from looking weak.
    claims = _claims_for(citations)
    entry["claim"] = " ".join(dict.fromkeys(c.sentence for c in citations))[:2000]

    def snap(content=None, source=None, passages: list[str] | None = None) -> None:
        """Capture screenshots for whatever we managed to retrieve."""
        if not options.take_screenshots:
            return

        # Where the citation sits in the user's own paper — always available,
        # because that PDF is local and never gated.
        if paper_path:
            citing_name, citing_page = shots.capture_citing(
                pdf_path=paper_path,
                out_dir=shots_dir,
                stem=_safe_stem(key),
                sentences=[c.sentence for c in citations],
                page_hint=citations[0].page if citations else None,
            )
            if citing_name:
                entry["citing_shot"] = citing_name
                entry["citing_page"] = citing_page

        target = ""
        if content is not None:
            target = content.final_url or ""
        if not target and source is not None:
            target = source.oa_url or source.url or ""
        if not target:
            return
        on_stage("screenshotting the publisher page", target)
        try:
            captured = shots.capture(
                url=target,
                out_dir=shots_dir,
                stem=_safe_stem(key),
                claim=entry["claim"],
                passage=passages or [],
                pdf_bytes=getattr(content, "pdf_bytes", None),
            )
            entry["shots"] = captured.to_dict()
            entry["notes"].extend(captured.notes)
        except Exception as exc:
            entry["notes"].append(f"Screenshot step failed: {type(exc).__name__}")
            entry["shots"] = {"notes": [traceback.format_exc(limit=1)]}

        # The publisher page was gated, so there was nothing on it to highlight.
        # Fall back to the abstract we did legitimately retrieve, rendered as a
        # labelled record rather than passed off as a page capture.
        if not entry["shots"].get("evidence") and source is not None and source.abstract:
            try:
                name = shots.render_abstract_card(
                    out_dir=shots_dir,
                    stem=_safe_stem(key),
                    title=source.title or entry["reference"].get("title", ""),
                    abstract=source.abstract,
                    quote=(passages or [""])[0],
                    source_label=(source.resolver or "index").replace("-", " "),
                    url=source.url or "",
                )
                entry["shots"]["evidence"] = name
                entry["shots"]["evidence_is_card"] = True
                entry["notes"].append(
                    "Publisher page was not capturable; evidence shown as the "
                    "indexed abstract record."
                )
            except Exception as exc:
                entry["notes"].append(f"Abstract card failed: {type(exc).__name__}")

    on_stage("looking the reference up in the citation indexes", "")
    try:
        source = resolve.resolve(reference)
    except Exception as exc:
        entry["notes"].append(f"Could not resolve a link: {type(exc).__name__}")
        entry["reason"] = "This reference could not be resolved to a retrievable source."
        return entry

    entry["source"] = source.to_dict()
    entry["notes"].extend(source.notes)
    entry["flags"].extend(f.to_dict() for f in crosscheck.source_flags(source))

    # A reference no index has heard of is a finding in itself, and a far more
    # serious one than "we couldn't read the source". Say so plainly instead of
    # letting it fall through to the generic unverified bucket.
    if source.existence == "not_found":
        entry["verdict"] = "not_found"
        entry["reason"] = next(
            (n for n in source.notes if "not registered" in n or "No record of this" in n),
            "No bibliographic index has any record of this reference.",
        )
        snap(source=source)
        return entry

    if not source.url and not source.abstract:
        entry["reason"] = (
            "No link or indexed record was found for this reference, so its content "
            "could not be checked."
        )
        return entry

    on_stage("downloading the cited source", source.oa_url or source.url or "")
    try:
        content = fetch.fetch_source(source)
    except Exception as exc:
        entry["notes"].append(f"Fetch failed: {type(exc).__name__}")
        entry["reason"] = "The cited source could not be downloaded."
        snap(source=source)
        return entry

    entry["fetched"] = content.to_dict()
    entry["notes"].extend(content.notes)

    body = content.text or source.abstract
    if not (body or "").strip():
        entry["reason"] = (
            "Nothing readable was retrieved from the cited source, so the claim "
            "could not be checked against it."
        )
        # Still worth a header shot — it shows where the link actually landed.
        snap(content=content, source=source)
        return entry

    on_stage("judging the claims against the retrieved text", "")
    verdict = match.judge(
        claims=claims,
        source_text=body,
        title=content.title or source.title,
        abstract=source.abstract,
        reference_line=reference.raw,
        use_model=options.use_model,
        max_claims=options.max_claims_per_reference,
    )
    _apply_verdict(entry, verdict)

    snap(content=content, source=source, passages=verdict.screenshot_passages())
    return entry


def _claims_for(citations: list[intext.Citation]) -> list[match.Claim]:
    """The distinct things a reference is cited for, best evidence first.

    Two narrowings happen here, and both exist because the alternative produces
    verdicts about text nobody wrote as a claim:

    * **Prose wins.** A reference cited from a comparison table *and* from three
      sentences is asserting three things and nothing in the table. Judging a
      row of column values returns "weak" about a row of column values.
    * **The clause, not the sentence.** `intext` has already cut each marker
      down to the span it governs; the full sentence rides along as context so
      the model can see what the clause depends on without being asked to hold
      this source responsible for the rest of it.
    """
    prose = [c for c in citations if c.prose]
    claims: list[match.Claim] = []
    seen: set[str] = set()
    for cite in (prose or citations):
        text = (cite.claim or cite.sentence).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        claims.append(
            match.Claim(
                text=text,
                context=cite.sentence.strip(),
                co_cited=cite.group_size,
                page=cite.page,
            )
        )
    return claims


def _apply_verdict(entry: dict, verdict: match.MatchResult) -> None:
    """Write one judgement onto an entry, headline and per-claim detail alike."""
    entry["verdict"] = verdict.verdict
    entry["score"] = round(verdict.score, 3)
    entry["reason"] = verdict.reason
    entry["engine"] = verdict.engine
    entry["match"] = verdict.to_dict()
    entry["claim_verdicts"] = [c.to_dict() for c in verdict.claim_verdicts]
    entry["claim_tally"] = verdict.claim_tally()


# --------------------------------------------------------------------------- #
# Setting a verdict by hand
# --------------------------------------------------------------------------- #

def set_verdict(
    run_dir: Path,
    key: str,
    verdict: str = "",
    note: str = "",
    clear: bool = False,
    claim_index: int | None = None,
) -> dict:
    """Record the reader's own verdict, or drop it again.

    The tool screens; a person decides. Once someone has opened the source and
    read it, their judgement is better evidence than anything here — and until
    they can record it, the report stays wrong in a way they cannot fix and
    cannot hand on.

    `claim_index` picks *which* verdict. A reference cited five times carries
    five separate judgements, and a reader who has just read the source usually
    disagrees with one of them, not with all five — so each citation can be set
    on its own, and the reference's headline is then re-derived from the claims
    beneath it. Omit it to set the reference's headline directly, which
    overrules the claims rather than summarising them.

    What the tool said is never overwritten, only displaced: `machine` keeps the
    original headline and every claim keeps its own, so clearing restores them
    exactly and a reader who marks the same thing twice never turns their own
    first answer into "what the tool found".
    """
    run_dir = Path(run_dir)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))

    entry = next(
        (e for e in report.get("references") or [] if e.get("key") == key), None
    )
    if entry is None:
        raise KeyError(f"No reference {key!r} in this report.")

    if not clear and verdict not in match.VERDICTS:
        raise ValueError(
            f"{verdict!r} is not a verdict. Use one of: " + ", ".join(match.VERDICTS)
        )

    _baseline(entry)

    if claim_index is None:
        if clear:
            entry.pop("override", None)
        else:
            entry["override"] = _stamp(verdict, note)
    else:
        claims = entry.get("claim_verdicts") or []
        if not 0 <= claim_index < len(claims):
            raise KeyError(
                f"Reference {key} has no citation {claim_index + 1}; it has {len(claims)}."
            )
        claim = claims[claim_index]
        claim.setdefault("machine_verdict", claim.get("verdict", ""))
        claim.setdefault("machine_reason", claim.get("reason", ""))
        if clear:
            claim.pop("override", None)
            claim["verdict"] = claim["machine_verdict"]
            claim["reason"] = claim["machine_reason"]
        else:
            claim["override"] = _stamp(verdict, note)
            claim["verdict"] = verdict
            claim["reason"] = _review_reason(verdict, claim["machine_verdict"], note)

    _settle_headline(entry)
    summarise(report)
    save(run_dir, report)
    return report


def _stamp(verdict: str, note: str) -> dict:
    return {
        "verdict": verdict,
        "note": (note or "").strip()[:2000],
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def _baseline(entry: dict) -> dict:
    """What the tool itself concluded, before anyone overruled it.

    Captured lazily, the first time a reference is touched, and never written
    again — so displacing a verdict twice cannot promote the reader's own first
    answer into the record of what the tool found.
    """
    if "machine" not in entry:
        # Reports written before per-claim review nested this inside `reviewed`.
        legacy = entry.get("reviewed") or {}
        entry["machine"] = {
            "verdict": legacy.get("machine_verdict", entry.get("verdict", "")),
            "reason": legacy.get("machine_reason", entry.get("reason", "")),
        }
    return entry["machine"]


def _settle_headline(entry: dict) -> None:
    """Re-derive a reference's headline verdict from whatever now stands.

    Three cases, in order of precedence:

    * a headline the reader set directly, which overrules everything below it;
    * otherwise, if they have judged any individual citation, a roll-up of the
      claim verdicts — because a card that disagrees with the claims listed
      inside it is worse than no headline at all. It rolls up the way the engine
      that produced those claims would have, which is not the same in both
      directions: see `match.roll_up`;
    * otherwise the tool's own verdict, restored exactly.
    """
    machine = _baseline(entry)
    claims = entry.get("claim_verdicts") or []
    entry["claim_tally"] = _tally(claims)

    override = entry.get("override")
    if override:
        entry["verdict"] = override["verdict"]
        entry["reason"] = _review_reason(
            override["verdict"], machine["verdict"], override.get("note", "")
        )
        entry["reviewed"] = {
            **override,
            "source": "reference",
            "machine_verdict": machine["verdict"],
            "machine_reason": machine["reason"],
        }
        return

    edited = [c for c in claims if c.get("override")]
    if edited:
        rolled = match.roll_up([c["verdict"] for c in claims], entry.get("engine", ""))
        entry["verdict"] = rolled
        entry["reason"] = (
            f"You judged {len(edited)} of {len(claims)} citing "
            f"{'places' if len(claims) != 1 else 'place'} yourself; rolled up across "
            f"all {len(claims)}, this reference now reads “{rolled}”. The tool had it "
            f"as “{machine['verdict']}”."
        )
        entry["reviewed"] = {
            "verdict": rolled,
            "note": "",
            "at": max(c["override"]["at"] for c in edited),
            "source": "claims",
            "edited_claims": len(edited),
            "machine_verdict": machine["verdict"],
            "machine_reason": machine["reason"],
        }
        return

    entry["verdict"] = machine["verdict"]
    entry["reason"] = machine["reason"]
    entry.pop("reviewed", None)
    # Nothing is displacing the tool's verdict any more, so the saved baseline
    # is no longer holding anything up. Dropping it keeps undo lossless: the
    # entry goes back to exactly the shape it had before anyone touched it.
    entry.pop("machine", None)


def _tally(claims: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim["verdict"]] = counts.get(claim["verdict"], 0) + 1
    return counts


def _review_reason(verdict: str, machine_verdict: str, note: str = "") -> str:
    """The reason line for something someone judged themselves.

    Says who decided before it says what was decided. This string is what the
    card and the exported PDF show, and a reader must never be able to read a
    hand-set verdict as something the tool established.
    """
    base = "Set by hand after review"
    if machine_verdict and machine_verdict != verdict:
        base += f", replacing the tool's verdict of “{machine_verdict}”"
    note = (note or "").strip()
    return f"{base}. {note}" if note else f"{base}."


# --------------------------------------------------------------------------- #
# Re-checking one reference
# --------------------------------------------------------------------------- #

@dataclass
class SuppliedSource:
    """A document the reader handed over for one specific reference."""

    name: str
    text: str
    kind: str = "file"       # "pdf" | "text"


def read_supplied(filename: str, data: bytes) -> SuppliedSource:
    """Pull judgeable text out of an uploaded .pdf or .txt.

    Raises ValueError with something a reader can act on, because this is the
    one place in the pipeline where the input came from a person rather than
    from the network, and "nothing happened" is not an acceptable answer.
    """
    name = Path(filename).name or "supplied"
    lowered = name.lower()

    if lowered.endswith(".pdf") or data[:5] == b"%PDF-":
        title, text = fetch._pdf_text(data)
        if not text.strip():
            raise ValueError(
                "No text could be read out of that PDF. Scanned page images "
                "carry no text layer — a text file of the relevant pages works."
            )
        return SuppliedSource(name=title.strip() or name, text=text, kind="pdf")

    if not lowered.endswith((".txt", ".text", ".md")):
        raise ValueError("Please supply a PDF or a plain text (.txt) file.")

    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = data.decode("utf-8", errors="replace")

    if len(text.strip()) < 40:
        raise ValueError("That file holds too little text to judge a citation against.")
    return SuppliedSource(name=name, text=text.strip(), kind="text")


def _rebuild(cls, data: dict):
    """Rebuild a dataclass from a stored dict, ignoring fields it no longer has."""
    fields = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in fields})


def recheck_one(
    run_dir: Path,
    key: str,
    options: Options,
    paper_path: str = "",
    supplied: SuppliedSource | None = None,
    claim_index: int | None = None,
) -> dict:
    """Re-run a single reference and fold the result back into its report.

    Two reasons a reader needs this. A reference can fail for reasons that have
    nothing to do with the citation — a publisher that was down, a host that
    stalled the whole run out of its time budget — and re-running just that one
    costs seconds instead of re-screening the paper. And where the tool resolved
    to the wrong document, or to a paywall it could not read, the reader often
    has the actual paper on disk: `supplied` judges the citation against that
    instead, which is better evidence than any lookup.

    `claim_index` narrows all of that to one citation. A reference cited five
    times is making five claims, and a reader who doubts one of them has no
    business spending five model calls to answer it — nor should the four
    verdicts they were content with be thrown away, along with any they had set
    by hand, to re-answer the fifth. Omit it to re-check the whole reference.

    The re-checked entry replaces the old one in `report.json` and the run-level
    tally, banner and warnings are all recomputed from it.
    """
    run_dir = Path(run_dir)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))

    index = next(
        (i for i, e in enumerate(report.get("references") or []) if e.get("key") == key),
        None,
    )
    if index is None:
        raise KeyError(f"No reference {key!r} in this report.")

    previous = report["references"][index]
    reference = _rebuild(refs.Reference, previous.get("reference"))
    citations = [_rebuild(intext.Citation, c) for c in previous.get("citations") or []]
    duplicate_of = _duplicate_of(previous)
    shots_dir = run_dir / "shots"

    if claim_index is not None:
        report["references"][index] = _recheck_claim(
            previous=previous,
            reference=reference,
            citations=citations,
            claim_index=claim_index,
            options=options,
            supplied=supplied,
        )
        summarise(report)
        save(run_dir, report)
        return report

    try:
        if supplied is not None:
            entry = _check_supplied(
                previous=previous,
                reference=reference,
                citations=citations,
                supplied=supplied,
                shots_dir=shots_dir,
                options=options,
                paper_path=paper_path,
            )
        else:
            entry = _check_one(
                key=key,
                reference=reference,
                citations=citations,
                shots_dir=shots_dir,
                options=options,
                duplicate_of=duplicate_of,
                paper_path=paper_path,
            )
    finally:
        # This runs on a request thread, not on a pooled worker, so nothing else
        # will ever come back to close the browser it just started.
        if options.take_screenshots:
            shots.close_thread_browser()

    stamp = datetime.now().isoformat(timespec="seconds")
    against = "supplied" if supplied is not None else "sources"
    filename = supplied.name if supplied is not None else ""

    # A re-check that came back with nothing readable did not overturn the
    # earlier finding — it failed to retrieve anything to judge. Letting it
    # through is exactly how a reference reads "unrelated" after one re-check
    # and "unverified" after the next: the publisher served the full text one
    # minute and a paywall stub the next, and the verdict followed the
    # retrieval rather than the citation. Nothing about the citation changed.
    #
    # The earlier entry is kept whole rather than having its verdict patched
    # back in, because its verdict, evidence text and screenshots all describe
    # one retrieval; restoring the headline alone leaves a card whose evidence
    # contradicts it. The failure is reported as a failure instead.
    if _retrieved_nothing(entry) and previous.get("engine"):
        detail = entry.get("reason") or "Nothing readable was retrieved this time."
        entry = dict(previous)
        entry["rechecked"] = {
            "at": stamp,
            "against": against,
            "filename": filename,
            "previous_verdict": previous.get("verdict", ""),
            # Nothing was displaced, so a hand-set verdict here is still a
            # judgement about the evidence still on the card. Clearing it would
            # throw away the reader's own conclusion to record a failed lookup.
            "cleared_review": "",
            "outcome": "nothing_retrieved",
            "detail": detail,
        }
        report["references"][index] = entry
        summarise(report)
        save(run_dir, report)
        return report

    # A hand-set verdict was a judgement about evidence that has just been
    # replaced. Carrying it forward would attach the reader's name to a reading
    # of something they never saw, so it is dropped — and said out loud, because
    # silently discarding someone's own conclusion is worse than losing it.
    displaced = (previous.get("reviewed") or {}).get("verdict", "")
    for stale in ("reviewed", "override", "machine"):
        entry.pop(stale, None)

    entry["rechecked"] = {
        "at": stamp,
        "against": against,
        "filename": filename,
        "previous_verdict": previous.get("verdict", ""),
        "cleared_review": displaced,
        "outcome": "judged",
    }
    # A verdict that has been re-run is no longer a casualty of the run's clock.
    entry.pop("timed_out", None)

    report["references"][index] = entry
    summarise(report)
    save(run_dir, report)
    return report


def _retrieved_nothing(entry: dict) -> bool:
    """Whether a check came back with no judgeable text at all.

    `not_found` is excluded deliberately: an index reporting that it has no
    record of a reference is a real finding *about the reference*, not a failure
    to retrieve one, and it must be allowed to replace an earlier verdict.
    """
    return not entry.get("engine") and entry.get("verdict") != "not_found"


# --------------------------------------------------------------------------- #
# Re-checking one citation of one reference
# --------------------------------------------------------------------------- #

def _recheck_claim(
    previous: dict,
    reference: refs.Reference,
    citations: list[intext.Citation],
    claim_index: int,
    options: Options,
    supplied: SuppliedSource | None = None,
) -> dict:
    """Re-judge one citation, leaving the others on the card exactly as they are.

    The narrow scope is the whole point, so it is enforced rather than assumed:
    one claim goes to the judge, one claim verdict comes back, and it is spliced
    into position. Its siblings are not re-read, not re-scored and not re-fetched
    for — including any the reader has judged themselves, which a whole-reference
    re-check would have cleared.

    Screenshots are skipped outright. The card's evidence capture describes the
    reference as a whole, and overwriting it on the strength of one re-judged
    citation would leave the other four pointing at a picture of something else.
    The re-judged claim carries its own quoted evidence in its own row.
    """
    stored = list(previous.get("claim_verdicts") or [])
    if not 0 <= claim_index < len(stored):
        raise KeyError(
            f"Reference {previous.get('key')} has no citation {claim_index + 1}; "
            f"it has {len(stored)}."
        )

    entry = dict(previous)
    entry["notes"] = list(previous.get("notes") or [])
    was = stored[claim_index]
    claim = _claim_at(citations, stored, claim_index)
    stamp = datetime.now().isoformat(timespec="seconds")
    against = "supplied" if supplied is not None else "sources"
    filename = supplied.name if supplied is not None else ""

    if supplied is not None:
        body, title, abstract, failure = supplied.text, supplied.name, "", ""
    else:
        body, title, abstract, failure = _retrieve_text(reference)

    # Nothing came back, so nothing was learned. The citation keeps the verdict
    # it had — see the whole-reference case for why a failed lookup must never
    # be allowed to present itself as a new judgement.
    if failure:
        entry["claim_verdicts"] = stored
        entry["rechecked"] = {
            "at": stamp,
            "against": against,
            "filename": filename,
            "previous_verdict": previous.get("verdict", ""),
            "cleared_review": "",
            "outcome": "nothing_retrieved",
            "detail": failure,
            "scope": "claim",
            "claim_index": claim_index,
        }
        return entry

    result = match.judge(
        claims=[claim],
        source_text=body,
        title=title or reference.title,
        abstract=abstract,
        reference_line=reference.raw,
        use_model=options.use_model,
        max_claims=1,
    )
    fresh = (
        result.claim_verdicts[0].to_dict()
        if result.claim_verdicts
        else {
            "claim": claim.text,
            "verdict": result.verdict,
            "score": round(result.score, 3),
            "reason": result.reason,
            "evidence_quote": "",
            "page": claim.page,
            "context": claim.context if claim.context != claim.text else "",
            "reconsidered": False,
        }
    )
    # A verdict the reader set on *this* citation judged evidence that has just
    # been replaced, so it goes — the same rule the whole-reference re-check
    # applies, narrowed to the one citation whose evidence actually moved.
    displaced = (was.get("override") or {}).get("verdict", "")
    fresh["rechecked"] = {
        "at": stamp,
        "against": against,
        "filename": filename,
        "previous_verdict": was.get("verdict", ""),
        "cleared_review": displaced,
        "source_title": title,
    }

    claims = list(stored)
    claims[claim_index] = fresh
    entry["claim_verdicts"] = claims
    entry["claim_tally"] = _tally(claims)
    entry["engine"] = _stronger_engine(previous.get("engine", ""), result.engine)

    # The tool's own headline has genuinely moved — one of the citations under
    # it was re-judged — so the baseline `_settle_headline` restores to is
    # rewritten here rather than left at what the tool concluded last time.
    # Rolled up from what the *tool* said about each citation, so a verdict the
    # reader set on a sibling does not quietly become part of the tool's record.
    rolled = match.roll_up(_machine_verdicts(claims), entry["engine"])
    entry["machine"] = {
        "verdict": rolled,
        "reason": _claim_rollup_reason(claims, claim_index, rolled),
    }
    _settle_headline(entry)

    entry["rechecked"] = {
        "at": stamp,
        "against": against,
        "filename": filename,
        "previous_verdict": previous.get("verdict", ""),
        "cleared_review": displaced,
        "outcome": "judged",
        "scope": "claim",
        "claim_index": claim_index,
    }
    entry.pop("timed_out", None)
    return entry


def _claim_at(
    citations: list[intext.Citation], stored: list[dict], claim_index: int
) -> match.Claim:
    """The claim behind a stored claim verdict, rebuilt from the citations.

    Rebuilt rather than reconstructed from the stored verdict because a stored
    verdict does not carry `co_cited`, and a group citation judged as though it
    were the only source cited at that point is exactly the misreading
    `_claims_for` exists to prevent — it would be asked to support the whole
    sentence and marked down for the parts belonging to its neighbours.

    Matched on text first so that a report whose claim list has since been
    ordered differently still re-checks the citation the reader clicked on,
    rather than whichever one now sits at that index.
    """
    rebuilt = _claims_for(citations)
    wanted = (stored[claim_index].get("claim") or "").strip()
    for claim in rebuilt:
        if claim.text.strip() == wanted:
            return claim
    if claim_index < len(rebuilt):
        return rebuilt[claim_index]
    # The citations are gone from the report but the claim text survives, so
    # judge that on its own rather than refusing.
    return match.Claim(
        text=wanted, context=stored[claim_index].get("context", "") or wanted
    )


def _stronger_engine(previous: str, fresh: str) -> str:
    """Which engine a card is labelled with after only part of it was re-judged.

    The label is not cosmetic: `match.roll_up` reads it, and the two tiers roll
    up in opposite directions — the model tier takes the most concerning claim,
    the lexical tier the least. So letting one lexically re-judged citation
    relabel the whole card as lexical would switch its untouched siblings from
    worst-case to best-case, cancelling two "unrelated" verdicts into a headline
    of "unverified" on the strength of nothing anyone re-examined.

    A model verdict is evidence and a lexical score is not, so the stronger of
    the two labels wins and a scoped re-check can never quietly loosen the rule
    the rest of the card is judged by.
    """
    if previous and previous != "lexical" and (not fresh or fresh == "lexical"):
        return previous
    return fresh or previous


def _machine_verdicts(claims: list[dict]) -> list[str]:
    """What the tool itself concluded for each citation, ignoring overrides."""
    return [c.get("machine_verdict") or c.get("verdict", "") for c in claims]


def _claim_rollup_reason(claims: list[dict], claim_index: int, rolled: str) -> str:
    """Why the card reads what it reads after one citation was re-checked.

    Says which citation moved before it says where the card landed, because the
    two often disagree: re-checking citation three of five to "supported" leaves
    a card that still reads "weak" on the strength of citation one, and a reader
    who is not told that reads it as the re-check having failed.
    """
    fresh = claims[claim_index]
    if len(claims) <= 1:
        return fresh.get("reason", "")
    tally = _tally(claims)
    summary = ", ".join(
        f"{n} {verdict}"
        for verdict, n in sorted(tally.items(), key=lambda kv: -match.concern(kv[0]))
    )
    return (
        f"Citation {claim_index + 1} of {len(claims)} was re-checked on its own and "
        f"now reads “{fresh.get('verdict', '')}”. Rolled up across all "
        f"{len(claims)} ({summary}), this reference reads “{rolled}”. "
        f"{fresh.get('reason', '')}"
    ).strip()


def _retrieve_text(reference: refs.Reference) -> tuple[str, str, str, str]:
    """Resolve and fetch one reference, for judging a single citation against.

    Returns `(body, title, abstract, failure)`. `failure` is a reader-facing
    sentence and is empty only when there is text worth judging.

    Deliberately does not touch flags, retraction findings or the card's stored
    source record. Those describe the reference, and this is being called to
    answer a question about one citation of it — a single-citation re-check that
    quietly rewrote the card's retraction status would be doing something nobody
    asked it to.
    """
    try:
        source = resolve.resolve(reference)
    except Exception as exc:
        return "", "", "", f"The reference could not be looked up ({type(exc).__name__})."

    if source.existence == "not_found":
        return "", "", "", (
            "No bibliographic index has any record of this reference — a finding "
            "about the whole reference rather than this one citation. Re-check "
            "the reference itself to record it."
        )
    if not source.url and not source.abstract:
        return "", "", "", "No link or indexed record was found for this reference."

    try:
        content = fetch.fetch_source(source)
    except Exception as exc:
        return "", "", "", f"The cited source could not be downloaded ({type(exc).__name__})."

    body = content.text or source.abstract
    if not (body or "").strip():
        return "", "", "", "Nothing readable was retrieved from the cited source."
    return body, content.title or source.title, source.abstract, ""


def _duplicate_of(entry: dict) -> str:
    """Recover the duplicate-entry finding from a stored report."""
    for flag in entry.get("flags") or []:
        if flag.get("kind") == "duplicate-entry":
            match_ = re.search(r"\[([^\]]+)\]", flag.get("message", ""))
            if match_:
                return match_.group(1)
    return ""


def _check_supplied(
    previous: dict,
    reference: refs.Reference,
    citations: list[intext.Citation],
    supplied: SuppliedSource,
    shots_dir: Path,
    options: Options,
    paper_path: str = "",
) -> dict:
    """Judge one reference against a document the reader supplied.

    Resolution and fetching are skipped outright. The reader has said which
    document this reference is, which is stronger evidence than a bibliographic
    search, and going back to the network could only substitute some other paper
    for the one they handed over.

    What the indices said — that the work exists, that it has or has not been
    retracted — is carried over untouched, because none of that was re-tested
    here and quietly dropping it would turn a real retraction finding into a
    blank. Only the content verdict is replaced.
    """
    entry = dict(previous)
    entry["notes"] = [
        f"Judged against a document supplied by the reader: {supplied.name}. "
        "The bibliographic lookup above is from the original run and was not repeated."
    ]
    entry["shots"] = {}
    entry.pop("citing_shot", None)
    entry.pop("citing_page", None)
    entry["fetched"] = {
        "url": "",
        "final_url": "",
        "kind": f"supplied {supplied.kind}",
        "title": supplied.name,
        "status": 0,
        "ok": True,
        "paywalled": False,
        "has_abstract": False,
        "notes": [],
        "text": supplied.text[:1500],
        "text_chars": len(supplied.text),
    }

    claims = _claims_for(citations)
    entry["claim"] = " ".join(dict.fromkeys(c.sentence for c in citations))[:2000]

    verdict = match.judge(
        claims=claims,
        source_text=supplied.text,
        title=supplied.name or reference.title,
        # Deliberately empty. The abstract on file belongs to whatever the run
        # originally resolved to, and mixing it into the judgement of a document
        # the reader supplied would score the citation against both at once.
        abstract="",
        reference_line=reference.raw,
        use_model=options.use_model,
        max_claims=options.max_claims_per_reference,
    )
    _apply_verdict(entry, verdict)

    if not options.take_screenshots:
        return entry

    if paper_path:
        citing_name, citing_page = shots.capture_citing(
            pdf_path=paper_path,
            out_dir=shots_dir,
            stem=_safe_stem(entry["key"]),
            sentences=[c.sentence for c in citations],
            page_hint=citations[0].page if citations else None,
        )
        if citing_name:
            entry["citing_shot"] = citing_name
            entry["citing_page"] = citing_page

    quotes = verdict.screenshot_passages()
    excerpt = _excerpt_around(supplied.text, quotes[0] if quotes else "")
    try:
        entry["shots"] = {
            "evidence": shots.render_abstract_card(
                out_dir=shots_dir,
                stem=_safe_stem(entry["key"]),
                title=supplied.name,
                abstract=excerpt,
                quote=quotes[0] if quotes else "",
                source_label="file supplied by the reader",
                banner="SUPPLIED DOCUMENT - text you provided for this reference",
                footnote=(
                    "This reference was re-checked against a document you uploaded, "
                    "not against anything retrieved from a publisher or an index."
                ),
            ),
            "evidence_is_card": True,
            "matched_text": quotes[0] if quotes else "",
            "notes": [],
        }
    except Exception as exc:
        entry["notes"].append(f"Evidence card failed: {type(exc).__name__}")
    return entry


def _excerpt_around(text: str, quote: str, width: int = 2400) -> str:
    """The part of a long document worth rendering onto the evidence card."""
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if len(text) <= width:
        return text
    at = text.find((quote or "")[:120]) if quote else -1
    if at < 0:
        return text[:width] + " …"
    lo = max(0, at - width // 3)
    prefix = "… " if lo else ""
    return prefix + text[lo : lo + width].strip() + " …"


def _safe_stem(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:60] or "ref"


def _sort_key(key: str):
    return (0, int(key)) if key.isdigit() else (1, key)


def _short(text: str, width: int = 70) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= width else text[: width - 1] + "…"
