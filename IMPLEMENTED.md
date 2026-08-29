# How CiteCheck was built

The companion to [README.md](README.md). That file says what the tool does; this
one says what it is made of, why each piece was chosen over the alternatives, and
the rules the whole thing is built to obey.

Read top to bottom it is the order the tool was actually assembled in: a paper
becomes text, text becomes citations and references, a reference becomes a real
work, a work becomes retrievable content, content becomes a verdict, and a
verdict becomes evidence somebody can check.

---

## Why this exists at all

Every paper rests on its bibliography, and almost nobody checks one. Verifying a
single reference by hand means finding the cited work, confirming it is the work
that was meant, checking it has not been retracted, locating the sentences that
cite it, and reading enough of the source to decide whether it says what it was
cited for. Ten minutes each, on a good day, for a bibliography of 150.

So it does not get done. Reviewers spot-check three. Editors trust the author.
Supervisors skim. And the failures that follow are not exotic — they are
ordinary, and they are everywhere:

- **References that do not exist.** Plausible title, plausible authors, plausible
  DOI, no such paper. Generated text produces these constantly, and they are
  invisible to a reader who does not look each one up.
- **Retracted sources**, cited years after the notice, holding up a claim that
  no longer stands on anybody's authority.
- **Citations that do not say what they are cited for.** The most common failure
  and the least detectable: a real, findable, respectable paper attached to a
  claim it never makes.
- **Numbering that has slipped.** One reference deleted mid-draft and every
  marker after it points at the wrong entry.

Each of those is mechanically checkable. None of them requires judgement about
the *quality* of a paper — only about whether the citation matches the source.
That is a narrow enough question to automate honestly, which is the whole
premise: **screen mechanically, decide humanly.** The tool's job is to put the
evidence in front of a person in seconds instead of ten minutes, and then get out
of the way.

### The rule everything else bends around

Wrongly accusing an author of fabricating a citation is the worst thing this tool
can do. A false "not found" is not a small error to be traded off against
coverage — it is an accusation of misconduct, made by software, in a document
that gets handed to other people.

So the asymmetry is built in at every level, not bolted on at the end:

- Two thresholds with a deliberate gap between them, and a whole band that means
  "could not confirm — check by hand" rather than "does not exist"
  ([Does the work exist?](README.md#does-the-work-exist)).
- An index that failed to answer is never counted as an index saying no.
- The lexical tier is structurally incapable of returning `unrelated`, because
  low word overlap is not evidence of a mismatch.
- Harsh model verdicts are re-read against the source's own abstract before they
  are allowed to stand.
- A reference the run's clock stopped is `unverified`, never `not_found`.

Read the rest of this document with that in mind. A surprising amount of the
design is downstream of it.

---

## Step 1 — The runtime: Python 3.12, Flask, threads

**Used:** Python 3.12, Flask 3, the standard library's `threading` and
`ThreadPoolExecutor`, server-sent events for progress.

**Why Python.** Every library this problem needs — PDF text extraction, HTTP,
fuzzy matching, browser automation, an OpenAI client — is first-class in Python
and second-class elsewhere. The tool is a pipeline of other people's parsers;
choosing the language with the best parsers is most of the decision.

**Why Flask, not FastAPI or Django.** The server does five things: accept an
upload, stream progress, serve a JSON report, serve a PDF and serve PNGs. Django
brings an ORM, migrations and an admin for an application with no database.
FastAPI's advantage is async I/O, and the work here is not async-shaped — it is
blocking network calls and a *synchronous* browser API (Playwright's sync API is
thread-affine), so it would have meant threads underneath the async anyway.
Flask is ~440 lines of `app.py` and no ceremony.

**Why threads and not Celery, RQ or a task queue.** A run is one upload by one
person watching a progress bar. A queue would add a broker, a worker process, a
result backend and a deployment story, to solve a problem — surviving a server
restart mid-run — that this tool does not have. The run is a daemon thread; the
progress stream is an append-only event log the client walks by index, so a
client that connects late replays what it missed exactly once and several clients
can watch the same run at once.

**Why server-sent events and not WebSockets.** Progress is one-directional. SSE
is a `text/event-stream` response and an `EventSource` in the browser — no
protocol upgrade, no library, no reconnection logic. The client never needs to
say anything back mid-run.

**Where the work is parallel.** References are independent, so they are checked
in a thread pool (1–8 workers, default 4). Almost all the cost is network wait,
which threads overlap perfectly well despite the GIL.

**What that made necessary.** A pool with no ceiling is a hang waiting to happen:
a publisher that accepts a connection and then sends nothing wedges a worker
indefinitely, and `requests` and `page.goto` take timeouts but `page.evaluate`
does not. A thread blocked in either cannot be interrupted from outside. So the
checking phase carries a wall-clock budget, and anything still running when it
expires is reported as unverified with the step it was on — one stalled
reference costs minutes, not the entire report.

---

## Step 2 — PDF to text: PyMuPDF

**Used:** PyMuPDF (`fitz`) for reading uploads, for rendering evidence, and for
reading any PDF fetched from a publisher.

**Why PyMuPDF over pdfminer.six, pdfplumber or PyPDF.** Three reasons, and only
the first is speed:

1. It extracts text *with coordinates* — blocks, lines and word boxes. That is
   what makes reading order recoverable on a two-column paper, and it is the same
   data the highlighting step needs later.
2. It renders. The evidence screenshots of PDFs are drawn by the same library
   that found the text, so a match is guaranteed to be highlightable.
3. It annotates. Highlights are real PDF annotations on the page, not boxes
   drawn over a bitmap by guesswork.

Using one library for parse, render and annotate removes a whole class of bug
where the text you matched and the pixels you drew disagree about where anything
is.

**What had to be written on top of it.** Raw extraction is not readable prose:

- **Column ordering.** PyMuPDF's own `sort=True` orders blocks by position across
  the whole page, which on a two-column paper interleaves the columns —
  consecutive sentences arrive from opposite sides of the gutter. Prose survives
  that badly and a reference list not at all: entries come out shredded into
  alternating halves and none of them parse. So the gutter is detected and each
  column is read in turn, with full-width blocks splitting the page into bands so
  a running header cannot jump to the top.
- **De-hyphenation**, so "agri-\ncultural" is one word again.
- **Invisible characters.** Publishers inject zero-width spaces into long
  strings, so a DOI printed as `10.<ZWSP>1109/…` reads normally and matches no
  pattern.
- **Running headers**, detected as short lines repeated on most pages.
- **The bibliography boundary**, searched from the back — papers mention
  "References" early — and falling back to a dense run of `[n]`-shaped lines when
  there is no heading at all.

---

## Step 3 — Citations and references: regular expressions and dataclasses

**Used:** hand-written Unicode-aware regular expressions, plain dataclasses.

**Why not GROBID or anystyle.** GROBID is the right answer for bulk
bibliographic extraction, and it is a Java service with a machine-learning model
behind it — a second runtime, a second deployment, and a container that dwarfs
this entire application. anystyle is Ruby. Both would have made "clone it and run
`python app.py`" untrue, which was a requirement. The bet was that a few hundred
lines of carefully-tested pattern matching could cover the styles that actually
turn up, and `tests/test_styles.py` is the receipt: APA, Harvard, Vancouver,
IEEE, ACM, Nature, Chicago, MLA and the Elsevier/Springer numbered styles, in
every author order, with accented and double-barrelled surnames.

**What the patterns had to survive**, each of which was a real failure before it
was a rule:

- Surnames are not ASCII. An ASCII-only character class stops at the first letter
  of "Eißfeldt" and loses the marker entirely.
- Author order is not agreed. "Agatz, N.", "N Agatz", "Bosona, Tesfaye" and
  "Kastner M, Tricco AC" are the same information in four layouts, and a parser
  that reads only the first mistakes the whole author list for the title — which
  is the field every "is this the right work?" test measures against.
- `[0,1]` is a value range, not a citation. No bibliography has an entry zero.
- `Table 2 (2019)` looks exactly like an author-year marker.
- A numbered bibliography can be cited author-year style, so numbered entries
  carry author-year aliases too — and an alias two entries both answer to is
  dropped, because verifying a claim against the wrong source is worse than
  reporting the marker as unmatched.
- A paper uses one citation scheme, so where both patterns fire, the majority
  wins. Numeric winning outright threw away every real citation in an author-year
  review whose summary tables happened to number their rows.

**The one idea that changed the verdicts most.** A marker is not answerable for
the sentence around it. In

> parameters such as soil moisture **[37]**, field temperature **[38]** and crop
> yield **[39]** can all be predicted

handing the whole sentence to all three sources asks each to support the other
two's content, and all three come back "weak" — three false findings from one
sentence. So each marker is cut down to the clause it governs, with the full
sentence carried alongside as context. That is a parsing decision that only pays
off three steps later, in the judge.

---

## Step 4 — Does the work exist: five open scholarly APIs

**Used:** `requests` against Crossref, OpenAlex, Semantic Scholar, arXiv,
Unpaywall and Europe PMC. No API keys, no paid data.

**Why five and not one.** They disagree, and the disagreement is the signal.
Crossref is the DOI registry, so a DOI it does not hold is very likely not a DOI.
OpenAlex flags retractions and is often faster to update than the Crossref
record. Semantic Scholar carries abstracts and open-access PDFs the others miss.
Europe PMC is the only one here that serves **machine-readable full text** —
which is the difference between judging a paper by its blurb and actually reading
it. Unpaywall has the broadest open-access coverage of any of them and refuses to
answer without a contact address, which is why the README pushes so hard on
setting one.

**The three-way answer.** Every index's response is recorded as a hit, a clean
miss, or a *transport failure*, kept in three separate lists. This is the single
most important structural decision in the resolver: an index being unreachable
must never be mistaken for the index saying "no", because the second is evidence
of fabrication and the first is evidence of nothing. Collapsing them into a
boolean is how a network hiccup becomes an accusation.

**Two failure modes that had to be designed out.**

*Taking row 0 on faith.* Crossref ranks by its own relevance score, which is
frequently not the best title match. The row that matches what was actually cited
is often second or third, so every candidate is scored — and title overlap alone
is brittle, so a matching first-author surname or publication year is allowed to
lift a borderline match over the line. Title similarity is symmetric (Jaccard)
rather than divided by the smaller token set: the obvious version scores "A
Scalable Location Service for Geographic Ad Hoc Routing" against "A Scalable
Security Service for Geographic Ad-Hoc Routing" at 0.83 and accepts an entirely
different paper.

*Anchoring to a guess.* When no identifier is printed, a title search may write a
wrong DOI into the record. Every later index is then asked about *that* DOI and
happily confirms it, and three indices appear to independently agree on a paper
the author never cited. So a weak search result cannot anchor anything
downstream, and every "is this the same work?" test measures against
`claimed_title` — what the citing paper printed — never against whatever the last
index guessed.

---

## Step 5 — Getting the content: requests, BeautifulSoup, lxml

**Used:** `requests` with a size cap and streaming reads; BeautifulSoup with the
`lxml` parser for HTML and JATS XML; PyMuPDF for fetched PDFs.

**The order of preference** is open-access mirror, then publisher URL, then
landing page — because a screenshot of a paywall proves nothing.

**Three content types, three paths.** A PDF goes through PyMuPDF. JATS XML from
Europe PMC is parsed properly rather than scraped, with tables, figures and the
cited paper's *own* reference list stripped out — a bibliography is full of other
people's titles and matches all sorts of claims. HTML gets its navigation, forms
and scripts removed, then the article body if the page marks one up.

**The guarantee that matters most: the abstract is always in what gets judged.**
Publisher pages are paywalled more often than not, but the abstract is almost
always indexed somewhere. Leading with it means a locked-down source is still
assessed on its real content rather than scored against a login page — and when
full text *did* arrive, the abstract costs nothing and states the paper's claims
more directly than its introduction does. On a typical IEEE/Elsevier/Springer
bibliography this is the difference between a report full of `unverified` and a
usable one.

**What is not done.** CAPTCHAs and consent walls are not bypassed. Several
publishers serve a bot check instead of the article, and defeating those is off
the table — so where it happens, the report says so and falls back to a labelled
abstract card rather than pretending it captured a page.

---

## Step 6 — The judgement: two tiers, rapidfuzz and OpenAI

**Used:** an always-on lexical tier (IDF-weighted token overlap +
`rapidfuzz.fuzz.token_set_ratio`) and an optional model tier (OpenAI chat
completions with **structured outputs**).

**Why two tiers and not one.** The model tier is far better and cannot be
required: no key, a rejected key, a rate limit, or a source too thin to judge all
have to degrade into something rather than nothing. The lexical tier also does a
job the model cannot — it locates *where in the source* the matching passage sits,
with an offset, which is what the evidence screenshot highlights. So it runs on
every reference even when the model is judging.

**Why the lexical tier may never say `unrelated`.** Word overlap can show that
two texts *do* discuss the same thing; low overlap shows nothing at all. "In the
early stages they had military purposes [1]" shares almost no vocabulary with the
abstract that supports it. Calling that unrelated would accuse an author on the
basis of arithmetic, so the honest floor is `unverified` and only a tier that has
actually read both texts may make the accusation.

**Why structured outputs.** A JSON schema with a `verdict` enum, a confidence, a
reason and a verbatim evidence quote. The verdict is machine-comparable, the
reason is human-readable, and the quote is required to be verbatim so the
screenshot step can find it on the page. Free-text output would have meant
parsing prose into a verdict, which is a second, worse classifier.

**Why determinism is pinned.** Temperature 0, `top_p` 1, fixed seed. At the
default temperature the identical claim against the identical text comes back
`related` on one call and `weak` on the next — and to anyone watching, that is
the tool changing its mind for reasons they cannot see. It is what makes a
re-check look broken. Nothing about reading a citation is a creative task. A
model that rejects those parameters outright (reasoning models do) falls back to
a plain call, because losing determinism is bad and failing the reference is
worse.

**The second reading.** `unrelated` and `weak` are accusations, and the commonest
way to reach one wrongly is to judge against text that buries or misses the
abstract — a landing page, a bot-check stub, or forty pages of body text in which
the one relevant paragraph never rose to the top. So the abstract is passed under
a heading of its own rather than left to compete for attention inside a
24,000-character dump, and any harsh verdict is re-judged against that abstract
alone before it is reported. If the second reading is kinder it wins, and the
report says the verdict was reconsidered and why.

**Rolling up, in two directions.** A reference cited five times holds five
verdicts, and the tiers roll them up in opposite directions on purpose. The model
tier takes the *most concerning*: when something that has read both texts says a
claim is unsupported, that is evidence, and evidence about one claim is not
cancelled by four others. The lexical tier takes the *best*: low overlap is not
evidence, so letting the weakest sentence set the headline would make a reference
look worse the more often it is cited. Getting this backwards in either direction
produces a card that punishes the reader for improving it.

---

## Step 7 — The evidence: Playwright and PyMuPDF

**Used:** Playwright (Chromium) for web pages, PyMuPDF for PDFs and for rendered
cards.

**Why screenshots at all.** This is the feature that makes the tool honest. A
verdict is a prompt to look, not a ruling — and telling someone to go and look is
useless if looking costs ten minutes. Three captures per reference make it
seconds: the citing sentence highlighted in *their* paper, the top of the source
so they can see the link resolved to the right work, and the matching passage
highlighted in place.

**Why Playwright over Selenium or a screenshot API.** It bundles its own browser,
drives it over a modern protocol, and — decisively — can run arbitrary JavaScript
in the page and hand back the result. The highlighter needs exactly that: it
builds a whitespace-normalised copy of the page's visible text along with a
per-character map back into the DOM, finds the passage in that flat string,
rebuilds a DOM `Range` from the map, and overlays boxes on its client rects.
Nothing that only takes pictures could do it.

**Why PDFs bypass the browser.** A browser's built-in PDF viewer is a black box
you cannot ask where a sentence is. PyMuPDF hands back word boxes, so matching is
done over normalised word tokens and survives line wraps, column breaks and
hyphenation — the extracted text was de-hyphenated while the page still carries
the break, so both sides are rejoined the same way and line up.

**Why candidates, not a candidate.** The highlighter is given up to forty
progressively shorter search strings: whole sentences first, because they make
the most convincing highlight, then sliding word windows, because a long span
fails whenever it crosses a column, page or markup boundary and somewhere in a
passage there is almost always a contiguous run of eight words on one line.
Candidates that are technically findable but tell the reader nothing — stopword
runs, citation debris, equation fragments — are rejected.

**The abstract card.** Where a publisher serves a bot check there is genuinely
nothing on the page to highlight. Rather than an empty panel, the abstract that
*was* legitimately retrieved from an index is rendered as a card, captioned
"INDEXED ABSTRACT — not a screenshot of the publisher page" and marked
`evidence_is_card` in the JSON. It can never be mistaken for a page capture. The
same renderer, differently captioned, shows the reader their own supplied
document back.

**Cost control.** Launching a browser per reference costs over a second each —
several minutes of pure startup on a 250-reference bibliography — so each worker
thread keeps one browser and reuses it, with a fresh context per reference for
isolation. Playwright's sync API is thread-affine, so shutdown has to happen on
the owning thread: a barrier occupies every worker at once so each closes exactly
its own, otherwise a fast thread takes all the cleanup tasks and the rest leak a
Chrome process per run.

---

## Step 8 — The report: JSON first, then a page over it

**Used:** `report.json` as the durable artefact; vanilla JavaScript and
hand-written CSS as a view over it, with no build step; the browser's own print
as the export.

**Why JSON is the product.** Everything a run learned is on disk in one file, and
the web page is one reader of it. That means the summary can be *re-derived* on
the way out: a report written by older code that is missing whatever the tally
has since learned to count is brought up to date on read, instead of showing a
banner that disagrees with its own cards forever.

**Why no front-end framework.** The page is one upload form, one progress bar and
a list of cards. React or Vue would add a build step, a `node_modules`, and a
deployment artefact to a project whose entire selling point is `python app.py`.
The interactions that actually needed thought — re-rendering one card in place
without the list re-sorting under the reader, holding a card's distance from the
top of the viewport across a rebuild so nothing moves — are not the ones a
framework solves for you.

**Why printing is the export.** The report is already a document with the
screenshots already on the page. Generating a PDF server-side would mean
rendering this same page in the headless Chromium that takes the screenshots, for
a worse result — it cannot see which filter the reader is looking at. What the
export *does* need is care: every card opened and put back, every lazy image
forced and awaited with a counter so it does not read as a hang, and a caption
recording the filter, the time and any screenshot that printed blank.

**Two counts, never merged.** A reference's headline is a roll-up of the
citations beneath it, so a reference-level tally and a citation-level tally are
answers to different questions — and a summary that shows one while the cards
show the other reads as simply broken. Both are computed, both are labelled, and
the sections a reader filters by are built from "references carrying at least one
citation of this verdict", so they overlap and sum to more than the reference
count. Deliberately.

---

## Step 9 — The part that makes it usable: the human in the loop

This is the last thing that was built and the reason the tool is worth handing to
somebody. A screening tool that cannot be corrected is a tool that gets ignored
the first time it is wrong.

**Re-check one reference.** Resolve, fetch, judge and screenshot that one
reference again — the answer to a publisher that was down or a host that stalled
the run out of its time budget. Seconds, instead of re-running two hundred checks
that were already right.

**Judge against a file you supply.** Hand over the PDF or `.txt` you have.
Resolution and fetching are skipped outright: you have said which document this
reference is, which is stronger evidence than any bibliographic search. What the
indices said — that the work exists, that it has or has not been retracted — is
carried over untouched, because none of it was re-tested.

**Re-check, or overrule, one citation.** A reference cited five times holds five
judgements, and a reader who has just read the source usually disagrees with one
of them, not all five. Overruling the whole card to fix one throws away four
verdicts that were right.

**Set the verdict yourself.** The tool screens; a person decides.

And then the rules that keep that honest, which are most of the code:

- **A hand-set verdict is always labelled as one** — on the card, in the reason
  line, in the exported PDF, and in the screening banner *even when the result is
  `clear`*. A report that reads clean because somebody marked it clean is a
  different document, and whoever receives it was not in the room.
- **What the tool found is displaced, never destroyed.** The original verdict and
  reason are kept, shown side by side, and restored exactly on undo — and the
  saved baseline is dropped once nothing is displacing it, so undo returns the
  entry to precisely the shape it had.
- **A re-check clears a hand-set verdict on the same thing, and says so.** That
  verdict was a reading of evidence the re-check has just replaced; carrying it
  forward would attach the reader's name to a reading of something they never
  saw.
- **A failed lookup is never a new judgement.** A re-check that retrieved nothing
  keeps the earlier entry whole — verdict, evidence and screenshots all describe
  one retrieval, and restoring the headline alone leaves a card whose evidence
  contradicts it. It is reported as the failure it was.
- **Everything derived is re-derived.** Tally, banner and warnings are recomputed
  from the entries after every change, so a finding that has been resolved leaves
  the summary the moment it is resolved.

---

## Step 10 — Tests: unittest, no network, no keys

**Used:** the standard library's `unittest`. 176 tests, none of which touches the
network or needs an API key.

**Why not pytest.** One less dependency for a suite this size, and
`python -m unittest discover -s tests -t .` works on any checkout.

**Why nothing reaches the network.** A test that calls Crossref fails when
Crossref is slow, and a suite that fails for reasons unrelated to the code is a
suite people stop running. Every source, report and bibliography a test needs is
written out in the file.

The suite is in four parts:

| File | Guarantees |
|---|---|
| `test_styles.py` | Every bibliography and marker style, spelled out in the file so it runs anywhere. |
| `test_corpus.py` | The real papers that have actually broken the parser. Bounds, not exact counts — parsing *more* references is an improvement and should never require editing a test. The PDFs are published articles and are not committed, so missing ones skip. |
| `test_claims.py` / `test_claim_recheck.py` / `test_report.py` | Claim scoping, marker precision, the screening headline, and the narrowness of a single-citation re-check. |
| `test_stability.py` | The same paper, checked twice, saying the same thing. |

That last one earns its place. Every test in it is a number or a verdict that
once moved when nothing about the citation had, and each was experienced by a
reader as the tool changing its mind: the summary counting references while the
cards counted citations; a citing sentence the model failed to answer on being
dropped from the roll-up, so the headline depended on which calls happened to
succeed; a re-check that retrieved nothing overwriting a verdict reached on text
that had been retrieved. None of those is a crash. All of them destroy trust.

---

## Step 11 — Shipping it: Docker, gunicorn, one shared password

**Used:** a Debian-bookworm Python image with Chromium installed at build time,
gunicorn, HTTP basic auth.

**Why the base image is pinned to bookworm.** Playwright supports a fixed list of
distributions. The unsuffixed `python:3.12-slim` tag follows Debian's latest, so
an upstream bump would quietly drop the base off that list and fail the build at
`playwright install` — several minutes in, on the very last layer.

**Why one worker and sixteen threads.** In-flight runs are per-process state, so
a second worker would strand an upload on one process and its progress stream on
another, and the client would sit watching a run that never moves. `--timeout 0`
stops gunicorn reaping the progress streams, which stay open for an entire run.

**Why Chromium lives outside any home directory.** Root installs the browsers;
the unprivileged runtime user has to be able to launch them.

**Why the sandbox flag is opt-in.** A container grants Chromium neither the
`SYS_ADMIN` its own sandbox needs nor a `/dev/shm` large enough to render a page,
so it dies on launch and every screenshot silently degrades. Both protections are
worth keeping locally, so dropping them is a deliberate environment variable
rather than a default.

**Why a password, and why only one.** The app is open unless
`CITECHECK_PASSWORD` is set — right for `127.0.0.1` and wrong for anywhere a
stranger can reach, because every upload spends real money against the model key.
It is one shared password compared in constant time, not a user system: it exists
so a link can be handed to someone who has no account, not to identify who is on
the other end.

---

## The whole stack, in one table

| Layer | Choice | The alternative, and why not |
|---|---|---|
| Language | Python 3.12 | The parsers this problem needs are all first-class here. |
| Web | Flask 3 | Django brings an ORM for an app with no database; FastAPI's async buys nothing against blocking calls and a thread-affine browser API. |
| Concurrency | `ThreadPoolExecutor` + SSE | A task queue adds a broker and a worker process to solve a problem this tool does not have. |
| PDF | PyMuPDF | pdfminer/pdfplumber extract text but do not render or annotate; one library for all three keeps text and pixels in agreement. |
| Bibliography | Regex + dataclasses | GROBID is better and is a Java service; the requirement was `python app.py`. |
| Existence | Crossref, OpenAlex, Semantic Scholar, arXiv, Europe PMC, Unpaywall | One index cannot disagree with itself, and the disagreement is the evidence. |
| Content | requests + BeautifulSoup/lxml | JATS XML is parsed properly rather than scraped; the abstract is always folded in. |
| Lexical judging | rapidfuzz + IDF overlap | Never allowed to accuse; also locates the passage the screenshot highlights. |
| Model judging | OpenAI structured outputs | Free text would need a second parser to turn prose into a verdict. |
| Evidence | Playwright + PyMuPDF | Selenium and screenshot APIs cannot run the DOM-range highlighter the web path needs. |
| Front end | Vanilla JS + CSS | A framework adds a build step to a project whose premise is that there isn't one. |
| Export | `window.print()` | Server-side rendering would use the same headless Chromium for a worse result. |
| Tests | `unittest`, offline | A suite that fails when Crossref is slow is a suite nobody runs. |
| Deploy | Docker + gunicorn + basic auth | One process, because run state is per-process. |

---

## What it deliberately does not do

Worth stating, because each of these was a decision rather than an omission.

- **It does not bypass CAPTCHAs, consent walls or bot checks.** Where a publisher
  serves one, the report says so and shows the indexed abstract instead.
- **It does not score papers.** No number out of 100. A screening judgement built
  from findings you can check, because an opaque score invites arguments nobody
  can settle.
- **It does not flag corrections or errata.** They are collected and noted, but an
  amended paper says the cited work was corrected, not that the citation
  misrepresents it — and the latter is the only question this report answers.
- **It does not accuse on thin evidence.** A fabricated reference with a plausible
  title may land in `unconfirmed` rather than `not_found`. That is the intended
  trade.
- **It does not decide anything.** Every verdict is a prompt to look, the
  screenshots exist so that takes seconds, and when the tool got it wrong there
  are four ways to correct it — all of which say, permanently, that a person did.
