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
  claude: "Claude",
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
  body.append("use_claude", $("useClaude").checked && !$("useClaude").disabled ? "1" : "0");
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
  show("results");

  const s = report.stats || {};
  $("resultTitle").textContent = report.paper_title || report.source_pdf;
  $("resultMeta").textContent = [
    `${s.pages ?? "?"} pages`,
    `${s.references_checked ?? 0} of ${s.references_cited ?? 0} cited references checked`,
    s.claims_judged ? `${s.claims_judged} individual claims judged` : null,
    `judged by ${ENGINE_LABEL[s.engine] || s.engine || "lexical overlap"}`,
    `${s.elapsed_seconds ?? "?"}s`,
  ].filter(Boolean).join(" · ");

  renderRisk(s.risk);
  renderTally(s);
  renderWarnings(report.warnings);
  renderFilters(s.verdicts || {}, report.references || []);
  renderCards();
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
  if (stats.corrected) {
    tiles.push(`<div class="tile weak"><span class="n">${stats.corrected}</span>
                  <span class="k">Corrected</span></div>`);
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
  else if ((entry.source?.integrity || []).length) score = Math.max(score, 3.5);
  return score;
}

function renderCards() {
  const list = (currentReport.references || [])
    .filter((r) => activeFilter === "all" || r.verdict === activeFilter)
    .sort((a, b) => urgency(b) - urgency(a));

  $("cards").innerHTML = list.map(cardHtml).join("") ||
    `<p class="status">Nothing in this category.</p>`;

  $("cards").querySelectorAll(".card-head").forEach((head) => {
    head.addEventListener("click", () => head.parentElement.classList.toggle("open"));
  });
  $("cards").querySelectorAll(".shot img").forEach((img) => {
    img.addEventListener("click", (e) => {
      e.stopPropagation();
      openLightbox(img.src, img.dataset.caption || "");
    });
  });
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
  ].join("");

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

  const integrity = (src.integrity || []).length
    ? `<div class="section"><h4>Published notices on this work</h4>
        <ul class="meta-list">${src.integrity
          .map((i) => `<li><strong>${esc(cap(i.kind))}</strong>${i.date ? ` (${esc(i.date)})` : ""}${
            i.doi ? ` — ${esc(i.doi)}` : ""} <span class="sub">via ${esc(i.source)}</span></li>`)
          .join("")}</ul></div>`
    : "";

  const shotBlocks = [];
  if (entry.citing_shot) {
    shotBlocks.push(shotHtml(entry.citing_shot,
      `In your paper${entry.citing_page ? ` — page ${entry.citing_page}` : ""}`,
      entry.claim || "", `${paperUrl}#page=${entry.citing_page || 1}`));
  }
  if (shots.header) {
    shotBlocks.push(shotHtml(shots.header, "Top of the source page", shots.page_title || title));
  }
  if (shots.evidence) {
    shotBlocks.push(shotHtml(shots.evidence,
      shots.evidence_is_card ? "Indexed abstract (page was not capturable)"
                             : "Matching passage, highlighted",
      shots.matched_text || ""));
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
  <article class="card">
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
      </div>
      ${flagsHtml}
      ${claimsHtml}
      ${integrity}
      ${shotsHtml}
      ${meta}
    </div>
  </article>`;
}

function claimHtml(claim, paperUrl) {
  const verdict = claim.verdict || "unverified";
  const page = claim.page
    ? `<span class="where"><a href="${paperUrl}#page=${claim.page}" target="_blank" rel="noopener">page ${claim.page} ↗</a></span>`
    : "";
  return `
    <div class="claim ${esc(verdict)}">
      <div class="claim-top">
        <span class="badge ${esc(verdict)}">${esc(VERDICT_LABEL[verdict] || verdict)}</span>
        ${page}
      </div>
      <blockquote>${esc(claim.claim)}</blockquote>
      <p class="why">${esc(claim.reason || "")}</p>
      ${claim.evidence_quote
        ? `<p class="verbatim">“${esc(truncate(claim.evidence_quote, 400))}”</p>` : ""}
    </div>`;
}

function shotHtml(filename, caption, alt, linkUrl) {
  const url = `/runs/${currentRun}/shots/${encodeURIComponent(filename)}`;
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

/* ── Misc ───────────────────────────────────────────────── */

$("startOver").addEventListener("click", () => {
  currentReport = null;
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
