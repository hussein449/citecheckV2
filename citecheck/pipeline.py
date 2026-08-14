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
from pathlib import Path
from typing import Callable, Iterable

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
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "source_pdf": self.source_pdf,
            "paper_title": self.paper_title,
            "stats": self.stats,
            "references": self.references,
            "orphan_keys": self.orphan_keys,
            "warnings": self.warnings,
        }


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
    report.orphan_keys = sorted(orphans, key=_sort_key)

    if orphans:
        report.warnings.append(
            f"{len(orphans)} citation marker(s) had no matching bibliography entry: "
            + ", ".join(sorted(orphans, key=_sort_key)[:12])
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
            report.warnings.append(f"[{key}] {entry['notes'][0]}")
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

    verdicts: dict[str, int] = {}
    flagged = 0
    retracted = 0
    not_found = 0
    claims_judged = 0
    for entry in report.references:
        verdicts[entry["verdict"]] = verdicts.get(entry["verdict"], 0) + 1
        claims_judged += len(entry.get("claim_verdicts") or [])
        if (entry.get("source") or {}).get("retracted"):
            retracted += 1
        if entry["verdict"] == "not_found":
            not_found += 1
        for flag in entry.get("flags", []):
            if flag["severity"] == "high":
                flagged += 1
                report.warnings.append(f"[{entry['key']}] {flag['message']}")
                break

    report.stats["verdicts"] = verdicts
    report.stats["flagged"] = flagged
    report.stats["retracted"] = retracted
    report.stats["not_found"] = not_found
    report.stats["claims_judged"] = claims_judged
    report.stats.update(_engine_outcome(report))
    report.stats["risk"] = risk_summary(report.stats)
    report.stats["elapsed_seconds"] = round(time.time() - started, 1)

    (run_dir / "report.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    progress({"stage": "done", "message": "Finished.", "percent": 100, "report": report.to_dict()})
    return report


# How each engine is named in prose written for the reader.
_ENGINE_DISPLAY = {"openai": "OpenAI", "lexical": "lexical word overlap"}


def _engine_outcome(report: Report) -> dict:
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
    eligible = [entry for entry in report.references if entry.get("engine")]
    judged = [entry for entry in eligible if entry["engine"] != "lexical"]
    planned = report.stats.get("engine_planned", "lexical")
    named = _ENGINE_DISPLAY.get(planned, planned)

    stats = {
        "engine": judged[0]["engine"] if judged else "lexical",
        "references_judged_by_model": len(judged),
        "references_judgeable": len(eligible),
        "engine_note": "",
    }

    if planned == "lexical" or not eligible:
        return stats

    if not judged:
        stats["engine_note"] = (
            f"{named} judging was enabled but produced no verdicts — every result "
            "below is lexical word overlap, which can show that two texts discuss "
            "the same thing but never that a citation is wrong. The per-reference "
            "reasons say why each call failed."
        )
        report.warnings.append(stats["engine_note"])
    elif len(judged) < len(eligible):
        stats["engine_note"] = (
            f"{named} judged {len(judged)} of {len(eligible)} references with "
            "retrievable text; the rest fell back to lexical word overlap."
        )

    return stats


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
    verdicts = stats.get("verdicts", {})
    checked = max(1, stats.get("references_checked", 0))

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
            f"{misrepresented} citation{'s' if misrepresented != 1 else ''} "
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
            f"{unverified} of {checked} references could not be verified — mostly paywalls"
        )

    if not headlines:
        headlines.append("No integrity problems were found in the references checked")

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
    claim_sentences = list(dict.fromkeys(c.sentence for c in citations))
    entry["claim"] = " ".join(claim_sentences)[:2000]

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
        claims=claim_sentences,
        source_text=body,
        title=content.title or source.title,
        abstract=source.abstract,
        reference_line=reference.raw,
        use_model=options.use_model,
        max_claims=options.max_claims_per_reference,
    )

    # Tie each per-claim judgement back to the page it was made on, so a
    # disputed claim can be found in the PDF without hunting for it.
    page_of = {c.sentence: c.page for c in citations}
    for claim_verdict in verdict.claim_verdicts:
        claim_verdict.page = page_of.get(claim_verdict.claim)

    entry["verdict"] = verdict.verdict
    entry["score"] = round(verdict.score, 3)
    entry["reason"] = verdict.reason
    entry["engine"] = verdict.engine
    entry["match"] = verdict.to_dict()
    entry["claim_verdicts"] = [c.to_dict() for c in verdict.claim_verdicts]
    entry["claim_tally"] = verdict.claim_tally()

    snap(content=content, source=source, passages=verdict.screenshot_passages())
    return entry


def _safe_stem(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:60] or "ref"


def _sort_key(key: str):
    return (0, int(key)) if key.isdigit() else (1, key)


def _short(text: str, width: int = 70) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= width else text[: width - 1] + "…"
