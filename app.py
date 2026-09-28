"""CiteCheck — upload a paper, verify what its references actually say.

Run with:  python app.py     then open http://127.0.0.1:5000
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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

# How long an uploaded manuscript and everything derived from it — the report,
# and captures of the manuscript's own pages — may stay on disk. Unpublished
# papers are confidential, and a server that keeps every one it was ever handed
# is holding material nobody asked it to hold. Unset or 0 keeps everything,
# which is right for a single user's own machine; any shared deployment should
# set it.
def _retention_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("CITECHECK_RETENTION_HOURS") or 0))
    except ValueError:
        return 0.0


_RETENTION_HOURS = _retention_hours()


def _running(run_id: str) -> bool:
    state = _RUNS.get(run_id)
    return state is not None and not state.get("done")


def _delete_run(run_id: str) -> None:
    """Remove a run's report, evidence images and uploaded manuscript."""
    shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
    for upload in UPLOADS_DIR.glob(f"{run_id}_*"):
        try:
            upload.unlink()
        except OSError:
            pass
    with _LOCK:
        _RUNS.pop(run_id, None)


def purge_expired(now: float | None = None, hours: float | None = None) -> list[str]:
    """Delete every finished run older than the retention period.

    Only names the server itself generated are touched, so nothing else that
    happens to sit in these directories can be swept up. A run still in progress
    is never deleted, however old its directory is.
    """
    hours = _RETENTION_HOURS if hours is None else hours
    if hours <= 0:
        return []
    cutoff = (time.time() if now is None else now) - hours * 3600

    # Measured from a run's most recent activity, not its creation: a report
    # re-checked this morning is still in use however old its upload is.
    latest: dict[str, float] = {}
    for path in list(RUNS_DIR.iterdir()) + list(UPLOADS_DIR.iterdir()):
        run_id = path.name[:22]
        if not _RUN_ID.fullmatch(run_id) or _running(run_id):
            continue
        for item in (path, path / "report.json"):
            try:
                stamp = item.stat().st_mtime
            except OSError:
                continue
            latest[run_id] = max(latest.get(run_id, 0.0), stamp)

    stale = sorted(run_id for run_id, stamp in latest.items() if stamp < cutoff)
    for run_id in stale:
        _delete_run(run_id)
    return stale


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
        "retention_hours": _RETENTION_HOURS,
        # Whether a key typed into the page could switch the model tier on. Not
        # when the operator forced it off, and not without the SDK installed.
        "user_key": not match.llm_forced_off() and _openai_installed(),
        # For the provider picker: what each one is called, what its key looks
        # like and which model a blank model field falls back to.
        "providers": [
            {"id": p.id, "label": p.label, "key_hint": p.key_hint,
             "default_model": match.model_name() if p.id == "openai" else p.default_model}
            for p in match.PROVIDERS.values()
        ],
        "local_llm": _ALLOW_LOCAL_LLM,
    }


def _openai_installed() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


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

    email, complaint = _email_arg()
    if complaint:
        return jsonify({"error": complaint}), 400
    llm, complaint = _llm_arg()
    if complaint:
        return jsonify({"error": complaint}), 400

    # Swept on upload rather than on a timer: a server nobody uses holds nothing
    # new, and one that is in use sweeps as often as it takes in manuscripts.
    purge_expired()

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
        contact_email=email,
        llm=llm,
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


# Deliberately loose — this only has to be good enough that a typo is caught
# here rather than turning into a silently skipped Unpaywall lookup an hour
# into a 250-reference run.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def _email_arg() -> tuple[str, str]:
    """The run's contact address, and a complaint if what was typed is not one.

    Returns `("", "")` when the field is blank — the environment's address then
    applies. A bad address is worse than none, because Unpaywall rejects the
    call and Crossref demotes the caller, so it is refused rather than dropped.
    """
    raw = (request.form.get("contact_email") or "").strip()
    if not raw:
        return "", ""
    if len(raw) > 254 or not _EMAIL_RE.match(raw):
        # Echoed back so the typo is visible, but clipped: the length check
        # exists precisely because the field can hold something enormous.
        return "", f"{raw[:80]!r} is not an email address."
    return raw, ""


# Printable ASCII with no spaces: every provider key shape seen so far (sk-...,
# sk-ant-..., AIza..., gsk_...) fits it. Only a paste accident — a stray
# newline, a quote, half a sentence — does not.
_API_KEY_RE = re.compile(r"^[!-~]{20,300}$")
# Model ids across providers: "gpt-4o", "claude-sonnet-5",
# "meta-llama/llama-3.3-70b-instruct:free", "models/gemini-2.5-flash".
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$")

# A custom endpoint is a URL the server will call on the user's say-so. On a
# public deployment that would let anyone aim it at the machine's own network,
# so by default only public https hosts are allowed. Running locally against
# Ollama or LM Studio is opted into explicitly.
_ALLOW_LOCAL_LLM = os.environ.get("CITECHECK_ALLOW_LOCAL_LLM", "") == "1"


def _endpoint_complaint(url: str) -> str:
    """Why *url* may not be used as a custom LLM endpoint, or "" if it may."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "That endpoint URL is not a valid address."
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return "The endpoint URL must start with https://."
    if parsed.username or parsed.password:
        return "Put the key in the API key field, not in the endpoint URL."
    if _ALLOW_LOCAL_LLM:
        return ""
    if parsed.scheme != "https":
        return "The endpoint URL must use https://."
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except (socket.gaierror, UnicodeError):
        return f"Could not find the host {parsed.hostname!r}."
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            return ("That endpoint is on a private network. To use a local model "
                    "server, set CITECHECK_ALLOW_LOCAL_LLM=1 on this machine.")
    return ""


def _llm_arg() -> tuple[match.LLMConfig, str]:
    """The run's choice of provider, key, model and endpoint, and a complaint.

    Every field blank means the server's own OpenAI setup. Unlike the email
    check, a complaint never echoes the key back: an error message is shown on
    screen and may be logged, and this one is a secret. Whether the key is
    *accepted* is only known once a call is made — a rejected key falls back to
    lexical scoring and the report says so; the Save button tests it up front.
    """
    default = match.LLMConfig()
    if request.form.get("use_model", "1") == "0":
        return default, ""

    provider = (request.form.get("provider") or "openai").strip()
    key = (request.form.get("api_key") or "").strip()
    model = (request.form.get("model") or "").strip()
    base_url = (request.form.get("base_url") or "").strip().rstrip("/")

    spec = match.PROVIDERS.get(provider)
    if spec is None:
        return default, "Pick a provider from the list."
    if key and not _API_KEY_RE.match(key):
        return default, "That API key doesn't look right — check it was pasted whole, or clear it."
    if model and not _MODEL_RE.match(model):
        return default, "That model name doesn't look right — it is usually something like gpt-4o or claude-sonnet-5."

    if provider == "custom":
        if not base_url:
            return default, "Add the endpoint URL of your OpenAI-compatible server."
        complaint = _endpoint_complaint(base_url)
        if complaint:
            return default, complaint
        if not model:
            return default, "Add the model name your endpoint serves."
    else:
        base_url = ""
        if provider != "openai" and not key:
            return default, f"Add your {spec.label} API key, or switch the provider back to OpenAI."

    return match.LLMConfig(provider=provider, api_key=key, model=model, base_url=base_url), ""


@app.post("/api/llm/check")
def llm_check():
    """Make one tiny call with the settings as entered, so a bad key or model
    name is caught when the settings are saved rather than an hour into a run."""
    if match.llm_forced_off():
        return jsonify({"ok": False, "error": "AI judging is turned off on this server."})
    llm, complaint = _llm_arg()
    if complaint:
        return jsonify({"ok": False, "error": complaint}), 400
    if not llm.key():
        return jsonify({"ok": False, "error": "No API key is set, here or on the server."})
    problem = match.check_connection(llm)
    return jsonify({"ok": not problem, "error": problem, "label": llm.label()})


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

    email, complaint = _email_arg()
    if complaint:
        return jsonify({"error": complaint}), 400
    llm, complaint = _llm_arg()
    if complaint:
        return jsonify({"error": complaint}), 400

    options = pipeline.Options(
        use_model=request.form.get("use_model", "1") != "0",
        take_screenshots=request.form.get("screenshots", "1") != "0" and _SHOTS_OK,
        max_claims_per_reference=_int_arg("max_claims", 6, 1, 20),
        contact_email=email,
        llm=llm,
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


@app.delete("/api/run/<run_id>")
def delete_run(run_id: str):
    """Delete a run now: its manuscript, report and evidence images.

    The retention period is a ceiling, not the only way out. Someone who has
    finished with a confidential manuscript should not have to wait for a
    sweep to be sure it is gone.
    """
    if not _RUN_ID.fullmatch(run_id):
        abort(404)
    if not (RUNS_DIR / run_id).exists() and not any(UPLOADS_DIR.glob(f"{run_id}_*")):
        abort(404)
    if _running(run_id):
        return jsonify({"error": "This run is still in progress."}), 409
    # Serialised with re-checks and verdicts, which read and rewrite the report
    # this is about to remove.
    with _RECHECK_LOCK:
        _delete_run(run_id)
    return jsonify({"deleted": run_id})


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

    purged = purge_expired()
    print("CiteCheck running at http://127.0.0.1:5000")
    print(
        "  retention        : "
        + (f"runs deleted {_RETENTION_HOURS:g} h after last use"
           f"{f' ({len(purged)} removed now)' if purged else ''}"
           if _RETENTION_HOURS else "kept until deleted — set CITECHECK_RETENTION_HOURS to expire them")
    )
    print(f"  relevance engine : {'OpenAI' if match.openai_available() else 'lexical (set OPENAI_API_KEY for model judging)'}")
    print(f"  screenshots      : {f'enabled via {_SHOTS_DETAIL}' if _SHOTS_OK else f'unavailable ({_SHOTS_DETAIL})'}")
    print(
        "  open access      : "
        + (f"Unpaywall enabled ({resolve.contact_email()})" if resolve.contact_email()
           else "Unpaywall skipped — set CITECHECK_CONTACT_EMAIL to enable it")
    )
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
