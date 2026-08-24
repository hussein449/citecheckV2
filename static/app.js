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

/* Everything downstream of `currentReport`, rebuilt from it. */
function refreshResults() {
  refreshSummary();
  renderCards();
}

/* The parts of the page that summarise the whole run: the banner, the tally,
   the filters, the meta line. Separated from the cards because a change to one
   reference moves all of these but only one card — and rebuilding 150 cards to
   show that one of them changed is most of the cost of doing it. */
function refreshSummary() {
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
  renderFilters(s.references_with || s.verdicts || {}, report.references || []);
}

/* Fold a single changed reference into the report the page is holding, without
   the server having to send back the other 149. */
function patchEntry(key, entry, stats, warnings) {
  const list = currentReport.references || [];
  const at = list.findIndex((r) => r.key === key);
  if (at >= 0) list[at] = entry;
  if (stats) currentReport.stats = stats;
  if (warnings) currentReport.warnings = warnings;
}

/* Rebuild whatever a verdict change touched without the page moving under the
   reader.

   Three things used to move it, and all three were unasked for. The summary
   above the list changes height when a tile or a warning appears. A filter
   reset re-sorts the entire list, so a different reference lands where the
   reader was looking. And both paths then smooth-scrolled to the edited card —
   an animation measured in seconds on a long report, for a card that was
   already on screen, because the reader had just clicked a button on it.

   So the card's distance from the top of the viewport is measured before the
   rebuild and restored after it. The list rearranges around the thing being
   edited while that thing stays exactly where the eye already is. */
function keepInPlace(key, rebuild) {
  const card = () => document.querySelector(`#cards .card[data-key="${key}"]`);
  const before = card()?.getBoundingClientRect().top;
  rebuild();
  const after = card()?.getBoundingClientRect().top;
  // Not smooth, and not `scrollIntoView`: this corrects a shift the reader
  // never asked for, so it has to be invisible rather than animated.
  if (before != null && after != null && after !== before) {
    window.scrollBy(0, after - before);
  }
}

/* Re-render one card in place. Its position in the list is deliberately left
   alone even when its verdict changes the sort order: a card that jumps
   somewhere else the instant you edit it is worse than a list that re-sorts on
   the next filter change. */
function replaceCard(key) {
  const card = document.querySelector(`#cards .card[data-key="${key}"]`);
  const entry = (currentReport.references || []).find((r) => r.key === key);
  // No fallback to `renderCards` here: rebuilding the list is what re-sorts it,
  // and re-sorting under a reader who just edited one card is the thing this
  // path exists to avoid. If the card is not on screen there is nothing to
  // replace, and the next filter change will render it wherever it belongs.
  if (!card || !entry) return;
  card.outerHTML = cardHtml(entry);
  bindCards(document.querySelector(`#cards .card[data-key="${key}"]`));
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

/* Two different things are counted here, and conflating them is what made this
   tally read as wrong. A reference's headline is the *most concerning* of the
   citations beneath it, so one cited five times that supports four and oversells
   the fifth headlines as the fifth — its four supported citations are on the
   card the reader is looking at, but a reference-level count never shows them.
   Counting "6 supported" while the cards plainly show more is not a rounding
   difference; it is two different questions. Both are answered, and labelled. */
function renderTally(stats) {
  const refs = stats.verdicts || {};
  const claims = stats.claim_verdicts || {};
  /* A verdict can exist at claim level and nowhere at reference level — three
     supported citations spread across references that each headline as
     something worse. Filtering on the reference count alone drops that tile and
     the citations with it. */
  const holding = stats.references_with || {};
  const tiles = VERDICTS.filter((v) => refs[v] || claims[v]).map((v) => {
    const c = claims[v] || 0;
    const inRefs = holding[v] ?? refs[v] ?? 0;
    /* Leads with the citation count, because a citation is the thing that was
       judged and the thing the sections below are built from. The reference
       count underneath says how many cards to expect in that section — never
       how many "are" that verdict, which was the number that read as wrong. */
    /* "in N references" would read as a contradiction wherever the two numbers
       cross — 31 unverified citations across 38 cards, because a reference the
       tool never got citation-level verdicts for still carries the verdict on
       its headline. So the second number is named as what it is: how many cards
       this section holds, matching its filter button exactly. */
    return `<div class="tile ${v}"><span class="n">${c || refs[v] || 0}</span>
              <span class="k">${esc(VERDICT_LABEL[v])}</span>
              <span class="sub">${
                c ? `${plural(c, "citation")} · ${plural(inRefs, "card")}`
                  : plural(inRefs, "card")
              }</span></div>`;
  });
  /* Retractions never show up as a verdict — a retracted paper can still
     support the claim it was cited for. They need their own tile or they
     vanish from the summary entirely. */
  if (stats.retracted) {
    tiles.unshift(`<div class="tile unrelated"><span class="n">${stats.retracted}</span>
                     <span class="k">Retracted</span>
                     <span class="sub">${plural(stats.retracted, "reference")}</span></div>`);
  }
  /* Retraction is a property of the work, not of any one citation of it, so its
     tile stays a reference count and is left out of the caption below. */
  $("tally").innerHTML = tiles.join("");

  const caption = $("tallyNote");
  if (!caption) return;
  const anySplit = VERDICTS.some((v) => (claims[v] || 0) !== (refs[v] || 0));
  caption.hidden = !tiles.length || !anySplit;
  caption.textContent =
    "Big number counts individual citations. A reference cited several times " +
    "appears in every section its citations fall into — four supported " +
    "citations and one unverified put it under both — so the section counts " +
    "add up to more than the number of references.";
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/* Only what is still outstanding. A finding the reader has ruled on is gone
   from here — it stays on its own reference's card, flags and all, which is
   where somebody goes to see what was decided and why. */
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
  /* Counts the cards each section will actually show — references carrying at
     least one citation of that verdict, not references headlining as it. The
     sections overlap by design, so these sum to more than the "All" count: a
     reference that supports four claims and cannot settle a fifth is genuinely
     in two of them, and hiding it from either is the bug this replaces. */
  const present = ["all", ...VERDICTS.filter((v) => counts[v])];
  $("filters").innerHTML = present
    .map((v) => `<button data-f="${v}" title="${
      v === "all"
        ? "Every reference"
        : `References with at least one citation judged ${VERDICT_LABEL[v]}`
    }">${
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

/* Every verdict a reference carries, not just the one on its headline.

   A reference cited five times holds five judgements and the headline is only
   the most concerning of them, so asking it which section a card belongs in
   gives one answer where there are several — and a reference that supports four
   claims and cannot settle a fifth vanishes from the supported section behind
   its own worst citation. Mirrors `pipeline.verdicts_in`, which counts them. */
function verdictsIn(entry) {
  const claims = entry.claim_verdicts || [];
  const found = new Set(claims.map((c) => c.verdict));
  // Nothing but a headline to go on, or a headline the reader set by hand —
  // which has to be findable under the verdict they chose even where the
  // citations beneath it disagree.
  if (!claims.length || entry.reviewed?.source === "reference") found.add(entry.verdict);
  found.delete(undefined);
  found.delete("");
  return found;
}

/* Most concerning first, so the findings that matter are never below the fold. */
const CONCERN = { not_found: 6, unrelated: 5, weak: 4, unverified: 3, related: 2, supported: 1 };

/* The verdict alone is not the whole story. A retracted source can still be
   "related" to the claim it was cited for — that says nothing about whether it
   should have been cited at all, and ranking on verdict alone buries it under
   references we merely could not read. */
function urgency(entry) {
  /* Ranked on the most concerning citation the card holds, not on its headline.
     The headline is a roll-up, and the lexical tier rolls up to the *best* case
     — so a card holding one unverified citation and one supported one headlines
     "supported" and would sort to the very bottom, below cards with nothing
     wrong with them. What needs a human look is the unverified citation, and it
     is still there whatever the headline says. */
  let score = Math.max(
    0, ...[...verdictsIn(entry)].map((v) => CONCERN[v] || 0)
  );
  if (entry.source?.retracted) score = Math.max(score, 6.5);
  else if ((entry.flags || []).some((f) => f.severity === "high")) score = Math.max(score, 4.5);
  return score;
}

function renderCards() {
  const list = (currentReport.references || [])
    .filter((r) => activeFilter === "all" || verdictsIn(r).has(activeFilter))
    .sort((a, b) => urgency(b) - urgency(a));

  $("cards").innerHTML = list.map(cardHtml).join("") ||
    `<p class="status">Nothing in this category.</p>`;

  bindCards($("cards"));
}

/* Wire up one card, or every card in a container. Split out of `renderCards`
   so a single edited card can be re-rendered and re-bound on its own. */
function bindCards(root) {
  if (!root) return;
  root.querySelectorAll(".card-head").forEach((head) => {
    head.addEventListener("click", () => {
      const card = head.parentElement;
      const open = card.classList.toggle("open");
      if (open) openKeys.add(card.dataset.key);
      else openKeys.delete(card.dataset.key);
    });
  });
  root.querySelectorAll(".shot img").forEach((img) => {
    img.addEventListener("click", (e) => {
      e.stopPropagation();
      openLightbox(img.src, img.dataset.caption || "");
    });
  });
  root.querySelectorAll(".recheck-run").forEach((btn) => {
    btn.addEventListener("click", () => runRecheck(btn.closest(".recheck").dataset.key, null));
  });
  root.querySelectorAll(".review-save").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = btn.closest(".recheck");
      setVerdict(box.dataset.key, { verdict: box.querySelector(".review-pick").value });
    });
  });
  root.querySelectorAll(".review-clear").forEach((btn) => {
    btn.addEventListener("click", () => {
      setVerdict(btn.closest(".recheck").dataset.key, { clear: "1" });
    });
  });
  root.querySelectorAll(".claim-save").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".claim-actions");
      setVerdict(row.dataset.key, {
        claim_index: row.dataset.claim,
        verdict: row.querySelector(".claim-pick").value,
      });
    });
  });
  root.querySelectorAll(".claim-clear").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".claim-actions");
      setVerdict(row.dataset.key, { claim_index: row.dataset.claim, clear: "1" });
    });
  });
  root.querySelectorAll(".claim-recheck").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".claim-actions");
      runRecheck(row.dataset.key, null, Number(row.dataset.claim));
    });
  });
  root.querySelectorAll(".claim-actions input[type=file]").forEach((input) => {
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      // Cleared so picking the same file twice still fires a change event.
      const row = input.closest(".claim-actions");
      input.value = "";
      if (file) runRecheck(row.dataset.key, file, Number(row.dataset.claim));
    });
  });
  root.querySelectorAll(".recheck input[type=file]").forEach((input) => {
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
async function runRecheck(key, file, claimIndex = null) {
  if (recheckInFlight) return;

  /* One citation or the whole reference. The controls live in different places
     — a claim row versus the card's re-check panel — so the busy state and the
     status line follow whichever one was clicked, and a single-citation
     re-check never lights up the card as though all of it were re-running. */
  const scoped = claimIndex != null;
  const box = scoped
    ? document.querySelector(`.claim-actions[data-key="${key}"][data-claim="${claimIndex}"]`)
    : document.querySelector(`.recheck[data-key="${key}"]`);
  const note = box?.querySelector(scoped ? ".claim-status" : ".recheck-status");
  const noteClass = scoped ? "claim-status" : "recheck-status";
  const controls = box ? [...box.querySelectorAll(".btn")] : [];

  recheckInFlight = true;
  openKeys.add(key);
  controls.forEach((el) => el.classList.add("busy"));
  setNote(note, "status working", file
    ? `Judging ${scoped ? "this citation" : "this reference"} against ${file.name}…`
    : scoped
      ? "Looking the source up again and re-reading it for this citation only…"
      : "Looking this reference up again and re-reading the source…", noteClass);

  const body = new FormData();
  body.append("key", key);
  if (scoped) body.append("claim_index", String(claimIndex));
  if (file) body.append("source", file);
  body.append("use_model", $("useModel").checked && !$("useModel").disabled ? "1" : "0");
  body.append("screenshots", $("useShots").checked && !$("useShots").disabled ? "1" : "0");

  try {
    const res = await fetch(`/api/recheck/${currentRun}`, { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Re-check failed (${res.status})`);

    patchEntry(key, data.entry, data.stats, data.warnings);
    keepInPlace(key, () => {
      refreshSummary();
      replaceCard(key);        // the card, and every control on it, is rebuilt
    });
    reportRecheck(key, data.entry, claimIndex);
  } catch (err) {
    setNote(
      document.querySelector(
        scoped
          ? `.claim-actions[data-key="${key}"][data-claim="${claimIndex}"] .claim-status`
          : `.recheck[data-key="${key}"] .recheck-status`
      ),
      "status error", err.message, noteClass
    );
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

    patchEntry(key, data.entry, data.stats, data.warnings);
    openKeys.add(key);

    /* The card is rebuilt where it stands and the list is never re-sorted, so
       editing a verdict cannot move a card to a new position or drop it out
       from under the reader. It may no longer match the filter it is sitting
       in; it stays visible until the next filter change, which is the moment
       the reader is asking to see the list rearranged. */
    keepInPlace(key, () => {
      refreshSummary();
      replaceCard(key);
    });

    const now = VERDICT_LABEL[data.entry.verdict] || data.entry.verdict;
    const after = document.querySelector(`.recheck[data-key="${key}"]`);
    setNote(after?.querySelector(".recheck-status"), "status done", statusFor(fields, data.entry, now));
  } catch (err) {
    setNote(note, "status error", err.message);
  }
}

/* Always says where the card's headline ended up, because that is the thing the
   reader is watching and it does not always follow the verdict they just set:
   editing one citation of five re-derives the headline from all five, so
   marking one "supported" can leave the card sitting at "weak" — correctly, and
   confusingly if nobody says so. */
function statusFor(fields, entry, now) {
  const headline = `The card now reads ${now}.`;
  if (fields.claim_index === undefined) {
    return fields.clear
      ? `Back to the tool's verdict — ${now}.`
      : `Recorded as your verdict. ${headline}`;
  }
  const which = `Citation ${Number(fields.claim_index) + 1}`;
  return fields.clear
    ? `${which} is back to the tool's verdict. ${headline}`
    : `${which} recorded as your verdict. ${headline}`;
}

function setNote(note, className, text, base = "recheck-status") {
  if (!note) return;
  note.hidden = false;
  note.className = `${base} ${className}`;
  note.textContent = text;
}

/* Says what changed, not just that something happened. "Still unverified" is a
   real answer and the reader needs to be told it plainly, or they will keep
   pressing the button expecting a different one.

   Writes the line and nothing else — the reader clicked a button on this card,
   so the card is already in front of them and `keepInPlace` has held it there. */
function reportRecheck(key, entry, claimIndex = null) {
  const scoped = claimIndex != null;
  const box = scoped
    ? document.querySelector(`.claim-actions[data-key="${key}"][data-claim="${claimIndex}"]`)
    : document.querySelector(`.recheck[data-key="${key}"]`);
  if (!box || !entry) return;
  const noteClass = scoped ? "claim-status" : "recheck-status";
  const note = box.querySelector(scoped ? ".claim-status" : ".recheck-status");
  const done = entry.rechecked || {};
  const was = done.previous_verdict;
  const now = VERDICT_LABEL[entry.verdict] || entry.verdict;

  /* The citation moved; the card's headline may not have. Re-checking citation
     three to "supported" can correctly leave a card reading "weak" on the
     strength of citation one, and a reader told only the headline reads that as
     the re-check having done nothing. Both are reported, in that order. */
  if (scoped && done.outcome === "judged") {
    const claim = (entry.claim_verdicts || [])[claimIndex] || {};
    const wasClaim = claim.rechecked?.previous_verdict;
    const nowClaim = VERDICT_LABEL[claim.verdict] || claim.verdict;
    const moved = wasClaim && wasClaim !== claim.verdict
      ? `Citation ${claimIndex + 1}: ${VERDICT_LABEL[wasClaim] || wasClaim} → ${nowClaim}.`
      : `Citation ${claimIndex + 1} re-checked — still ${nowClaim}.`;
    const dropped = claim.rechecked?.cleared_review
      ? " Your own verdict on it was cleared — it judged the evidence this replaced."
      : "";
    setNote(note, "status done",
      `${moved} The card now reads ${now}.${dropped} The other citations were not re-checked.`,
      noteClass);
    return;
  }

  /* The lookup came back empty, so nothing was re-judged and the earlier
     finding stands. Reporting that as a verdict — "still unverified" — is what
     made re-checking look like the tool changing its mind: the reader sees a
     verdict move, or fail to, and has no way to tell that the source simply
     did not load this time. Say which of the two happened. */
  if (done.outcome === "nothing_retrieved") {
    const what = scoped ? `citation ${claimIndex + 1} still reads` : "the verdict is unchanged at";
    const stands = scoped
      ? VERDICT_LABEL[((entry.claim_verdicts || [])[claimIndex] || {}).verdict] || now
      : now;
    setNote(
      note,
      "status warn",
      `Nothing was retrieved this time, so ${what} ${stands}. ` +
        `${done.detail || ""} This is a failed lookup, not a new judgement — ` +
        `if you have the source yourself, judge it against that instead.`,
      noteClass
    );
    return;
  }

  /* Their own verdict was a reading of evidence this re-check has just
     replaced, so it was dropped. Losing someone's conclusion silently is worse
     than losing it. */
  const dropped = done.cleared_review
    ? ` Your own verdict was cleared — it judged the evidence this replaced.`
    : "";
  setNote(
    note,
    "status done",
    (was && was !== entry.verdict
      ? `Re-checked: ${VERDICT_LABEL[was] || was} → ${now}.`
      : `Re-checked — the verdict is still ${now}.`) + dropped,
    noteClass
  );
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

  /* What this reference's citations actually came to, on the head where it is
     readable without opening the card. */
  const perClaim = entry.claim_verdicts || [];
  const spread = VERDICTS
    .map((v) => [v, perClaim.filter((c) => c.verdict === v).length])
    .filter(([, n]) => n);
  const breakdown = perClaim.length > 1
    ? `<div class="card-tally">${spread
        .map(([v, n]) => `<span class="pip ${v}${
          v === activeFilter ? " lit" : ""
        }">${n} ${esc(VERDICT_LABEL[v])}</span>`)
        .join("")}</div>`
    : "";

  /* A card whose citations disagree does not get to wear one of their verdicts
     as though it were the card's answer. Which one it wore depended on the
     engine — the model tier rolls up to the worst citation, the lexical tier to
     the best — so the same mixed card read "Unverified" or "Supported"
     depending on how it was judged, and either way the citations it did not
     name looked like they did not exist. The badge says the citations disagree
     and the pips beside it say how; the roll-up is still on the card, in the
     verdict section, where it is labelled as a summary. */
  const mixed = spread.length > 1 && !entry.reviewed;
  const headBadge = mixed
    ? `<span class="badge mixed" title="This reference is cited ${perClaim.length} times and the citations were judged differently">Mixed</span>`
    : `<span class="badge ${esc(entry.verdict)}">${
        esc(VERDICT_LABEL[entry.verdict] || entry.verdict)}</span>`;

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
         <div class="claims">${claims.map((c, i) => claimHtml(c, paperUrl, entry.key, i)).join("")}</div></div>`
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
      ${headBadge}
      <div class="card-title">
        <h3>${esc(title)}${chips}</h3>
        <div class="sub">${esc(truncate(entry.reason || "", 180))}</div>
        ${breakdown}
      </div>
      <span class="chev">›</span>
    </div>
    <div class="card-body">
      <div class="section"><h4>Verdict</h4>
        ${mixed ? `<p class="rollup-note">Rolled up across ${perClaim.length}
           citations this reads <b>${esc(VERDICT_LABEL[entry.verdict] || entry.verdict)}</b>,
           but they were judged separately and did not agree — each one is listed
           below with its own verdict.</p>` : ""}
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
  const last = entry.rechecked;
  const done = last
    ? `<p class="recheck-was">Last re-checked ${esc(last.at || "")}${
        last.against === "supplied"
          ? ` against <b>${esc(last.filename || "a supplied file")}</b>`
          : " against the citation indexes"}.${
        last.outcome === "nothing_retrieved"
          ? " That re-check retrieved nothing, so the verdict above is the earlier one."
          : ""}</p>`
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

  const standing = !review
    ? ""
    : review.source === "claims"
    /* Derived, not set directly: say so, or the reader sees "your verdict" on a
       headline they never chose and cannot find where they chose it. */
    ? `<p class="review-was">This card reads
         <b>${esc(VERDICT_LABEL[review.verdict] || review.verdict)}</b> because you judged
         ${review.edited_claims} of its citations yourself, above. The tool had it as
         <b>${esc(VERDICT_LABEL[review.machine_verdict] || review.machine_verdict || "—")}</b>.</p>`
    : `<p class="review-was">You set this to <b>${esc(VERDICT_LABEL[review.verdict] || review.verdict)}</b>
         on ${esc(review.at || "")}. The tool had it as
         <b>${esc(VERDICT_LABEL[review.machine_verdict] || review.machine_verdict || "—")}</b>.
         ${review.note ? `<span class="review-note">“${esc(review.note)}”</span>` : ""}</p>`;

  return `
    <div class="review">
      <div class="recheck-actions">
        <select class="review-pick" aria-label="Your verdict for this reference">${options}</select>
        <button type="button" class="btn ghost review-save">Set this verdict myself</button>
        ${review?.source === "reference"
          ? `<button type="button" class="btn ghost review-clear">Use the tool's verdict again</button>` : ""}
      </div>
      <p class="review-hint">Sets the whole card at once, overruling the individual
        citations above. Recorded as yours, not the tool's — on the card, in the
        tally and in the exported PDF.</p>
      ${standing}
    </div>`;
}

function claimHtml(claim, paperUrl, key, index) {
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
  const mine = claim.override
    ? `<span class="revisited yours">your verdict</span>` : "";

  /* Each citation gets its own control, because a reader who has just read the
     source usually disagrees with one of the five judgements on a reference,
     not with all five. Without this the only way to correct one was to overrule
     the whole reference, which throws away four verdicts that were right. */
  const options = VERDICTS.map(
    (v) => `<option value="${v}"${v === verdict ? " selected" : ""}>${esc(VERDICT_LABEL[v])}</option>`
  ).join("");
  const controls = `
    <div class="claim-actions" data-key="${esc(key)}" data-claim="${index}">
      <select class="claim-pick" aria-label="Your verdict for this citation">${options}</select>
      <button type="button" class="btn ghost tiny claim-save">Set this one myself</button>
      ${claim.override
        ? `<button type="button" class="btn ghost tiny claim-clear">Undo</button>` : ""}
      <button type="button" class="btn ghost tiny claim-recheck">Re-check just this one</button>
      <label class="btn ghost tiny claim-pick-file">Judge this one against a file…
        <input type="file" hidden
               accept=".pdf,.txt,.text,.md,application/pdf,text/plain">
      </label>
      <p class="claim-status" hidden></p>
    </div>`;

  /* What the last re-check of *this* citation did. The card carries its own
     "last re-checked" line, but that one is about the reference — and after a
     single-citation re-check it would say the whole thing was re-run, which is
     the misreading this feature exists to remove. */
  const done = claim.rechecked
    ? `<p class="claim-was">Citation re-checked ${esc(claim.rechecked.at || "")}${
        claim.rechecked.against === "supplied"
          ? ` against <b>${esc(claim.rechecked.filename || "a supplied file")}</b>`
          : " against the citation indexes"}.${
        claim.rechecked.cleared_review
          ? " Your own verdict on it was cleared — it judged the evidence this replaced."
          : ""}</p>`
    : "";

  /* Filtered to one verdict, the reader is looking for the citations that put
     this card in that section — which on a card of five may be one of them.
     Marking them is the difference between "this reference is unverified" and
     "this one citation of it is", which is the whole distinction. */
  const lit = activeFilter !== "all" && verdict === activeFilter ? " lit" : "";

  return `
    <div class="claim ${esc(verdict)}${claim.override ? " mine" : ""}${lit}">
      <div class="claim-top">
        <span class="badge ${esc(verdict)}">${esc(VERDICT_LABEL[verdict] || verdict)}</span>
        ${page}${revisited}${mine}
      </div>
      <blockquote>${esc(claim.claim)}</blockquote>
      ${context}
      <p class="why">${esc(claim.reason || "")}</p>
      ${claim.override && claim.machine_verdict
        ? `<p class="claim-machine"><span>The tool said:</span>
             ${esc(VERDICT_LABEL[claim.machine_verdict] || claim.machine_verdict)}
             — ${esc(truncate(claim.machine_reason || "", 260))}</p>` : ""}
      ${claim.evidence_quote
        ? `<p class="verbatim">“${esc(truncate(claim.evidence_quote, 400))}”</p>` : ""}
      ${done}
      ${controls}
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
