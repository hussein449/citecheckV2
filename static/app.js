/* CiteCheck front-end: upload → stream progress → render the report. */

const $ = (id) => document.getElementById(id);

const panels = {
  upload: $("panel-upload"),
  progress: $("panel-progress"),
  results: $("panel-results"),
};

let currentRun = null;
let currentReport = null;
let activeFilter = "all";
/* Which cards the reader has expanded. Held by reference key rather than by DOM
   node, because re-checking one reference rebuilds every card — and a card that
   collapses itself the moment its verdict changes hides the very thing the
   reader just asked for. */
const openKeys = new Set();
let recheckInFlight = false;

/* Ordered by how much each verdict demands a look, worst first — the same
   ordering the report itself uses, so tiles, filters and cards all agree. */
const VERDICTS = ["not_found", "unrelated", "weak", "unverified", "related", "supported"];

const VERDICT_LABEL = {
  supported: "Supported",
  related: "Related",
  weak: "Weak link",
  unrelated: "Unrelated",
  unverified: "Unverified",
  not_found: "Not found",
};

const ENGINE_LABEL = {
  openai: "OpenAI",
  lexical: "lexical overlap",
};

const RISK_TITLE = {
  critical: "Critical issues found",
  concern: "Issues found",
  review: "Needs review",
  clear: "No integrity problems found",
};

/* ── Upload ─────────────────────────────────────────────── */

const drop = $("drop");
const fileInput = $("file");

$("browse").addEventListener("click", (e) => { e.stopPropagation(); fileInput.click(); });
drop.addEventListener("click", () => fileInput.click());

["dragenter", "dragover"].forEach((evt) =>
  drop.addEventListener(evt, (e) => { e.preventDefault(); drop.classList.add("over"); })
);
["dragleave", "drop"].forEach((evt) =>
  drop.addEventListener(evt, (e) => { e.preventDefault(); drop.classList.remove("over"); })
);
drop.addEventListener("drop", (e) => {
  const file = e.dataTransfer?.files?.[0];
  if (file) startRun(file);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files?.[0]) startRun(fileInput.files[0]);
});

async function startRun(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    return showError("That doesn't look like a PDF.");
  }
  hideError();

  const body = new FormData();
  body.append("pdf", file);
  body.append("max_references", $("maxRefs").value);
  body.append("workers", $("workers").value);
  body.append("use_model", $("useModel").checked && !$("useModel").disabled ? "1" : "0");
  body.append("screenshots", $("useShots").checked && !$("useShots").disabled ? "1" : "0");

  show("progress");
  $("progressTitle").textContent = file.name;
  $("statusLine").textContent = "Uploading…";
  $("barFill").style.width = "2%";
  $("log").innerHTML = "";

  let data;
  try {
    const res = await fetch("/api/upload", { method: "POST", body });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
  } catch (err) {
    show("upload");
    return showError(err.message);
  }

  currentRun = data.run_id;
  listen(data.run_id);
}

function listen(runId) {
  const source = new EventSource(`/api/stream/${runId}`);

  source.onmessage = (msg) => {
    const event = JSON.parse(msg.data);

    if (event.stage === "closed") { source.close(); return; }

    if (typeof event.percent === "number") {
      $("barFill").style.width = `${event.percent}%`;
    }
    if (event.message) {
      $("statusLine").textContent = event.message;
      addLog(event);
    }
    if (event.stage === "error") {
      source.close();
      show("upload");
      showError(event.message);
      return;
    }
    if (event.stage === "done" && event.report) {
      source.close();
      renderReport(event.report, runId);
    }
  };

  source.onerror = () => {
    source.close();
    if (!currentReport) {
      $("statusLine").textContent = "Lost the connection to the server.";
    }
  };
}

function addLog(event) {
  const li = document.createElement("li");
  const dot = document.createElement("span");
  dot.className = "dot";
  if (event.entry?.verdict) dot.style.background = colorFor(event.entry.verdict);
  li.append(dot, document.createTextNode(event.message));
  const log = $("log");
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
}

/* ── Results ────────────────────────────────────────────── */

function renderReport(report, runId) {
  currentReport = report;
  currentRun = runId;
  activeFilter = "all";
  openKeys.clear();
  show("results");
  refreshResults();
}

/* Everything downstream of `currentReport`, rebuilt from it. Called again after
   a single reference is re-checked, so the banner, the tally and the filters
   never keep counting a verdict that has since been overturned. */
function refreshResults() {
  const report = currentReport || {};
  const s = report.stats || {};
  $("resultTitle").textContent = report.paper_title || report.source_pdf;
  $("resultMeta").textContent = [
    `${s.pages ?? "?"} pages`,
    `${s.references_checked ?? 0} of ${s.references_cited ?? 0} cited references checked`,
    s.claims_judged ? `${s.claims_judged} individual claims judged` : null,
    s.rechecked ? `${s.rechecked} re-checked by hand` : null,
    s.reviewed ? `${s.reviewed} verdict${s.reviewed === 1 ? "" : "s"} set by you` : null,
    engineSummary(s),
    `${s.elapsed_seconds ?? "?"}s`,
  ].filter(Boolean).join(" · ");

  const note = $("engineNote");
  note.hidden = !s.engine_note;
  note.textContent = s.engine_note || "";

  renderRisk(s.risk);
  renderTally(s);
  renderWarnings(report.warnings);
  renderFilters(s.verdicts || {}, report.references || []);
  renderCards();
}

// Names the engine that produced the verdicts, and how many references it
// actually judged. A configured key that was rejected reads as "lexical
// overlap" here, because that is what every verdict below it came from.
function engineSummary(stats) {
  const judged = stats.references_judged_by_model || 0;
  if (!judged) return "judged by lexical overlap";
  const label = ENGINE_LABEL[stats.engine] || stats.engine;
  return `judged by ${label} (${judged} of ${stats.references_judgeable} references)`;
}

function renderRisk(risk) {
  const box = $("riskBanner");
  if (!risk) { box.hidden = true; return; }
  box.hidden = false;
  box.className = `risk ${risk.level || "clear"}`;
  box.innerHTML =
    `<h3>${esc(RISK_TITLE[risk.level] || risk.level)}</h3><ul>` +
    (risk.headlines || []).map((h) => `<li>${esc(h)}</li>`).join("") +
    "</ul>";
}

function renderTally(stats) {
  const counts = stats.verdicts || {};
  const tiles = VERDICTS.filter((v) => counts[v]).map(
    (v) => `<div class="tile ${v}"><span class="n">${counts[v]}</span>
              <span class="k">${esc(VERDICT_LABEL[v])}</span></div>`
  );
  /* Retractions never show up as a verdict — a retracted paper can still
     support the claim it was cited for. They need their own tile or they
     vanish from the summary entirely. */
  if (stats.retracted) {
    tiles.unshift(`<div class="tile unrelated"><span class="n">${stats.retracted}</span>
                     <span class="k">Retracted</span></div>`);
  }
  $("tally").innerHTML = tiles.join("");
}

function renderWarnings(warnings) {
  const box = $("warnings");
  if (!warnings?.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML =
    "<strong>Worth knowing</strong><ul>" +
    warnings.map((w) => `<li>${esc(w)}</li>`).join("") +
    "</ul>";
}

function renderFilters(counts, references) {
  const present = ["all", ...VERDICTS.filter((v) => counts[v])];
  $("filters").innerHTML = present
    .map((v) => `<button data-f="${v}">${
      v === "all" ? `All (${references.length})` : `${VERDICT_LABEL[v]} (${counts[v]})`
    }</button>`)
    .join("");
  $("filters").querySelectorAll("button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.f === activeFilter);
    btn.addEventListener("click", () => {
      activeFilter = btn.dataset.f;
      $("filters").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      renderCards();
    });
  });
}

/* Most concerning first, so the findings that matter are never below the fold. */
const CONCERN = { not_found: 6, unrelated: 5, weak: 4, unverified: 3, related: 2, supported: 1 };

/* The verdict alone is not the whole story. A retracted source can still be
   "related" to the claim it was cited for — that says nothing about whether it
   should have been cited at all, and ranking on verdict alone buries it under
   references we merely could not read. */
function urgency(entry) {
  let score = CONCERN[entry.verdict] || 0;
  if (entry.source?.retracted) score = Math.max(score, 6.5);
  else if ((entry.flags || []).some((f) => f.severity === "high")) score = Math.max(score, 4.5);
  return score;
}

function renderCards() {
  const list = (currentReport.references || [])
    .filter((r) => activeFilter === "all" || r.verdict === activeFilter)
    .sort((a, b) => urgency(b) - urgency(a));

  $("cards").innerHTML = list.map(cardHtml).join("") ||
    `<p class="status">Nothing in this category.</p>`;

  $("cards").querySelectorAll(".card-head").forEach((head) => {
    head.addEventListener("click", () => {
      const card = head.parentElement;
      const open = card.classList.toggle("open");
      if (open) openKeys.add(card.dataset.key);
      else openKeys.delete(card.dataset.key);
    });
  });
  $("cards").querySelectorAll(".shot img").forEach((img) => {
    img.addEventListener("click", (e) => {
      e.stopPropagation();
      openLightbox(img.src, img.dataset.caption || "");
    });
  });
  $("cards").querySelectorAll(".recheck-run").forEach((btn) => {
    btn.addEventListener("click", () => runRecheck(btn.closest(".recheck").dataset.key, null));
  });
  $("cards").querySelectorAll(".review-save").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = btn.closest(".recheck");
      setVerdict(box.dataset.key, { verdict: box.querySelector(".review-pick").value });
    });
  });
  $("cards").querySelectorAll(".review-clear").forEach((btn) => {
    btn.addEventListener("click", () => {
      setVerdict(btn.closest(".recheck").dataset.key, { clear: "1" });
    });
  });
  $("cards").querySelectorAll(".recheck input[type=file]").forEach((input) => {
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      // Cleared so picking the same file twice still fires a change event —
      // re-running the identical document is a legitimate thing to want.
      const key = input.closest(".recheck").dataset.key;
      input.value = "";
      if (file) runRecheck(key, file);
    });
  });
}

/* ── Re-checking one reference ──────────────────────────── */

/* A verdict is a prompt to look, and looking sometimes says the tool got it
   wrong: it resolved to the wrong paper, or read a paywall stub, or the
   publisher was simply down. Re-running the whole paper to settle one reference
   costs minutes and re-does 200 checks that were already right. */
async function runRecheck(key, file) {
  if (recheckInFlight) return;

  const box = document.querySelector(`.recheck[data-key="${key}"]`);
  const note = box?.querySelector(".recheck-status");
  const controls = box ? [...box.querySelectorAll(".btn")] : [];

  recheckInFlight = true;
  openKeys.add(key);
  controls.forEach((el) => el.classList.add("busy"));
  setNote(note, "status working", file
    ? `Judging this reference against ${file.name}…`
    : "Looking this reference up again and re-reading the source…");

  const body = new FormData();
  body.append("key", key);
  if (file) body.append("source", file);
  body.append("use_model", $("useModel").checked && !$("useModel").disabled ? "1" : "0");
  body.append("screenshots", $("useShots").checked && !$("useShots").disabled ? "1" : "0");

  try {
    const res = await fetch(`/api/recheck/${currentRun}`, { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Re-check failed (${res.status})`);

    currentReport = data.report;
    refreshResults();          // the card, and every control on it, is rebuilt
    reportRecheck(key, data.entry);
  } catch (err) {
    setNote(note, "status error", err.message);
  } finally {
    recheckInFlight = false;
    controls.forEach((el) => el.classList.remove("busy"));
  }
}

/* Unlike a re-check this is instant — no network fetch, no model call — so it
   needs no busy state, just the report coming back changed. */
async function setVerdict(key, fields) {
  const box = document.querySelector(`.recheck[data-key="${key}"]`);
  const note = box?.querySelector(".recheck-status");

  const body = new FormData();
  body.append("key", key);
  Object.entries(fields).forEach(([name, value]) => body.append(name, value));

  try {
    const res = await fetch(`/api/verdict/${currentRun}`, { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Could not save that (${res.status})`);

    currentReport = data.report;
    openKeys.add(key);
    refreshResults();

    const after = document.querySelector(`.recheck[data-key="${key}"]`);
    setNote(after?.querySelector(".recheck-status"), "status done",
      fields.clear
        ? `Back to the tool's verdict — ${VERDICT_LABEL[data.entry.verdict] || data.entry.verdict}.`
        : `Recorded as your verdict: ${VERDICT_LABEL[data.entry.verdict] || data.entry.verdict}.`);
    after?.scrollIntoView({ block: "center", behavior: "smooth" });
  } catch (err) {
    setNote(note, "status error", err.message);
  }
}

function setNote(note, className, text) {
  if (!note) return;
  note.hidden = false;
  note.className = `recheck-status ${className}`;
  note.textContent = text;
}

/* Says what changed, not just that something happened. "Still unverified" is a
   real answer and the reader needs to be told it plainly, or they will keep
   pressing the button expecting a different one. */
function reportRecheck(key, entry) {
  const box = document.querySelector(`.recheck[data-key="${key}"]`);
  if (!box || !entry) return;
  const was = entry.rechecked?.previous_verdict;
  const now = VERDICT_LABEL[entry.verdict] || entry.verdict;
  /* Their own verdict was a reading of evidence this re-check has just
     replaced, so it was dropped. Losing someone's conclusion silently is worse
     than losing it. */
  const dropped = entry.rechecked?.cleared_review
    ? ` Your own verdict was cleared — it judged the evidence this replaced.`
    : "";
  setNote(
    box.querySelector(".recheck-status"),
    "status done",
    (was && was !== entry.verdict
      ? `Re-checked: ${VERDICT_LABEL[was] || was} → ${now}.`
      : `Re-checked — the verdict is still ${now}.`) + dropped
  );
  box.scrollIntoView({ block: "center", behavior: "smooth" });
}

function cardHtml(entry) {
  const ref = entry.reference || {};
  const src = entry.source || {};
  const shots = entry.shots || {};
  const label = ref.number != null ? `[${ref.number}]` : `(${entry.key})`;
  const title = ref.title || src.title || truncate(ref.raw || "Untitled reference", 110);
  const paperUrl = `/api/paper/${currentRun}`;

  const flags = entry.flags || [];
  const chips = [
    src.retracted ? `<span class="chip retracted">Retracted</span>` : "",
    !src.retracted && flags.some((f) => f.severity === "high")
      ? `<span class="chip flagged">Flagged</span>` : "",
    entry.rechecked ? `<span class="chip rechecked">Re-checked</span>` : "",
    entry.reviewed ? `<span class="chip reviewed">Your verdict</span>` : "",
  ].join("");

  /* Evidence images are written back to the same filenames on a re-check, so
     the browser would keep serving the pre-recheck capture from cache. */
  const shotVersion = entry.rechecked?.at || "";

  const flagsHtml = flags.length
    ? `<div class="section"><h4>Flags</h4>${flags
        .map((f) => `<p class="flag ${esc(f.severity)}">${esc(f.message)}</p>`)
        .join("")}</div>`
    : "";

  /* Each citing sentence, judged on its own. This is the heart of the report:
     one reference can support one claim and be oversold for the next. */
  const claims = entry.claim_verdicts || [];
  const claimsHtml = claims.length
    ? `<div class="section"><h4>Each place it's cited, judged separately</h4>
         <div class="claims">${claims.map((c) => claimHtml(c, paperUrl)).join("")}</div></div>`
    : `<div class="section"><h4>Where it's cited in your paper</h4>${
        (entry.citations || []).map((c) => `
          <blockquote class="quote">${esc(c.sentence)}
            <span class="where">${esc(c.label)} ·
              <a href="${paperUrl}#page=${c.page}" target="_blank" rel="noopener">page ${c.page} ↗</a>
            </span></blockquote>`).join("") || "<p class='status'>No sentence captured.</p>"
      }</div>`;

  /* Only retractions are surfaced. Corrections and errata are still collected
     — the retraction notice itself comes out of this list — but a published
     erratum says nothing about whether the citation matches the reference,
     which is the only question this report answers. */
  const integrity = src.retracted && (src.integrity || []).length
    ? `<div class="section"><h4>Published notices on this work</h4>
        <ul class="meta-list">${src.integrity
          .map((i) => `<li><strong>${esc(cap(i.kind))}</strong>${i.date ? ` (${esc(i.date)})` : ""}${
            i.doi ? ` — ${esc(i.doi)}` : ""} <span class="sub">via ${esc(i.source)}</span></li>`)
          .join("")}</ul></div>`
    : "";

  const suppliedEvidence = entry.rechecked?.against === "supplied";
  const shotBlocks = [];
  if (entry.citing_shot) {
    shotBlocks.push(shotHtml(entry.citing_shot,
      `In your paper${entry.citing_page ? ` — page ${entry.citing_page}` : ""}`,
      entry.claim || "", `${paperUrl}#page=${entry.citing_page || 1}`, shotVersion));
  }
  if (shots.header) {
    shotBlocks.push(shotHtml(shots.header, "Top of the source page",
      shots.page_title || title, "", shotVersion));
  }
  if (shots.evidence) {
    shotBlocks.push(shotHtml(shots.evidence,
      suppliedEvidence ? `From the file you supplied — ${entry.rechecked.filename}`
        : shots.evidence_is_card ? "Indexed abstract (page was not capturable)"
                                 : "Matching passage, highlighted",
      shots.matched_text || "", "", shotVersion));
  }
  const shotsHtml = shotBlocks.length
    ? `<div class="section"><h4>Evidence</h4><div class="shots">${shotBlocks.join("")}</div></div>`
    : "";

  const notes = (entry.notes || []).filter(Boolean);
  const url = src.landing_url || src.url || "";
  const found = [
    esc(src.resolver || "—"),
    (src.indices_hit || []).length ? `confirmed by ${esc(src.indices_hit.join(", "))}` : "",
  ].filter(Boolean).join(" · ");

  const meta = `
    <div class="section"><h4>Reference detail</h4>
      <ul class="meta-list">
        <li><strong>As printed:</strong> ${esc(truncate(ref.raw || "", 320))}</li>
        ${url ? `<li><strong>Resolved to:</strong> <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a></li>` : ""}
        ${src.oa_url && src.oa_url !== url ? `<li><strong>Open-access copy:</strong> <a href="${esc(src.oa_url)}" target="_blank" rel="noopener noreferrer">${esc(src.oa_url)}</a></li>` : ""}
        ${src.doi ? `<li><strong>DOI:</strong> ${esc(src.doi)}</li>` : ""}
        <li><strong>Found via:</strong> ${found}</li>
        <li><strong>Content checked:</strong> ${esc(entry.fetched?.kind || "nothing retrieved")}${
          entry.fetched?.text_chars ? ` (${entry.fetched.text_chars.toLocaleString()} chars)` : ""}</li>
        <li><strong>Cited:</strong> ${entry.citation_count} time${entry.citation_count === 1 ? "" : "s"}</li>
      </ul>
      ${notes.length ? `<ul class="notes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    </div>`;

  return `
  <article class="card${openKeys.has(entry.key) ? " open" : ""}" data-key="${esc(entry.key)}">
    <div class="card-head">
      <span class="card-num">${esc(label)}</span>
      <span class="badge ${esc(entry.verdict)}">${esc(VERDICT_LABEL[entry.verdict] || entry.verdict)}</span>
      <div class="card-title">
        <h3>${esc(title)}${chips}</h3>
        <div class="sub">${esc(truncate(entry.reason || "", 180))}</div>
      </div>
      <span class="chev">›</span>
    </div>
    <div class="card-body">
      <div class="section"><h4>Verdict</h4>
        <p class="reason">${esc(entry.reason || "No explanation available.")}</p>
        ${entry.reviewed ? `<p class="reason machine-said"><span>What the tool found:</span>
           ${esc(VERDICT_LABEL[entry.reviewed.machine_verdict] || entry.reviewed.machine_verdict || "—")}
           — ${esc(truncate(entry.reviewed.machine_reason || "", 400))}</p>` : ""}
      </div>
      ${recheckHtml(entry)}
      ${flagsHtml}
      ${claimsHtml}
      ${integrity}
      ${shotsHtml}
      ${meta}
    </div>
  </article>`;
}

/* Sits directly under the verdict, because that is where a reader decides they
   disagree with it. */
function recheckHtml(entry) {
  const done = entry.rechecked
    ? `<p class="recheck-was">Last re-checked ${esc(entry.rechecked.at || "")}${
        entry.rechecked.against === "supplied"
          ? ` against <b>${esc(entry.rechecked.filename || "a supplied file")}</b>`
          : " against the citation indexes"}.</p>`
    : "";
  return `
    <div class="section recheck" data-key="${esc(entry.key)}">
      <h4>Check this one again</h4>
      <p class="status">Re-run this reference on its own — useful when the
        publisher was down, or when the lookup landed on the wrong paper. If you
        have the source yourself, hand it over and it will be judged against that
        instead of against anything retrieved.</p>
      <div class="recheck-actions">
        <button type="button" class="btn ghost recheck-run">Re-run the check</button>
        <label class="btn ghost recheck-pick">Judge against a file…
          <input type="file" hidden
                 accept=".pdf,.txt,.text,.md,application/pdf,text/plain">
        </label>
        <span class="recheck-hint">PDF or .txt</span>
      </div>
      ${reviewHtml(entry)}
      ${done}
      <p class="recheck-status" hidden></p>
    </div>`;
}

/* Setting the verdict yourself. Sits with the other two actions because it is
   the same decision — "the tool got this wrong" — just answered from your own
   reading rather than by asking the tool to look again. */
function reviewHtml(entry) {
  const review = entry.reviewed;
  const chosen = review?.verdict || entry.verdict;
  const options = VERDICTS.map(
    (v) => `<option value="${v}"${v === chosen ? " selected" : ""}>${esc(VERDICT_LABEL[v])}</option>`
  ).join("");

  const standing = review
    ? `<p class="review-was">You set this to <b>${esc(VERDICT_LABEL[review.verdict] || review.verdict)}</b>
         on ${esc(review.at || "")}. The tool had it as
         <b>${esc(VERDICT_LABEL[review.machine_verdict] || review.machine_verdict || "—")}</b>.
         ${review.note ? `<span class="review-note">“${esc(review.note)}”</span>` : ""}</p>`
    : "";

  return `
    <div class="review">
      <div class="recheck-actions">
        <select class="review-pick" aria-label="Your verdict for this reference">${options}</select>
        <button type="button" class="btn ghost review-save">Set this verdict myself</button>
        ${review ? `<button type="button" class="btn ghost review-clear">Use the tool's verdict again</button>` : ""}
      </div>
      <p class="review-hint">Recorded as yours, not the tool's — on the card, in the
        tally and in the exported PDF.</p>
      ${standing}
    </div>`;
}

function claimHtml(claim, paperUrl) {
  const verdict = claim.verdict || "unverified";
  const page = claim.page
    ? `<span class="where"><a href="${paperUrl}#page=${claim.page}" target="_blank" rel="noopener">page ${claim.page} ↗</a></span>`
    : "";
  /* The claim is the clause the marker governs, which is usually narrower than
     the sentence it sits in. Showing the sentence underneath is what lets a
     reader confirm the right slice was judged — and see the other references
     that carry the rest of it. */
  const context = claim.context
    ? `<p class="claim-context"><span>In full:</span> ${esc(truncate(claim.context, 400))}</p>`
    : "";
  const revisited = claim.reconsidered
    ? `<span class="revisited">softened after re-reading the abstract</span>` : "";
  return `
    <div class="claim ${esc(verdict)}">
      <div class="claim-top">
        <span class="badge ${esc(verdict)}">${esc(VERDICT_LABEL[verdict] || verdict)}</span>
        ${page}${revisited}
      </div>
      <blockquote>${esc(claim.claim)}</blockquote>
      ${context}
      <p class="why">${esc(claim.reason || "")}</p>
      ${claim.evidence_quote
        ? `<p class="verbatim">“${esc(truncate(claim.evidence_quote, 400))}”</p>` : ""}
    </div>`;
}

function shotHtml(filename, caption, alt, linkUrl, version) {
  const url = `/runs/${currentRun}/shots/${encodeURIComponent(filename)}`
    + (version ? `?v=${encodeURIComponent(version)}` : "");
  const link = linkUrl
    ? ` <a href="${linkUrl}" target="_blank" rel="noopener" class="shot-link">open ↗</a>`
    : "";
  return `<div class="shot"><figure>
      <img src="${url}" loading="lazy" alt="${esc(truncate(alt, 120))}" data-caption="${esc(caption)}">
      <figcaption>${esc(caption)}${link}</figcaption>
    </figure></div>`;
}

/* ── Lightbox ───────────────────────────────────────────── */

function openLightbox(src, caption) {
  $("lightboxImg").src = src;
  $("lightboxCaption").textContent = caption;
  $("lightbox").hidden = false;
}
$("lightboxClose").addEventListener("click", () => ($("lightbox").hidden = true));
$("lightbox").addEventListener("click", (e) => {
  if (e.target.id === "lightbox") $("lightbox").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("lightbox").hidden = true;
});

/* ── Export ─────────────────────────────────────────────── */

/* Printing is the export: the report is already a document, the screenshots
   are already on the page, and the browser's own "Save as PDF" produces a file
   the reader can hand on. Generating a PDF server-side would mean rendering
   this same page in headless Chromium — the copy of it that takes the
   screenshots — for a worse result, because it cannot see which filter the
   reader is looking at. */
$("exportPdf").addEventListener("click", async () => {
  const button = $("exportPdf");
  const cards = [...document.querySelectorAll("#cards .card")];
  /* A collapsed card prints as a heading and nothing else, so every card is
     opened for the export and put back exactly as the reader had it. */
  const wasOpen = cards.map((card) => card.classList.contains("open"));
  const restore = () => cards.forEach((card, i) => card.classList.toggle("open", wasOpen[i]));

  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    cards.forEach((card) => card.classList.add("open"));
    /* Evidence screenshots are the point of the report, and they are lazy on
       screen — which means that on a long report almost none of them have been
       fetched, because almost none have been scrolled to. Opening the cards is
       not enough: a lazy image below the viewport stays unfetched, and would
       print as an empty box. Ask for them all, then wait. */
    const missing = await imagesSettled($("cards"), (loaded, total) => {
      // On a large report this takes tens of seconds. A button that sits on
      // "Preparing…" that long reads as a hang, and the reader clicks again.
      button.textContent = `Preparing… ${loaded}/${total}`;
    });
    $("printMeta").textContent = exportCaption(missing);
    window.addEventListener("afterprint", restore, { once: true });
    window.print();
  } finally {
    restore();
    button.disabled = false;
    button.textContent = "Export as PDF";
  }
});

/* The PDF outlives the screen it was exported from, so it has to say what it
   contains. A filtered export is a legitimate thing to send someone — "here
   are the five that don't check out" — but only if it admits it is a subset. */
function exportCaption(missingShots = 0) {
  const total = (currentReport?.references || []).length;
  const shown = document.querySelectorAll("#cards .card").length;
  const scope = activeFilter === "all"
    ? `All ${total} checked reference${total === 1 ? "" : "s"}`
    : `Filtered to “${VERDICT_LABEL[activeFilter] || activeFilter}” — ${shown} of ${total} checked references`;
  /* If the export goes out with blank evidence boxes, the file has to say so.
     A reader cannot tell a screenshot that failed to load from one that was
     never captured, and the second would look like the check was skipped. */
  const gap = missingShots
    ? ` · ${missingShots} screenshot${missingShots === 1 ? "" : "s"} did not load and appear blank — re-export to include them`
    : "";
  return `${scope} · exported ${new Date().toLocaleString()}${gap}`;
}

/* Resolves with the number of images that never arrived. */
function imagesSettled(root, onProgress = () => {}, timeout = 60000) {
  const images = [...root.querySelectorAll("img")];
  images.forEach((img) => {
    // Overrides loading="lazy", which is right for the screen and wrong here.
    if (img.loading === "lazy") img.loading = "eager";
    img.decoding = "sync";
  });

  const pending = images.filter((img) => !img.complete);
  let done_ = images.length - pending.length;
  onProgress(done_, images.length);

  const settled = Promise.all(pending.map((img) => new Promise((done) => {
    const tick = () => { onProgress(++done_, images.length); done(); };
    img.addEventListener("load", tick, { once: true });
    img.addEventListener("error", tick, { once: true });
  })));
  // A publisher screenshot that never arrives must not hold the export open
  // for ever; the caption above owns up to whatever is missing.
  const capped = new Promise((done) => setTimeout(done, timeout));

  return Promise.race([settled, capped])
    .then(() => images.filter((img) => !img.complete || !img.naturalWidth).length);
}

/* ── Misc ───────────────────────────────────────────────── */

$("startOver").addEventListener("click", () => {
  currentReport = null;
  openKeys.clear();
  fileInput.value = "";
  show("upload");
});

function show(name) {
  Object.entries(panels).forEach(([key, el]) => (el.hidden = key !== name));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showError(message) {
  const el = $("uploadError");
  el.textContent = message;
  el.hidden = false;
}
function hideError() { $("uploadError").hidden = true; }

function colorFor(verdict) {
  return {
    supported: "#16a34a",
    related: "#0284c7",
    weak: "#d97706",
    unrelated: "#dc2626",
    not_found: "#b91c1c",
    unverified: "#94a3b8",
  }[verdict] || "#94a3b8";
}

function cap(text) {
  text = String(text || "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function truncate(text, n) {
  text = (text || "").replace(/\s+/g, " ").trim();
  return text.length > n ? text.slice(0, n - 1) + "…" : text;
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
