"""CiteCheck — upload a paper, verify what its references actually say.

Run with:  python app.py     then open http://127.0.0.1:5000
"""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from werkzeug.utils import secure_filename

from citecheck import match, pipeline, resolve, shots

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
UPLOADS_DIR = BASE_DIR / "uploads"
RUNS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 60

# Run ids are generated, never user-supplied — anything else is a path-traversal
# attempt against the run directories.
_RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# run_id -> {"queue": Queue, "events": [...], "done": bool, "report": dict|None}
_RUNS: dict[str, dict] = {}
_LOCK = threading.Lock()
# Re-checks read report.json, replace one entry and write it back, so two of
# them running at once would lose whichever finished first.
_RECHECK_LOCK = threading.Lock()

# Launching a browser takes a moment, so probe once at import and reuse.
_SHOTS_OK, _SHOTS_DETAIL = shots.browser_status()


_ENGINE_LABEL = {
    "openai": "OpenAI judging",
    "lexical": "Lexical judging",
}


def _capabilities() -> dict:
    engine = match.active_engine()
    return {
        # "configured", not "working" — whether the key is accepted is only
        # knowable once a call is made, so the report says what actually judged.
        "model": engine != "lexical",
        "engine": engine,
        "engine_label": _ENGINE_LABEL.get(engine, engine),
        "screenshots": _SHOTS_OK,
        "screenshot_detail": _SHOTS_DETAIL,
        "max_upload_mb": MAX_UPLOAD_MB,
        "contact_email": bool(resolve.contact_email()),
    }


# One shared password, for when the app is reachable from outside localhost.
# Nothing here is per-user: it exists so a link can be handed to someone without
# an account, not to identify who is on the other end.
#
# Unset, the app stays open — which is what you want on 127.0.0.1 and nowhere
# else. Uploading a paper costs real money against the configured model key, so
# an unset password on a public host means strangers spending it.
_PASSWORD = os.environ.get("CITECHECK_PASSWORD", "")
_USERNAME = os.environ.get("CITECHECK_USERNAME", "client")


@app.before_request
def _require_password():
    if not _PASSWORD:
        return None

    auth = request.authorization
    # compare_digest keeps the comparison constant-time; a plain == leaks the
    # password one character at a time to anyone willing to measure.
    if (
        auth is not None
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", _USERNAME)
        and hmac.compare_digest(auth.password or "", _PASSWORD)
    ):
        return None

    # The realm is the text the browser prints above its own login prompt, so
    # the visitor can tell what they are being asked to sign in to.
    return Response(
        "This demo is password-protected.\n",
        401,
        {"WWW-Authenticate": 'Basic realm="CiteCheck demo"'},
    )


@app.get("/")
def index():
    return render_template("index.html", capabilities=_capabilities())


@app.get("/api/capabilities")
def capabilities():
    return jsonify(_capabilities())


@app.post("/api/upload")
def upload():
    uploaded = request.files.get("pdf")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "No file was selected."}), 400
    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF."}), 400

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    safe_name = secure_filename(uploaded.filename) or "paper.pdf"
    pdf_path = UPLOADS_DIR / f"{run_id}_{safe_name}"
    uploaded.save(pdf_path)

    options = pipeline.Options(
        max_references=_int_arg("max_references", 250, 1, 500),
        use_model=request.form.get("use_model", "1") != "0",
        take_screenshots=request.form.get("screenshots", "1") != "0",
        workers=_int_arg("workers", 4, 1, 8),
    )

    with _LOCK:
        _RUNS[run_id] = {
            "events": [],
            "done": False,
            "report": None,
            "signal": threading.Condition(),
        }

    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id, str(pdf_path), run_dir, options),
        daemon=True,
    )
    thread.start()

    return jsonify({"run_id": run_id, "filename": safe_name})


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(request.form.get(name, default))))
    except (TypeError, ValueError):
        return default


def _run_pipeline(run_id: str, pdf_path: str, run_dir: Path, options: pipeline.Options) -> None:
    state = _RUNS[run_id]

    def emit(event: dict) -> None:
        with state["signal"]:
            state["events"].append(event)
            state["signal"].notify_all()

    try:
        report = pipeline.run(pdf_path, run_dir, options, progress=emit)
        state["report"] = report.to_dict()
    except Exception as exc:
        app.logger.error("Run %s failed:\n%s", run_id, traceback.format_exc())
        emit(
            {
                "stage": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "percent": 100,
            }
        )
    finally:
        with state["signal"]:
            state["done"] = True
            state["signal"].notify_all()


@app.get("/api/stream/<run_id>")
def stream(run_id: str):
    state = _RUNS.get(run_id)
    if state is None:
        abort(404)

    def generate():
        # Walk the event log by index so a client that connects mid-run replays
        # what it missed exactly once, and several clients can watch at once.
        sent = 0
        while True:
            with state["signal"]:
                while sent >= len(state["events"]) and not state["done"]:
                    state["signal"].wait(timeout=15)
                pending = state["events"][sent:]
                finished = state["done"] and not pending
            for event in pending:
                sent += 1
                yield _sse(event)
            if finished:
                yield _sse({"stage": "closed"})
                return
            if not pending:
                yield ": keep-alive\n\n"  # keeps idle proxies from closing us

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/recheck/<run_id>")
def recheck(run_id: str):
    """Re-run one reference, optionally against a document the reader supplies.

    Synchronous rather than streamed: this is one reference, it takes seconds
    rather than minutes, and the caller has a single card to update. Streaming
    it would mean a second event log for a job with one step in it.

    A `claim_index` narrows it to one citation of that reference — one lookup,
    one model call, and the sibling verdicts on the card left alone.
    """
    if not _RUN_ID.fullmatch(run_id):
        abort(404)
    run_dir = RUNS_DIR / run_id
    if not (run_dir / "report.json").exists():
        abort(404)

    key = (request.form.get("key") or "").strip()
    if not key:
        return jsonify({"error": "No reference was named."}), 400

    # Absent means "re-check the whole reference"; a number picks one of the
    # citations judged inside it. Parsed exactly as /api/verdict parses it, so
    # the two per-citation actions cannot disagree about what an index means.
    raw_index = (request.form.get("claim_index") or "").strip()
    try:
        claim_index = int(raw_index) if raw_index else None
    except ValueError:
        return jsonify({"error": f"{raw_index!r} is not a citation number."}), 400

    supplied = None
    uploaded = request.files.get("source")
    if uploaded is not None and uploaded.filename:
        try:
            supplied = pipeline.read_supplied(uploaded.filename, uploaded.read())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    options = pipeline.Options(
        use_model=request.form.get("use_model", "1") != "0",
        take_screenshots=request.form.get("screenshots", "1") != "0" and _SHOTS_OK,
        max_claims_per_reference=_int_arg("max_claims", 6, 1, 20),
    )

    papers = sorted(UPLOADS_DIR.glob(f"{run_id}_*"))
    try:
        # Serialised: two re-checks of the same run both read report.json, both
        # write it back, and the second one silently discards the first.
        with _RECHECK_LOCK:
            report_data = pipeline.recheck_one(
                run_dir,
                key,
                options,
                paper_path=str(papers[0]) if papers else "",
                supplied=supplied,
                claim_index=claim_index,
            )
    except KeyError:
        return jsonify({"error": f"Reference {key} is not in this report."}), 404
    except Exception as exc:
        app.logger.error("Recheck %s/%s failed:\n%s", run_id, key, traceback.format_exc())
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    # Keep the in-memory copy in step, so a later /api/report does not serve the
    # pre-recheck report back over the one just written.
    state = _RUNS.get(run_id)
    if state is not None:
        state["report"] = report_data

    return jsonify(_one_reference(report_data, key))


def _one_reference(report: dict, key: str) -> dict:
    """The reply to a change that touched exactly one reference.

    Deliberately *not* the whole report. A 150-reference screening report is
    around two megabytes of JSON, and returning it after every click meant a
    verdict change — which costs the server 60ms — spent seconds on the wire
    for a payload the client already held, all but one entry of it unchanged.
    Only the entry, the derived tally and the derived warnings can have moved,
    so only those come back.
    """
    return {
        "entry": next(
            (e for e in report.get("references") or [] if e["key"] == key), None
        ),
        "stats": report.get("stats", {}),
        "warnings": report.get("warnings", []),
    }


@app.post("/api/verdict/<run_id>")
def verdict(run_id: str):
    """Record the reader's own verdict on one reference, or drop it again.

    The tool screens; a person decides. Once someone has opened the source and
    read it, their judgement beats anything here — and the report has to be able
    to carry that, clearly labelled as theirs.
    """
    if not _RUN_ID.fullmatch(run_id):
        abort(404)
    run_dir = RUNS_DIR / run_id
    if not (run_dir / "report.json").exists():
        abort(404)

    key = (request.form.get("key") or "").strip()
    if not key:
        return jsonify({"error": "No reference was named."}), 400

    # Absent means "the reference's own headline"; a number picks one of the
    # citations judged inside it.
    raw_index = (request.form.get("claim_index") or "").strip()
    try:
        claim_index = int(raw_index) if raw_index else None
    except ValueError:
        return jsonify({"error": f"{raw_index!r} is not a citation number."}), 400

    try:
        with _RECHECK_LOCK:
            report_data = pipeline.set_verdict(
                run_dir,
                key,
                verdict=(request.form.get("verdict") or "").strip(),
                note=request.form.get("note") or "",
                clear=request.form.get("clear") == "1",
                claim_index=claim_index,
            )
    except KeyError:
        return jsonify({"error": f"Reference {key} is not in this report."}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    state = _RUNS.get(run_id)
    if state is not None:
        state["report"] = report_data

    return jsonify(_one_reference(report_data, key))


@app.get("/api/report/<run_id>")
def report(run_id: str):
    state = _RUNS.get(run_id)
    if state and state.get("report"):
        return jsonify(state["report"])

    path = RUNS_DIR / run_id / "report.json"
    if not path.exists():
        abort(404)

    # Re-derived on the way out rather than served verbatim. Everything
    # `summarise` computes is a function of the entries, so this changes nothing
    # for a report written by the current code — but a report written by an
    # older one is missing whatever the tally has learned to count since, and
    # would otherwise show a summary that disagrees with its own cards for ever.
    return jsonify(pipeline.summarise(json.loads(path.read_text(encoding="utf-8"))))


@app.get("/api/paper/<run_id>")
def paper(run_id: str):
    """Serve the uploaded PDF so the report can deep-link to a page in it."""
    if not _RUN_ID.fullmatch(run_id):
        abort(404)
    matches = sorted(UPLOADS_DIR.glob(f"{run_id}_*"))
    if not matches:
        abort(404)
    return send_from_directory(
        UPLOADS_DIR, matches[0].name, mimetype="application/pdf", as_attachment=False
    )


@app.get("/runs/<run_id>/shots/<path:filename>")
def shot(run_id: str, filename: str):
    directory = RUNS_DIR / run_id / "shots"
    if not directory.is_dir():
        abort(404)
    return send_from_directory(directory, filename)


@app.errorhandler(413)
def too_large(_err):
    return jsonify({"error": f"That file is larger than the {MAX_UPLOAD_MB} MB limit."}), 413


def _port_is_free(host: str, port: int) -> bool:
    """Windows lets a second process bind an already-served port, which silently
    splits requests between two servers. Check before starting."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) != 0


if __name__ == "__main__":
    HOST, PORT = "127.0.0.1", 5000
    if not _port_is_free(HOST, PORT):
        raise SystemExit(
            f"Something is already serving http://{HOST}:{PORT} — stop it first, "
            "otherwise requests get split between two servers."
        )

    print("CiteCheck running at http://127.0.0.1:5000")
    print(f"  relevance engine : {'OpenAI' if match.openai_available() else 'lexical (set OPENAI_API_KEY for model judging)'}")
    print(f"  screenshots      : {f'enabled via {_SHOTS_DETAIL}' if _SHOTS_OK else f'unavailable ({_SHOTS_DETAIL})'}")
    print(
        "  open access      : "
        + (f"Unpaywall enabled ({resolve.contact_email()})" if resolve.contact_email()
           else "Unpaywall skipped — set CITECHECK_CONTACT_EMAIL to enable it")
    )
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
