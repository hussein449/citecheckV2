"""Measure runtime and behaviour under simultaneous runs.

    python -m tools.benchmark paper.pdf [more.pdf ...] --levels 1,2,5,10

At each concurrency level k, k runs are started at once — cycling through the
PDFs given — each on its own thread, which is exactly how the server starts a
run per upload. Every run is timed, and the script records whether it failed
outright, how many of its references hit the time budget, which engine judged
it, and what evidence its verdicts rested on.

Results go to ``runs/benchmark-<timestamp>/results.json`` with one row per run,
and a summary table is printed. Nothing here is mocked: runs call the live
bibliographic services and, unless ``--lexical`` is given, the model API, so a
level-10 pass on a 150-reference paper costs what ten real uploads cost.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from citecheck import match, pipeline

ROOT = Path(__file__).resolve().parent.parent


def _one_run(pdf: Path, run_dir: Path, options: pipeline.Options) -> dict:
    started = time.time()
    row = {"pdf": pdf.name, "run_dir": str(run_dir.relative_to(ROOT))}
    try:
        report = pipeline.run(str(pdf), run_dir, options).to_dict()
    except Exception as exc:
        row.update(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=3),
            seconds=round(time.time() - started, 1),
        )
        return row

    stats = report["stats"]
    refs = report["references"]
    checked = stats.get("references_checked", 0)
    seconds = round(time.time() - started, 1)
    row.update(
        ok=True,
        seconds=seconds,
        pages=stats.get("pages"),
        citations_found=stats.get("citations_found"),
        references_checked=checked,
        claims_judged=stats.get("claims_judged"),
        seconds_per_reference=round(seconds / checked, 2) if checked else None,
        timed_out=sum(1 for e in refs if e.get("timed_out")),
        engine=stats.get("engine"),
        references_judged_by_model=stats.get("references_judged_by_model"),
        references_judgeable=stats.get("references_judgeable"),
        coverage=stats.get("coverage"),
    )
    return row


def run_level(
    level: int, pdfs: list[Path], out_dir: Path, options: pipeline.Options
) -> dict:
    """Start *level* runs together and wait for all of them."""
    rows: list[dict | None] = [None] * level
    threads = []
    gate = threading.Barrier(level)

    def go(slot: int) -> None:
        pdf = pdfs[slot % len(pdfs)]
        gate.wait()                      # release every run at the same instant
        rows[slot] = _one_run(pdf, out_dir / f"level-{level}" / f"run-{slot + 1}", options)

    started = time.time()
    for slot in range(level):
        thread = threading.Thread(target=go, args=(slot,), daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    wall = round(time.time() - started, 1)

    done = [r for r in rows if r]
    ok = [r for r in done if r.get("ok")]
    latencies = [r["seconds"] for r in ok]
    return {
        "level": level,
        "wall_seconds": wall,
        "runs": done,
        "failed_runs": len(done) - len(ok),
        "timed_out_references": sum(r.get("timed_out", 0) for r in ok),
        "latency_median": round(statistics.median(latencies), 1) if latencies else None,
        "latency_max": max(latencies) if latencies else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--levels", default="1", help="comma-separated, e.g. 1,2,5,10")
    parser.add_argument("--workers", type=int, default=4, help="reference workers per run")
    parser.add_argument("--max-references", type=int, default=250)
    parser.add_argument("--lexical", action="store_true", help="skip the model tier")
    parser.add_argument("--no-screenshots", action="store_true")
    parser.add_argument("--contact-email", default="")
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.pdfs if not p.is_file()]
    if missing:
        parser.error("not found: " + ", ".join(missing))
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    options = pipeline.Options(
        max_references=args.max_references,
        use_model=not args.lexical,
        take_screenshots=not args.no_screenshots,
        workers=args.workers,
        contact_email=args.contact_email,
    )
    out_dir = ROOT / "runs" / f"benchmark-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = match.active_engine() if options.use_model else "lexical"
    results = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
        },
        "settings": {
            "workers": options.workers,
            "max_references": options.max_references,
            "engine": engine,
            "model": match.model_name() if engine != "lexical" else "",
            "screenshots": options.take_screenshots,
            "pdfs": [p.name for p in args.pdfs],
        },
        "levels": [],
    }

    for level in levels:
        print(f"level {level}: starting {level} simultaneous run(s)…", flush=True)
        outcome = run_level(level, args.pdfs, out_dir, options)
        results["levels"].append(outcome)
        (out_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(
            f"level {level}: wall {outcome['wall_seconds']}s, median run "
            f"{outcome['latency_median']}s, slowest {outcome['latency_max']}s, "
            f"{outcome['failed_runs']} failed, "
            f"{outcome['timed_out_references']} references timed out",
            flush=True,
        )

    print(f"\nResults: {out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
