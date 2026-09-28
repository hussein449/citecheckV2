"""Score CiteAudit against human labels.

Two steps:

    python -m tools.evaluate export runs/<run_id> [...] -o sheet.csv
    python -m tools.evaluate score sheet.csv [--second second_annotator.csv]

`export` writes one row per citation the tool found, with the tool's answers
filled in and the human columns (`h_*`) empty. Annotators fill those in from
the manuscript and the cited source, and add a row — `pred_key` left blank,
`h_is_citation` = y — for every citation marker the tool missed.

`score` computes only what the labels support, and says so when a metric has
no labelled rows instead of printing a number:

  * marker extraction    precision and recall (h_is_citation)
  * association          share of true markers linked to the right entry (h_ref_key)
  * clause scoping       share of multi-citation markers given the right clause (h_clause_ok)
  * existence            confusion against h_exists (y/n), false no-match rate
  * claim support        agreement and Cohen's kappa against h_support,
                         overall and by evidence category
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from citecheck import pipeline

COLUMNS = [
    "run_id", "ref_key", "pred_key", "label", "page", "group_size", "prose",
    "sentence", "clause", "pred_existence", "pred_support", "evidence",
    "h_is_citation", "h_ref_key", "h_clause_ok", "h_exists", "h_support", "notes",
]
SUPPORT = ("supported", "related", "weak", "unrelated", "unverified")


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def export_rows(run_dir: Path) -> list[dict]:
    report = json.loads((Path(run_dir) / "report.json").read_text(encoding="utf-8"))
    rows = []
    for entry in report.get("references") or []:
        verdicts = {
            (c.get("claim") or "").strip(): c.get("machine_verdict") or c.get("verdict", "")
            for c in entry.get("claim_verdicts") or []
        }
        existence = (entry.get("source") or {}).get("existence", "")
        for cite in entry.get("citations") or []:
            clause = (cite.get("claim") or cite.get("sentence") or "").strip()
            rows.append({
                "run_id": report.get("run_id", Path(run_dir).name),
                "ref_key": entry.get("key", ""),
                "pred_key": cite.get("key", ""),
                "label": cite.get("label", ""),
                "page": cite.get("page", ""),
                "group_size": cite.get("group_size", 1),
                "prose": "y" if cite.get("prose", True) else "n",
                "sentence": cite.get("sentence", ""),
                "clause": clause,
                "pred_existence": existence,
                "pred_support": verdicts.get(clause, ""),
                "evidence": pipeline.coverage_of(entry),
            })
    return rows


def write_sheet(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in COLUMNS})


def read_sheet(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def _yes(value: str) -> bool | None:
    value = (value or "").lower()
    if value in ("y", "yes", "1", "true"):
        return True
    if value in ("n", "no", "0", "false"):
        return False
    return None


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa for two labelings of the same items."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    left = Counter(a for a, _ in pairs)
    right = Counter(b for _, b in pairs)
    expected = sum(left[k] * right.get(k, 0) for k in left) / (n * n)
    if expected == 1:
        return 1.0 if observed == 1 else 0.0
    return round((observed - expected) / (1 - expected), 4)


def marker_metrics(rows: list[dict]) -> dict:
    labelled = [r for r in rows if _yes(r.get("h_is_citation")) is not None]
    predicted = [r for r in labelled if r.get("pred_key")]
    tp = sum(1 for r in predicted if _yes(r["h_is_citation"]))
    fp = len(predicted) - tp
    fn = sum(1 for r in labelled if not r.get("pred_key") and _yes(r["h_is_citation"]))
    return {"n": len(labelled), "tp": tp, "fp": fp, "fn": fn,
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn)}


def association_metrics(rows: list[dict]) -> dict:
    usable = [r for r in rows if r.get("pred_key") and _yes(r.get("h_is_citation"))
              and r.get("h_ref_key")]
    right = sum(1 for r in usable if r["pred_key"] == r["h_ref_key"])
    return {"n": len(usable), "correct": right, "accuracy": _ratio(right, len(usable))}


def clause_metrics(rows: list[dict]) -> dict:
    usable = [r for r in rows if _yes(r.get("h_clause_ok")) is not None]
    right = sum(1 for r in usable if _yes(r["h_clause_ok"]))
    return {"n": len(usable), "correct": right, "accuracy": _ratio(right, len(usable))}


def existence_metrics(rows: list[dict]) -> dict:
    """Per reference, not per citation: one label per (run, reference)."""
    seen: dict[tuple[str, str], tuple[str, bool]] = {}
    for r in rows:
        real = _yes(r.get("h_exists"))
        if real is None or not r.get("pred_existence"):
            continue
        seen[(r.get("run_id", ""), r.get("ref_key", ""))] = (r["pred_existence"], real)
    matrix = Counter(f"{pred}|{'exists' if real else 'does_not_exist'}"
                     for pred, real in seen.values())
    real = [p for p, e in seen.values() if e]
    fake = [p for p, e in seen.values() if not e]
    return {
        "n": len(seen),
        "confusion": dict(matrix),
        "false_no_match_rate": _ratio(sum(p == "not_found" for p in real), len(real)),
        "unconfirmed_rate_real": _ratio(sum(p == "unconfirmed" for p in real), len(real)),
        "fabrication_detection_rate": _ratio(sum(p == "not_found" for p in fake), len(fake)),
    }


def support_metrics(rows: list[dict]) -> dict:
    pairs = [(r["pred_support"], r["h_support"]) for r in rows
             if r.get("pred_support") in SUPPORT and r.get("h_support") in SUPPORT]
    out = {
        "n": len(pairs),
        "accuracy": _ratio(sum(a == b for a, b in pairs), len(pairs)),
        "kappa": cohen_kappa(pairs),
        "by_evidence": {},
    }
    for category in pipeline.COVERAGE_CATEGORIES:
        subset = [(r["pred_support"], r["h_support"]) for r in rows
                  if r.get("evidence") == category
                  and r.get("pred_support") in SUPPORT and r.get("h_support") in SUPPORT]
        if subset:
            out["by_evidence"][category] = {
                "n": len(subset),
                "accuracy": _ratio(sum(a == b for a, b in subset), len(subset)),
                "kappa": cohen_kappa(subset),
            }
    return out


def inter_annotator(first: list[dict], second: list[dict]) -> dict:
    """Agreement between two annotators on h_support, matched row by row."""
    key = lambda r: (r.get("run_id"), r.get("ref_key"), r.get("page"), r.get("clause"))
    other = {key(r): r.get("h_support") for r in second}
    pairs = [(r["h_support"], other[key(r)]) for r in first
             if r.get("h_support") in SUPPORT and other.get(key(r)) in SUPPORT]
    return {"n": len(pairs), "kappa": cohen_kappa(pairs)}


def score(rows: list[dict], second: list[dict] | None = None) -> dict:
    result = {
        "marker_extraction": marker_metrics(rows),
        "association": association_metrics(rows),
        "clause_scoping": clause_metrics(rows),
        "existence": existence_metrics(rows),
        "claim_support": support_metrics(rows),
    }
    if second is not None:
        result["inter_annotator_support"] = inter_annotator(rows, second)
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score CiteAudit against human labels.")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="write an annotation sheet from run reports")
    exp.add_argument("runs", nargs="+", type=Path)
    exp.add_argument("-o", "--output", type=Path, required=True)

    sc = sub.add_parser("score", help="compute metrics from a filled-in sheet")
    sc.add_argument("sheet", type=Path)
    sc.add_argument("--second", type=Path, help="second annotator's sheet")
    sc.add_argument("-o", "--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "export":
        rows = [row for run in args.runs for row in export_rows(run)]
        write_sheet(rows, args.output)
        print(f"{len(rows)} citation rows written to {args.output}")
        return 0

    result = score(read_sheet(args.sheet),
                   read_sheet(args.second) if args.second else None)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
