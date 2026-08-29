---
title: CiteCheck
emoji: 🔍
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Upload a paper, verify what its references actually say.
---

# CiteCheck

Upload a paper. For every reference, CiteCheck confirms the cited work actually
exists, checks whether it has been retracted, finds the sentences that cite it,
decides whether the source really says what each one claims, and screenshots the
evidence.

```bash
python app.py
```

Then open <http://127.0.0.1:5000>.

First run:

```bash
pip install -r requirements.txt
```

Drop a PDF on the page — up to 60 MB — and the run streams its progress as it
goes. **Advanced settings** on the upload panel control the four things worth
changing:

| Setting | Default | What it does |
|---|---|---|
| References to check | 250 | Stops after the first *n* cited references (1–500). |
| Parallel workers | 4 | How many references are checked at once (1–8). Faster, and harder on publisher sites. |
| Judge relevance with an AI model | on, when a key is set | Off scores the run lexically and costs nothing. |
| Capture evidence screenshots | on, when a browser starts | Off skips the three captures and is much quicker. |

Both toggles are *disabled* rather than merely unchecked when the thing behind
them is unavailable, and the header says which — a run that quietly fell back to
lexical scoring because a key was missing is the failure nobody notices.

## What it does

For each numbered reference in an uploaded PDF:

1. **Finds the citing sentences.** Handles `[1]`, `[1, 2]`, `[1-4]` and
   author–year styles, expanding ranges and recording the page. Each marker is
   then cut down to the clause it actually governs — see
   [Per-claim judging](#per-claim-judging) — and bracketed numbers that cite
   nothing (a value range, a table's row labels) are reported apart from real
   unmatched markers rather than counted as citations.
   A paper that numbers its bibliography but cites it as "(Bosona, 2020)" is
   still matched — author-year keys are indexed onto the numbered entries, and
   an alias that two entries both answer to is dropped rather than guessed. Where
   both marker styles appear the majority wins, so a summary table with a
   numeric "Study ID" column cannot outvote every real citation in an
   author-year review.
2. **Resolves the reference to a real source** — arXiv ID, DOI or bare URL from
   the entry itself, else a Crossref bibliographic lookup, cross-checked against
   OpenAlex, Semantic Scholar, Unpaywall and Europe PMC.
3. **Confirms the work exists.** Every index that answers is recorded. A
   reference nothing has heard of is reported as `not_found` — see
   [Does the work exist?](#does-the-work-exist) below.
4. **Checks for retractions and corrections** published against the cited work.
5. **Retrieves the content.** Open-access full text where possible — including
   machine-readable JATS XML from Europe PMC — otherwise the indexed abstract,
   which is always included in what gets judged so a paywalled source is still
   assessed on its real content rather than on a login page.
6. **Judges each claim separately** against the source and explains every
   verdict, re-reading the abstract before any harsh one stands. See
   [Per-claim judging](#per-claim-judging).
7. **Captures three pieces of evidence:**
   - **In your paper** — the citing sentence highlighted on its own page, so a
     questionable citation can be found instantly. Every page number in the
     report also deep-links into your PDF.
   - **Top of the source** — title, journal, authors, confirming the link
     resolved to the right work.
   - **The matching passage**, highlighted in place.

It also runs two checks that need no network access:

- **Author mismatch.** If the prose says "Pinto et al. [29]" but entry 29 is by
  Kim et al., the numbering has slipped. Flagged as high severity.
- **Duplicate entries.** The same work listed twice under different numbers.

The report opens with a screening judgement — `critical`, `concern`, `review` or
`clear` — built from findings you can check, not from a weighted score. An
opaque number out of 100 invites arguments nobody can settle.

## Reading the report

One card per reference, worst first. Everything above the cards is derived from
them, so a re-check or a hand-set verdict moves the banner, the tally and the
filters the instant it moves the card.

**The screening banner** is the top line — `critical`, `concern`, `review` or
`clear`, with the findings that put it there. It counts *citations* rather than
reference headlines, and only what is still outstanding: a finding somebody has
opened, read and ruled on leaves the banner and stays on its own card. A run that
checked nothing never reads `clear`; it says which stage came up empty.

**The tally** counts two different things and labels both. The big number on a
tile is citations, because a citation is what was judged; beneath it is how many
cards that section holds. The two disagree constantly and legitimately — a
reference cited five times that supports four claims and cannot settle the fifth
is one card and five judgements.

**The filters** follow from that. A section holds every reference carrying at
least one citation of that verdict, not the references headlining as it, so the
sections overlap on purpose and sum to more than the number of references. The
reference described above belongs under *Supported* and under *Unverified*, and
hiding it from either is worse than the double count. Inside an open card, the citations that
put it in the section you are filtering on are marked.

**Sorting is by what most needs a look** — the most concerning citation a card
holds, with a retracted source and a high-severity flag outranking the verdict —
never by reference number. A card whose citations disagree is badged **Mixed**
rather than wearing one of their verdicts, with a pip per verdict beside it.

Every page number deep-links into the uploaded PDF, and any screenshot opens
full-size in a lightbox.

### Exporting the report

**Export as PDF** prints the page: the report is already a document and the
browser's own "Save as PDF" produces a file you can hand on. Every card is opened
for the print and put back exactly as you had it, and the lazy screenshots are
all fetched first — the button counts them off, because on a long report that
takes tens of seconds and a silent one reads as a hang.

The exported file says what it contains: the filter it was taken under, the time,
and how many screenshots failed to load and print blank. A filtered export is a
legitimate thing to send someone — "here are the five that don't check out" — but
only if it admits it is a subset. On paper the controls disappear and the
provenance stays: a verdict that came from a re-check, or from your own reading,
is still marked as such, because whoever receives the PDF was not in the room
when that call was made.

## Verdicts

| Verdict | Meaning |
|---|---|
| `supported` | The source directly backs the claim. |
| `related` | Same topic and consistent, but does not state the claim. |
| `weak` | Only loosely connected. |
| `unrelated` | The source is about something else. |
| `unverified` | Not enough retrievable text to judge either way. |
| `not_found` | No bibliographic index has any record of this reference. |

`unrelated` is a serious accusation, so **only the model tier may return it**.
Word overlap can show that two texts *do* discuss the same thing, but low
overlap is not evidence of a mismatch — a sentence like "in the early stages
they had military purposes [1]" shares almost no vocabulary with the abstract
that supports it. Without a model key, the floor is `unverified`.

## Does the work exist?

Every reference is looked up in Crossref, OpenAlex, Semantic Scholar, arXiv and
Europe PMC, and each index's answer is recorded separately as a hit, a clean
miss, or a transport failure. That last distinction matters: an index being
unreachable must never be mistaken for the index saying "no".

Two thresholds govern the outcome, and the gap between them is deliberate:

| Best agreement with the printed title | Result |
|---|---|
| ≥ 0.55 | `confirmed` |
| 0.35 – 0.55 | not confirmed, **no accusation** — flagged for a human to check |
| < 0.35, with at least one index answering | `not_found` |

Wrongly accusing an author of fabricating a citation is the worst mistake this
tool can make, so a real paper that merely indexes badly lands in the silent
middle band rather than being called imaginary.

Two further safeguards:

- **Title matching is corroborated** by first-author surname and publication
  year, and every candidate an index returns is scored — not just the top row.
  Crossref ranks by its own relevance score, which is frequently not the best
  title match, and taking row 0 on faith makes real references look unfindable.
- **Confirmation is anchored to what the citing paper actually printed.** When
  no identifier is given, a title search may write a wrong DOI into the record;
  every later index is then asked about *that* DOI and happily confirms it. The
  result is three indices apparently agreeing on a paper the author never cited.
  A weak search result therefore cannot anchor anything downstream.

A well-formed DOI or arXiv ID that is registered nowhere is treated as stronger
evidence than an unmatched title: identifiers are issued, not guessed.

## Retractions and corrections

Read from Crossref (`updated-by`), OpenAlex (`is_retracted`) and Europe PMC.
Retractions, withdrawals and removals raise a **high-severity** flag, and the
notices behind it — kind, date and DOI — are listed on the reference.

Corrections, errata and expressions of concern arrive through the same list and
are kept in the JSON, but they are deliberately **not** flagged: an amended paper
says the cited work was corrected, not that the citation misrepresents it, and
the latter is the only question this report answers. They appear as a note on the
reference rather than as a finding against it.

A retracted source cannot support the claim made about it, and citing one
uncritically is a finding in its own right — so retracted references sort to the
top of the report regardless of how well they otherwise match the claim.

## Per-claim judging

A reference cited in five places is making five different claims, and the cited
work may back some and not others. Judging them together — as one concatenated
blob — returns a single verdict that is right about none of them and quietly
hides the claim that was oversold.

So each citing sentence is judged on its own, and the report keeps every
per-claim verdict with the page it was made on.

### The claim is the clause, not the sentence

One sentence often makes several claims on several sources:

> parameters such as soil moisture **[37]**, field temperature **[38]** and crop
> yield **[39]** can all be predicted

Each marker answers only for its own clause. Handing the whole sentence to all
three asks each source to support the other two's content, and all three come
back "weak" for failing to — a false finding on every one of them. So each
marker is cut down to the span it governs (the last one keeps the trailing
predicate the list depends on), and the full sentence rides along as *context*
so the model can see what the clause depends on without holding this source
answerable for the rest of it. A clause too thin to stand alone falls back to
the whole sentence: half a claim is worse evidence than a shared one.

Where several references are cited at one point — `[23-28]` — the judge is told
how many, because a group citation asks each source for part of what the
sentence says rather than all of it.

**Prose wins over table rows.** A reference cited both from a comparison table
and from three sentences is asserting those three things and nothing in the
table, so where it is cited from both, the sentences are what get judged. A
marker in a table row is still recorded — a reference cited only from a table is
still a reference worth checking — but scoring a source against
"Ref. Method Year Dataset" returns a confident verdict about nothing at all.

### Harsh verdicts get a second reading

`unrelated` and `weak` are accusations: they say the author cited something that
does not say what they claimed. The commonest way to reach one wrongly is to
judge against text that buries or misses the abstract — a publisher landing
page, a bot-check stub, or forty pages of body text in which the one relevant
paragraph never rose to the top.

So the abstract is always passed to the model under a heading of its own rather
than left to compete for attention inside the body text, and any `unrelated` or
`weak` verdict is re-judged against that abstract before it is reported. If the
second reading is kinder, it wins, and the report says the verdict was
reconsidered and why.

### Rolling up to one headline

The headline verdict is a roll-up, and the two tiers roll up differently on
purpose:

- The **model tier** takes the *most concerning* per-claim verdict. When a model
  that has read both texts says a claim is unsupported, that is evidence, and
  evidence about one claim is not cancelled by four others.
- The **lexical tier** keeps a best-case headline. Low word overlap is not
  evidence of anything, so letting the weakest sentence set the headline would
  make a reference look worse the more often it is cited, for no real reason.

`max_claims_per_reference` (default 6) bounds the model spend on a reference
cited a dozen times.

## Re-checking one reference

Every reference card carries a **Check this one again** panel, because a verdict
is a prompt to look and looking sometimes says the tool got it wrong. Re-running
the whole paper to settle one reference costs minutes and re-does two hundred
checks that were already right.

- **Re-run the check** resolves, fetches, judges and screenshots that one
  reference again. This is the answer to a publisher that was down, or a host
  that stalled the run out of its time budget.
- **Judge against a file…** takes a PDF or `.txt` you supply and judges the
  citation against *that*. Resolution and fetching are skipped outright: you
  have said which document this reference is, which is stronger evidence than
  any bibliographic search.
- **Set this verdict myself** takes your own verdict from a dropdown. The tool
  screens; you decide. Once you have opened the source and read it, your
  judgement beats anything here.

Each citation inside a card carries the same dropdown of its own, under
*Each place it's cited, judged separately*. A reference cited five times holds
five judgements, and a reader who has just read the source usually disagrees
with one of them rather than all five — overruling the whole reference to fix
one throws away four verdicts that were right.

Editing a citation re-derives the card's headline from the claims beneath it,
**the way the engine that produced them would have**: worst-case for the model
tier, best-case for lexical (see [Rolling up](#rolling-up-to-one-headline)).
Applying one tier's rule to the other's reference would mean improving a claim
made the card look worse — in front of the person who had just improved it. The
status line always says where the headline ended up, because it does not always
follow the verdict you just set.

What the indices said — that the work exists, that it has or has not been
retracted — is carried over untouched when you supply a file, because none of it
was re-tested. Only the content verdict is replaced.

### A hand-set verdict is always labelled as one

These reports get handed to other people, so a verdict you set yourself must
never be readable as something the tool established. It is marked **Your
verdict** on the card, the reason line says it was set by hand and which verdict
it replaced, and the screening banner says so too — *even when the result is
`clear`*, because a report that reads clean because someone marked it clean is a
different document, and whoever receives the PDF was not in the room when that
call was made.

What the tool found is never destroyed, only displaced: `machine_verdict` and
`machine_reason` keep the original, the card shows both side by side, and
**Use the tool's verdict again** restores it exactly.

Re-checking a reference *clears* a hand-set verdict on it, and says so. Your
verdict was a reading of evidence the re-check has just replaced, and carrying
it forward would attach your name to a reading of something you never saw.

The run's tally, screening banner and warnings are all recomputed from the
entries, so a re-check or a hand-set verdict that clears a finding clears it
from the headline too — and the exported PDF records which verdicts came from a
re-check and which from you.

## Judging engine

Set the key — in a `.env` file next to `app.py` (copy `.env.example`), or as a
normal environment variable.

```ini
OPENAI_API_KEY=sk-...
```

| Variable | Default | Notes |
|---|---|---|
| `CITECHECK_LLM` | auto | `off` skips the model entirely and scores lexically. |
| `CITECHECK_OPENAI_MODEL` | `gpt-4o` | Any model supporting structured outputs. |
| `OPENAI_BASE_URL` | — | For Azure or any OpenAI-compatible gateway. |
| `CITECHECK_CONTACT_EMAIL` | — | Your email. Not a secret — see below. |
| `CITECHECK_PASSWORD` / `_USERNAME` | — | One shared password in front of every route. See [beyond localhost](#a-shared-password). |
| `CITECHECK_NO_SANDBOX` | — | `1` drops Chromium's sandbox, which a container needs. See [in a container](#in-a-container). |

With no key, it falls back to lexical scoring. That still finds and screenshots
everything, but its verdicts are much weaker — this is the single biggest lever
on output quality.

A key that is *set but rejected* falls back the same way, per reference. The
report distinguishes the two cases: its header names the engine that actually
returned verdicts and how many references it judged, so a run where every call
failed reads "judged by lexical overlap", never "judged by OpenAI".

Sampling is pinned off — temperature 0, fixed seed — because judging the same
citation twice has to give the same answer, and at the default temperature it
does not: the identical claim against the identical text comes back `related` on
one call and `weak` on the next, which to anyone watching is the tool changing
its mind for no reason they can see. A model that rejects those parameters
outright (reasoning models do) falls back to a plain call: losing determinism is
bad, failing the reference over it is worse.

`.env` is gitignored. Never commit a key. Values already in the environment beat
the file, so an exported key is never overridden by a stale one.

### Why set a contact email

`CITECHECK_CONTACT_EMAIL` is not a secret and is worth setting anyway:

- Crossref and OpenAlex put callers who identify themselves on a **faster, more
  reliable pool**.
- **Unpaywall refuses to answer without it.** Unpaywall has the broadest
  open-access coverage of any source here, so leaving this blank means more
  references come back abstract-only.

The startup banner says which mode you are in, so this can't fail silently.

## Running it beyond localhost

### A shared password

Left unset, the app is open. That is right for `127.0.0.1` and wrong for anywhere
a stranger can reach, because every upload spends real money against the model
key above.

```ini
CITECHECK_PASSWORD=something-long
CITECHECK_USERNAME=client
```

With a password set, every route asks for HTTP basic auth against it, compared in
constant time — a plain `==` leaks the password one character at a time to anyone
willing to measure. Nothing here is per-user: it exists so a link can be handed
to someone who has no account, not to identify who is on the other end.

### In a container

`Dockerfile` builds the whole thing — Python 3.12 on Debian bookworm, Chromium
installed at build time into `/ms-playwright` so an unprivileged runtime user can
still launch it, gunicorn on `$PORT` (7860). It exists for a hosted demo, and the
front matter at the top of this file is the Hugging Face Spaces configuration
that goes with it. Delete both to undo the deployment; nothing else depends on
them.

| Variable | Why |
|---|---|
| `CITECHECK_NO_SANDBOX=1` | A container grants Chromium neither the `SYS_ADMIN` its own sandbox needs nor a `/dev/shm` big enough to render a page, so it dies on launch and every screenshot silently degrades. Set in the image; opt-in everywhere else, because both protections are worth keeping locally. |
| `PORT` | What gunicorn binds. Defaults to 7860. |

One worker, sixteen threads, no request timeout. That is not tuning: in-flight
runs are per-process state, so a second worker would strand an upload on one
process and its progress stream on another — the client would watch a run that
never moves — and the progress stream stays open for the whole run, which
gunicorn would otherwise reap.

## Screenshots

Uses Playwright. It tries its own Chromium first, then falls back to an
installed Chrome or Edge, so **no browser download is usually needed**. For the
bundled build:

```bash
python -m playwright install chromium
```

PDFs are highlighted with PyMuPDF rather than a browser PDF viewer — matching is
done over word boxes, so it survives line wraps, column breaks and hyphenation.

CAPTCHAs and consent walls are **not** bypassed.

Several publishers (IEEE, SAGE, AIAA) serve a bot check instead of the article,
so there is genuinely nothing on the page to highlight. Where that happens and
an abstract was retrieved from an index, the evidence panel shows an **abstract
card** instead: the indexed abstract with the matched sentence highlighted,
captioned *"INDEXED ABSTRACT — not a screenshot of the publisher page"* so it can
never be mistaken for a page capture. The JSON marks these with
`shots.evidence_is_card: true`.

## Output

Everything lands in `runs/<run-id>/`:

- `report.json` — the full structured result
- `shots/<key>_citing.png` — the citing sentence in your paper (blue highlight)
- `shots/<key>_header.png` — top of the cited source
- `shots/<key>_evidence.png` — matching passage, or an abstract card

The JSON is the durable artefact; the web page is a view over it. Fields worth
knowing per reference:

| Field | Meaning |
|---|---|
| `claim_verdicts[]` | Every citing sentence with its own verdict, reason, quote and page. |
| `source.existence` | `confirmed` / `unconfirmed` / `not_found`. |
| `source.indices_hit` / `_missed` / `_errored` | Which indices confirmed, denied, or failed to answer. |
| `source.retracted`, `source.integrity[]` | Retraction and correction notices. |
| `source.claimed_title`, `source.best_agreement` | What the entry printed, and how well anything matched it. |
| `claim_tally` | How many of this reference's citations came to each verdict. |
| `flags[]` | `author-mismatch`, `duplicate-entry`, `retracted-source`, `reference-not-found`, each with a severity. |
| `citing_shot`, `citing_page` | The capture of the citing sentence inside your own paper, and the page it is on. |
| `rechecked` | When this reference — or one citation of it — was last re-checked, against what, and what that displaced. |
| `reviewed`, `machine` | A verdict set by hand, and what the tool concluded before it. `machine` disappears again when the hand-set verdict is cleared, so undo is lossless. |
| `timed_out` | Set when the run's clock stopped this reference mid-check. |

And per run:

| Field | Meaning |
|---|---|
| `stats.risk` | The screening judgement shown at the top of the report. |
| `stats.verdicts` / `claim_verdicts` | Reference headlines, and the citations beneath them. The two are different questions and are never folded together. |
| `stats.references_with` | How many references carry at least one citation of each verdict — what the filters count. |
| `stats.engine`, `engine_note` | Which engine actually returned verdicts, and how many references it judged. |
| `stats.flagged_open`, `retracted_open` | Findings nobody has ruled on yet — what the banner reads. The un-suffixed totals stay put. |
| `warnings` / `base_warnings` | What is still outstanding, and the run-level complaints it was derived from. |

## The HTTP API

The page is a client of these; nothing is private to it.

| Route | Does |
|---|---|
| `GET /api/capabilities` | Which judging engine is configured, whether a browser could be launched, the upload limit. |
| `POST /api/upload` | Takes `pdf` plus the settings above, starts the run, returns its `run_id`. |
| `GET /api/stream/<run_id>` | Server-sent progress for that run. The event log is replayed by index, so a client that connects late — or a second one watching alongside — sees every event exactly once. |
| `GET /api/report/<run_id>` | The finished `report.json`, with its tally, banner and warnings re-derived on the way out, so a report written by older code is never served with a summary that disagrees with its own cards. |
| `POST /api/recheck/<run_id>` | Re-runs one reference: `key`, optionally `claim_index` to narrow it to a single citation, optionally a `source` file to judge against instead. |
| `POST /api/verdict/<run_id>` | Records your own verdict on a reference or one of its citations, or clears one. |
| `GET /api/paper/<run_id>` | The uploaded PDF, so page links open at the right page. |
| `GET /runs/<run_id>/shots/<file>` | One captured screenshot. |

Both write routes are serialised against each other: each reads `report.json`,
replaces one entry and writes it back, so two at once would lose whichever
finished first. They reply with the changed entry and the re-derived tally and
warnings — never the whole report, which on a 150-reference run is about two
megabytes the client already holds. Run ids are generated and pattern-checked on
the way in; anything else addressing a run directory is a path-traversal attempt.

## Limits worth knowing

- **Paywalls dominate.** On a typical IEEE/Elsevier/Springer bibliography,
  most sources return only an abstract. Verdicts are made on that, and the
  report says so per reference. Setting `CITECHECK_CONTACT_EMAIL` measurably
  reduces how often this happens.
- **Reference parsing is heuristic.** APA, Harvard, Vancouver, IEEE, ACM,
  Nature, Chicago, MLA and the Elsevier/Springer numbered styles are covered by
  tests, in every author order — "Surname, A.B.", "A.B. Surname", "AB Surname"
  and spelled-out given names — along with accented and double-barrelled
  surnames, two-column layouts, and identifiers split across line breaks. But
  bibliographies are endlessly inventive, so when parsing does come up empty the
  report says so instead of reporting a clean result: a run that checked nothing
  is never shown as "No integrity problems found".
- **`not_found` is deliberately conservative.** A fabricated reference with a
  plausible title may land in `unconfirmed` rather than `not_found`. That is the
  intended trade: the report says it could not be matched and asks you to check,
  rather than risk accusing an author of inventing a real paper.
- **A run has a clock.** A publisher that accepts a connection and then sends
  nothing can wedge a worker indefinitely, and a thread blocked in a socket or a
  browser call cannot be interrupted. So the checking phase has a wall-clock
  ceiling — three times the up-front estimate, at least three minutes — and
  anything still running when it expires is reported `unverified`, saying which
  step it was on and against which host. Never `not_found`: that would accuse an
  author of citing something imaginary on the evidence of a slow server. Those
  references re-check individually in seconds.
- **A verdict is a prompt to look, not a judgement.** `unrelated` means read it
  yourself. The screenshots exist so that takes seconds, and when the tool got
  it wrong you can re-check that one reference — against your own copy of the
  source if you have it. See [Re-checking one reference](#re-checking-one-reference).

## Tests

No dependencies beyond the app's own, and no network:

```bash
python -m unittest discover -s tests -t .
```

176 tests, and none of them reaches the network — every source, report and
bibliography they need is written out in the file.

`tests/test_styles.py` is the format-coverage guarantee: every bibliography and
in-text marker style is spelled out there, so it runs on any checkout.
`tests/test_corpus.py` re-parses the real papers that have broken the parser
before; those PDFs are published articles and are not committed, so it skips
whatever is missing. See `tests/corpus/README.md` to populate it.

`tests/test_stability.py` is the one worth knowing about. Every test in it is a
number or a verdict that once moved when nothing about the citation had — the
tally counting references while the cards counted citations, a claim the model
failed to answer on being dropped from the roll-up, a re-check that retrieved
nothing overwriting a verdict reached on text that had been retrieved. A tool
that changes its mind for reasons the reader cannot see is not one they can hand
to anybody.

## Layout

```
app.py                 Flask server, upload + SSE progress
citecheck/
  pdf_parse.py         PDF -> clean text, body/bibliography split
  intext.py            citation markers + the clause each one governs
  refs.py              bibliography -> structured entries
  resolve.py           entry -> URL, metadata, existence + retraction evidence
  fetch.py             retrieve source text (PDF/HTML/JATS), guarantee the abstract
  match.py             lexical scoring + per-claim model judging
  crosscheck.py        author-mismatch, duplicate, retraction and existence flags
  shots.py             header + highlighted-evidence screenshots
  pipeline.py          orchestration, screening risk summary, single-reference re-check
tests/
  test_styles.py       bibliography + marker styles, self-contained
  test_corpus.py       real papers that have broken the parser
  test_report.py       the screening headline, incl. the empty-run guard
  test_claims.py       claim scoping, marker precision, re-checking one reference
  test_claim_recheck.py  re-checking one citation and leaving its siblings alone
  test_stability.py    the same paper, checked twice, saying the same thing
```

`IMPLEMENTED.md` is the companion to this file: what each piece of the stack is,
why it was chosen over the alternatives, and the rules the whole thing is built
to obey.
