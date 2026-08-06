# CiteCheck

Upload a paper. For every reference, CiteCheck confirms the cited work actually
exists, checks whether it has been retracted, finds the sentences that cite it,
decides whether the source really says what each one claims, and screenshots the
evidence.

```bash
python app.py
```

Then open <http://127.0.0.1:5000>.

## What it does

For each numbered reference in an uploaded PDF:

1. **Finds the citing sentences.** Handles `[1]`, `[1, 2]`, `[1-4]` and
   author–year styles, expanding ranges and recording the page.
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
6. **Judges each citing sentence separately** against the source and explains
   every verdict. See [Per-claim judging](#per-claim-judging).
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
Retractions, withdrawals and removals raise a **high-severity** flag; corrections
and expressions of concern raise a medium one, with the notice's date and DOI
listed on the reference.

A retracted source cannot support the claim made about it, and citing one
uncritically is a finding in its own right — so retracted references sort to the
top of the report regardless of how well they otherwise match the claim.

## Per-claim judging

A reference cited in five places is making five different claims, and the cited
work may back some and not others. Judging them together — as one concatenated
blob — returns a single verdict that is right about none of them and quietly
hides the claim that was oversold.

So each citing sentence is judged on its own, and the report keeps every
per-claim verdict with the page it was made on. The headline verdict is a
roll-up, and the two tiers roll up differently on purpose:

- The **model tier** takes the *most concerning* per-claim verdict. When a model
  that has read both texts says a claim is unsupported, that is evidence, and
  evidence about one claim is not cancelled by four others.
- The **lexical tier** keeps a best-case headline. Low word overlap is not
  evidence of anything, so letting the weakest sentence set the headline would
  make a reference look worse the more often it is cited, for no real reason.

`max_claims_per_reference` (default 6) bounds the model spend on a reference
cited a dozen times.

## Judging engine

Set **either** key — in a `.env` file next to `app.py` (copy `.env.example`), or
as a normal environment variable.

```ini
OPENAI_API_KEY=sk-...
# or
ANTHROPIC_API_KEY=sk-ant-...
```

| Variable | Default | Notes |
|---|---|---|
| `CITECHECK_LLM` | auto | `claude`, `openai`, or `off`. Claude wins if both keys are set. |
| `CITECHECK_OPENAI_MODEL` | `gpt-4o` | Any model supporting structured outputs. |
| `CITECHECK_CLAUDE_MODEL` | `claude-opus-5` | |
| `OPENAI_BASE_URL` | — | For Azure or any OpenAI-compatible gateway. |
| `CITECHECK_CONTACT_EMAIL` | — | Your email. Not a secret — see below. |

With no key, it falls back to lexical scoring. That still finds and screenshots
everything, but its verdicts are much weaker — this is the single biggest lever
on output quality.

`.env` is gitignored. Never commit a key.

### Why set a contact email

`CITECHECK_CONTACT_EMAIL` is not a secret and is worth setting anyway:

- Crossref and OpenAlex put callers who identify themselves on a **faster, more
  reliable pool**.
- **Unpaywall refuses to answer without it.** Unpaywall has the broadest
  open-access coverage of any source here, so leaving this blank means more
  references come back abstract-only.

The startup banner says which mode you are in, so this can't fail silently.

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
| `stats.risk` | The screening judgement shown at the top of the report. |

## Limits worth knowing

- **Paywalls dominate.** On a typical IEEE/Elsevier/Springer bibliography,
  most sources return only an abstract. Verdicts are made on that, and the
  report says so per reference. Setting `CITECHECK_CONTACT_EMAIL` measurably
  reduces how often this happens.
- **Reference parsing is heuristic.** It handles the common numeric and
  author–year styles, both "Surname, A.B." and "A.B. Surname", accented names,
  and identifiers split across line breaks — but bibliographies are endlessly
  inventive.
- **`not_found` is deliberately conservative.** A fabricated reference with a
  plausible title may land in `unconfirmed` rather than `not_found`. That is the
  intended trade: the report says it could not be matched and asks you to check,
  rather than risk accusing an author of inventing a real paper.
- **A verdict is a prompt to look, not a judgement.** `unrelated` means read it
  yourself. The screenshots exist so that takes seconds.

## Layout

```
app.py                 Flask server, upload + SSE progress
citecheck/
  pdf_parse.py         PDF -> clean text, body/bibliography split
  intext.py            citation markers + the sentence around them
  refs.py              bibliography -> structured entries
  resolve.py           entry -> URL, metadata, existence + retraction evidence
  fetch.py             retrieve source text (PDF/HTML/JATS), guarantee the abstract
  match.py             lexical scoring + per-claim Claude/OpenAI judging
  crosscheck.py        author-mismatch, duplicate, retraction and existence flags
  shots.py             header + highlighted-evidence screenshots
  pipeline.py          orchestration + screening risk summary
```
