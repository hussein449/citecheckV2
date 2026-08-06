# CiteCheck

Upload a paper. For every reference, CiteCheck finds the sentence that cites it,
fetches the cited source, decides whether that source actually says what it was
cited for, and screenshots the evidence.

```bash
python app.py
```

Then open <http://127.0.0.1:5000>.

## What it does

For each numbered reference in an uploaded PDF:

1. **Finds the citing sentences.** Handles `[1]`, `[1, 2]`, `[1-4]` and
   author–year styles, expanding ranges and recording the page.
2. **Resolves the reference to a real source** — arXiv ID, DOI or bare URL from
   the entry itself, else a Crossref bibliographic lookup, enriched with
   OpenAlex and Semantic Scholar for an open-access mirror.
3. **Retrieves the content.** Open-access PDF or HTML where possible. The
   indexed abstract is always retrieved as well and always included in what
   gets judged, so a paywalled source is still assessed on its real content
   rather than on a login page.
4. **Judges the claim** against the source and explains the verdict.
5. **Captures three pieces of evidence:**
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

## Verdicts

| Verdict | Meaning |
|---|---|
| `supported` | The source directly backs the claim. |
| `related` | Same topic and consistent, but does not state the claim. |
| `weak` | Only loosely connected. |
| `unrelated` | The source is about something else. |
| `unverified` | Not enough retrievable text to judge either way. |

`unrelated` is a serious accusation, so **only the model tier may return it**.
Word overlap can show that two texts *do* discuss the same thing, but low
overlap is not evidence of a mismatch — a sentence like "in the early stages
they had military purposes [1]" shares almost no vocabulary with the abstract
that supports it. Without a model key, the floor is `unverified`.

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
| `CITECHECK_OPENAI_MODEL` | `gpt-4.1` | Any model supporting structured outputs. |
| `CITECHECK_CLAUDE_MODEL` | `claude-opus-5` | |
| `OPENAI_BASE_URL` | — | For Azure or any OpenAI-compatible gateway. |

With no key, it falls back to lexical scoring. That still finds and screenshots
everything, but its verdicts are much weaker — this is the single biggest lever
on output quality.

`.env` is gitignored. Never commit a key.

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

The JSON is the durable artefact; the web page is a view over it.

## Limits worth knowing

- **Paywalls dominate.** On a typical IEEE/Elsevier/Springer bibliography,
  most sources return only an abstract. Verdicts are made on that, and the
  report says so per reference.
- **Reference parsing is heuristic.** It handles the common numeric and
  author–year styles, both "Surname, A.B." and "A.B. Surname", accented names,
  and identifiers split across line breaks — but bibliographies are endlessly
  inventive.
- **A verdict is a prompt to look, not a judgement.** `unrelated` means read it
  yourself. The screenshots exist so that takes seconds.

## Layout

```
app.py                 Flask server, upload + SSE progress
citecheck/
  pdf_parse.py         PDF -> clean text, body/bibliography split
  intext.py            citation markers + the sentence around them
  refs.py              bibliography -> structured entries
  resolve.py           entry -> URL + metadata (Crossref/OpenAlex/S2)
  fetch.py             retrieve source text, guarantee the abstract
  match.py             lexical scoring + Claude/OpenAI judging
  crosscheck.py        author-mismatch and duplicate checks
  shots.py             header + highlighted-evidence screenshots
  pipeline.py          orchestration
```
