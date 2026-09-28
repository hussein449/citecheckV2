"""Decide whether a cited source actually supports the claim made about it.

Two tiers:
  * a lexical tier that always runs — IDF-weighted overlap plus fuzzy phrase
    matching. It also locates the best-matching passage in the source, which is
    what the screenshot step highlights.
  * an optional model tier (when OPENAI_API_KEY is set) that reads the claim
    and the source text and returns a reasoned verdict.

The lexical tier is never skipped: even under the model tier, its passage
location is what anchors the evidence screenshot.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, asdict

from rapidfuzz import fuzz

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "he", "her", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "may", "might", "more",
    "most", "must", "no", "not", "of", "on", "or", "other", "our", "over", "same",
    "she", "should", "so", "some", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "through", "to", "two",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "will",
    "with", "would", "you", "your", "et", "al", "fig", "figure", "table", "eq",
    "also", "however", "using", "used", "use", "based", "shown", "show", "results",
    "study", "studies", "paper", "work", "approach", "method", "methods", "data",
}

VERDICTS = ("supported", "related", "weak", "unrelated", "unverified", "not_found")

# How much each verdict demands a human look at it. Rolling several per-claim
# verdicts up to one headline takes the most concerning, not the average: a
# reference that genuinely supports four claims and misrepresents a fifth is a
# problem, and averaging is exactly how that fifth claim disappears.
_CONCERN = {
    "unrelated": 5,
    "not_found": 5,
    "weak": 4,
    "unverified": 3,
    "related": 2,
    "supported": 1,
}


def concern(verdict: str) -> int:
    """How much *verdict* demands a human look.

    Exposed because callers that hold stored report dicts rather than
    `ClaimVerdict` objects still have to order verdicts by the same scale, and
    a second ordering defined somewhere else is one that will drift from this
    one the first time a verdict is added.
    """
    return _CONCERN.get(verdict, 0)


def most_concerning(verdicts) -> str:
    """The verdict among *verdicts* that most demands a human look."""
    return max(verdicts, key=lambda v: _CONCERN.get(v, 0), default="unverified")


def roll_up(verdicts, engine: str) -> str:
    """One headline from several per-claim verdicts, the way *engine* would.

    Exposed because a reference whose claims a reader has edited must re-derive
    its headline exactly as the engine that produced them would have — and the
    two tiers roll up in opposite directions (see `judge`). Applying the model's
    worst-case rule to a lexically judged reference would mean improving one
    claim makes the card look *worse*, which is indefensible in front of the
    person who just did the improving.
    """
    verdicts = list(verdicts)
    if not verdicts:
        return "unverified"
    if engine and engine != "lexical":
        return most_concerning(verdicts)
    return min(verdicts, key=lambda v: _CONCERN.get(v, 0))


@dataclass
class Passage:
    text: str
    score: float
    offset: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Claim:
    """One thing a citing paper asserts on the strength of one reference.

    `text` is the clause the marker governs, which is often narrower than the
    sentence it sits in. `context` carries that full sentence, because a clause
    read alone can lose the subject it depends on — and `co_cited` says how many
    references were cited together at that point, which is the difference
    between "this source fails to support the claim" and "this source supplies
    one of the six things the sentence lists".
    """

    text: str
    context: str = ""
    co_cited: int = 1
    page: int | None = None


def _as_claims(claims: "str | Claim | list") -> list[Claim]:
    """Accept a bare string, a list of strings, or a list of Claims."""
    if isinstance(claims, (str, Claim)):
        claims = [claims]
    out: list[Claim] = []
    for item in claims or []:
        claim = item if isinstance(item, Claim) else Claim(text=str(item))
        if claim.text and claim.text.strip():
            out.append(claim)
    return out


@dataclass
class ClaimVerdict:
    """One citing claim, judged on its own merits."""

    claim: str
    verdict: str = "unverified"
    score: float = 0.0
    reason: str = ""
    evidence_quote: str = ""
    page: int | None = None
    # The full sentence the claim was cut from, when it is narrower than that
    # sentence — so the report can show a reader what was actually judged.
    context: str = ""
    # Set when a harsh verdict was re-checked against the cited work's own
    # abstract and the second look disagreed. See `_second_look`.
    reconsidered: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MatchResult:
    verdict: str = "unverified"
    score: float = 0.0
    reason: str = ""
    engine: str = "lexical"
    best_passage: Passage | None = None
    passages: list[Passage] = field(default_factory=list)
    shared_terms: list[str] = field(default_factory=list)
    matched_claim: str = ""
    claim_verdicts: list[ClaimVerdict] = field(default_factory=list)

    def screenshot_passage(self) -> str:
        """Text to hunt for on the page.

        Prefers real page text over the synthetic title+abstract block (which
        appears nowhere verbatim), and prefers a substantial passage over a
        short fragment — a stray half-line scores well but highlights nothing
        a reader can act on.
        """
        chosen = self.screenshot_passages()
        return chosen[0] if chosen else ""

    def screenshot_passages(self) -> list[str]:
        """All plausible highlight targets, best first.

        Handing the screenshot step several candidates rather than one matters:
        the top-scoring passage may be absent from the rendered page even when a
        lower-ranked one is right there.
        """
        real = [p for p in self.passages if p.offset >= 0 and p.score > 0]
        if not real and self.best_passage:
            real = [self.best_passage]
        ordered = sorted(real, key=lambda p: (len(p.text) < 120, -p.score))
        out: list[str] = []
        for passage in ordered:
            if passage.text and passage.text not in out:
                out.append(passage.text)
        return out

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "score": round(self.score, 3),
            "reason": self.reason,
            "engine": self.engine,
            "best_passage": self.best_passage.to_dict() if self.best_passage else None,
            "passages": [p.to_dict() for p in self.passages],
            "shared_terms": self.shared_terms,
            "matched_claim": self.matched_claim,
            "claim_verdicts": [c.to_dict() for c in self.claim_verdicts],
        }

    def claim_tally(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for claim in self.claim_verdicts:
            counts[claim.verdict] = counts.get(claim.verdict, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Lexical tier
# --------------------------------------------------------------------------- #

def _stem(token: str) -> str:
    """Crude suffix stripping — enough to align 'farming' with 'farms'."""
    for suffix in ("ational", "ization", "iveness", "ing", "edly", "ies", "ied",
                   "ess", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9\-]{1,}", (text or "").lower())
    return [_stem(w) for w in words if w not in _STOPWORDS and len(w) > 2]


def split_passages(text: str, target: int = 320) -> list[tuple[int, str]]:
    """Chop source text into passage-sized windows with their offsets.

    PDF and HTML extraction both emit a newline per rendered line, so splitting
    naively on newlines yields 60-character fragments rather than passages. Soft
    line wraps are rejoined first; only blank lines separate paragraphs.
    """
    if not text:
        return []

    joined = re.sub(r"\n[ \t]*\n[\s]*", "\x00", text)   # blank line = real break
    joined = joined.replace("\n", " ")                   # everything else wraps
    joined = re.sub(r"[ \t]{2,}", " ", joined)

    chunks: list[tuple[int, str]] = []
    cursor = 0
    for para in joined.split("\x00"):
        base = cursor
        cursor += len(para) + 1
        para = para.strip()
        if len(para) < 40:
            continue
        if len(para) <= target * 1.6:
            chunks.append((base, para))
            continue
        # Long paragraph: break on sentence boundaries into ~target windows.
        offset = 0
        buf: list[str] = []
        buf_start = 0
        for sent in re.split(r"(?<=[.!?])\s+", para):
            if not buf:
                buf_start = offset
            buf.append(sent)
            offset += len(sent) + 1
            if sum(len(s) for s in buf) >= target:
                chunks.append((base + buf_start, " ".join(buf).strip()))
                buf = []
        if buf:
            chunks.append((base + buf_start, " ".join(buf).strip()))
    return [(off, c) for off, c in chunks if len(c) > 40]


def _idf(passages: list[str]) -> dict[str, float]:
    n = max(1, len(passages))
    doc_freq: dict[str, int] = {}
    for passage in passages:
        for token in set(tokenize(passage)):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    return {t: math.log(1 + n / (1 + df)) for t, df in doc_freq.items()}


def score_passage(claim_tokens: list[str], passage: str, idf: dict[str, float]) -> tuple[float, list[str]]:
    """Weighted overlap between the claim and one passage, 0..1."""
    if not claim_tokens:
        return 0.0, []
    passage_tokens = set(tokenize(passage))
    if not passage_tokens:
        return 0.0, []

    claim_set = set(claim_tokens)
    shared = claim_set & passage_tokens
    if not shared:
        return 0.0, []

    default_idf = 1.0
    hit = sum(idf.get(t, default_idf) for t in shared)
    total = sum(idf.get(t, default_idf) for t in claim_set)
    overlap = hit / total if total else 0.0

    # Phrase-level similarity catches reworded but genuinely matching content.
    phrase = fuzz.token_set_ratio(" ".join(claim_tokens), passage.lower()) / 100.0

    score = 0.68 * overlap + 0.32 * phrase
    return min(1.0, score), sorted(shared, key=lambda t: -idf.get(t, default_idf))[:10]


def lexical_match(
    claims: str | list[str],
    source_text: str,
    title: str = "",
    abstract: str = "",
) -> MatchResult:
    """Score each citing sentence against the source and keep the best hit.

    Scoring per sentence rather than on all of them concatenated matters: a
    reference cited eight times would otherwise be judged against a 2,000-char
    blob whose terms no single passage could ever cover, and would look weak
    purely for being cited often.
    """
    result = MatchResult(engine="lexical")
    tokenized = [(c, tokenize(c.text)) for c in _as_claims(claims)]
    tokenized = [(c, t) for c, t in tokenized if t]
    if not tokenized:
        result.reason = "The citing sentences carried no comparable content words."
        return result

    windows = split_passages(source_text)
    # Title and abstract are strong signals even when full text is thin. The
    # -1 offset marks this as a synthetic block, not a real passage on the page.
    header = " ".join(filter(None, [title, abstract]))
    if header:
        windows.insert(0, (-1, header[:1200]))

    if not windows:
        result.reason = "No readable text was retrieved for this reference."
        return result

    idf = _idf([w for _, w in windows])

    best_overall = -1.0
    best_rows: list[tuple[float, Passage, list[str]]] = []
    best_claim = tokenized[0][0].text

    for claim, claim_tokens in tokenized:
        scored: list[tuple[float, Passage, list[str]]] = []
        for offset, passage in windows:
            score, shared = score_passage(claim_tokens, passage, idf)
            scored.append(
                (score, Passage(text=passage[:600], score=round(score, 3), offset=offset), shared)
            )
        scored.sort(key=lambda row: -row[0])
        top = scored[:3]

        # Blend the best passage with the mean of the top three so a single
        # lucky sentence cannot carry an otherwise unrelated document.
        blended = 0.72 * top[0][0] + 0.28 * (sum(s for s, _, _ in top) / len(top))
        result.claim_verdicts.append(
            ClaimVerdict(
                claim=claim.text,
                verdict=_verdict_from_score(blended),
                score=round(blended, 3),
                reason=f"Lexical overlap {blended:.2f} against the retrieved text.",
                evidence_quote=top[0][1].text if top[0][0] > 0 else "",
                page=claim.page,
                context=claim.context if claim.context != claim.text else "",
            )
        )
        if blended > best_overall:
            best_overall = blended
            best_rows = top
            best_claim = claim.text

    result.passages = [p for _, p, _ in best_rows]
    result.best_passage = best_rows[0][1]
    result.shared_terms = best_rows[0][2]
    result.matched_claim = best_claim
    result.score = round(best_overall, 3)
    result.verdict = _verdict_from_score(result.score)
    result.reason = _lexical_reason(result)
    return result


def _verdict_from_score(score: float) -> str:
    """Map a lexical score to a verdict.

    Deliberately never returns "unrelated". Word overlap can show that two texts
    *do* discuss the same thing, but low overlap is not evidence of a mismatch —
    a citing sentence like "in the early stages they had military purposes [1]"
    shares almost no vocabulary with the abstract of the paper that supports it.
    Calling that "unrelated" would accuse an author of miscitation on the basis
    of nothing. Only the model tier, which actually reads both texts, may make
    that claim; here the honest floor is "can't tell".
    """
    if score >= 0.52:
        return "supported"
    if score >= 0.34:
        return "related"
    if score >= 0.18:
        return "weak"
    return "unverified"


def _lexical_reason(result: MatchResult) -> str:
    terms = ", ".join(result.shared_terms[:6]) or "no distinctive terms"
    if result.verdict == "unverified":
        return (
            f"Word overlap with the retrieved text was low ({result.score:.2f}), "
            "which is not enough to judge either way — this needs a human read, "
            "or model judging enabled."
        )
    return f"Lexical overlap {result.score:.2f}; strongest shared terms: {terms}."


# --------------------------------------------------------------------------- #
# Model tier
# --------------------------------------------------------------------------- #

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["supported", "related", "weak", "unrelated", "unverified"],
            "description": (
                "supported: the source directly backs the claim. "
                "related: same topic and consistent, but does not state the claim. "
                "weak: only loosely connected. "
                "unrelated: the source is about something else. "
                "unverified: the retrieved text is too thin to judge."
            ),
        },
        "confidence": {"type": "number", "description": "0 to 1."},
        "reason": {"type": "string", "description": "One or two sentences of justification."},
        "evidence_quote": {
            "type": "string",
            "description": (
                "The sentence from the source that best corresponds to the claim, "
                "quoted verbatim. Empty string if none exists."
            ),
        },
    },
    "required": ["verdict", "confidence", "reason", "evidence_quote"],
    "additionalProperties": False,
}

def _judge_prompt(
    claim: Claim,
    reference_line: str,
    title: str,
    abstract: str,
    excerpt: str,
) -> str:
    blocks = [f"# Claim this reference is cited for\n{claim.text.strip()}"]

    context = (claim.context or "").strip()
    if context and context != claim.text.strip():
        blocks.append(
            "# The full sentence that claim was taken from\n"
            f"{context}\n\n"
            "Only the claim above is this reference's to support. The rest of the "
            "sentence rests on the other references cited alongside it, and is "
            "not evidence against this one."
        )

    if claim.co_cited > 1:
        blocks.append(
            f"# Note\nThis reference is one of {claim.co_cited} cited together at "
            "this point. A group citation asks each source for part of what the "
            "sentence says, not all of it."
        )

    blocks.append(
        "# Reference as printed in the bibliography\n"
        f"{reference_line.strip() or '(not available)'}"
    )
    blocks.append(f"# Title of the retrieved source\n{title.strip() or '(unknown)'}")

    # The abstract gets a heading of its own rather than being left to compete
    # for attention inside a 24,000-character dump of body text. It is the one
    # part of a source that states what the work is about in its own words, and
    # it is what a human checking this citation would read first.
    if (abstract or "").strip():
        blocks.append(f"# Abstract of the cited source\n{abstract.strip()}")

    blocks.append(f"# Text retrieved from the cited source\n{excerpt}")
    return "\n\n".join(blocks)


_JUDGE_SYSTEM = (
    "You verify academic citations. Given a claim from a citing paper and text "
    "from the work it cites, decide whether the cited work actually supports the "
    "claim.\n\n"
    "Judge only from the source text provided. Do not use outside knowledge about "
    "the paper. Read the abstract in full before deciding: it states what the work "
    "is about more directly than its body does, and a source whose abstract covers "
    "the claim's subject is not unrelated to it.\n\n"
    "Be accurate about which verdict fits. 'unrelated' means the cited work is "
    "about a different subject altogether — it is an accusation of miscitation, so "
    "do not reach for it merely because the source does not state the claim in so "
    "many words. A source on the same subject that does not assert the claim is "
    "'related'. A source that touches the subject only in passing is 'weak'. If the "
    "retrieved text is only an abstract or a stub and cannot settle the question, "
    "say so and prefer 'unverified' over guessing.\n\n"
    "Quote evidence verbatim from the source text so it can be located in the "
    "document; never paraphrase in that field."
)


@dataclass(frozen=True)
class Provider:
    """An LLM service reachable through the OpenAI-compatible chat API.

    Nearly every hosted model now speaks that API alongside its own, so one
    client covers all of them; what differs is where to send the call, what to
    call the model, and how much of the request each one honours.
    """

    id: str
    label: str
    base_url: str          # empty means the SDK default (api.openai.com)
    default_model: str
    key_hint: str
    # How far the service goes in enforcing the verdict's shape: "json_schema"
    # (strict structured output), "json_object" (valid JSON, shape unenforced)
    # or "none" (the prompt asks for JSON and the reply is parsed leniently).
    # Only the starting point — a service that rejects it is stepped down.
    response_format: str = "json_schema"
    # OpenAI honours temperature, top_p and seed together. Others reject the
    # combination (Anthropic will not take temperature and top_p at once) or
    # ignore seed, so they get temperature alone.
    full_sampling: bool = False
    # Anthropic requires an output cap; OpenAI's reasoning models reject this
    # parameter by name, so it is only sent where it is needed.
    max_tokens: int = 0


# Default models are starting points the user can overwrite in the page; they
# are named here only so a blank model field does something sensible.
PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("openai", "OpenAI", "", "gpt-4o", "sk-...", full_sampling=True),
    Provider("anthropic", "Anthropic (Claude)", "https://api.anthropic.com/v1/",
             "claude-sonnet-5", "sk-ant-...", response_format="none", max_tokens=1024),
    Provider("gemini", "Google Gemini",
             "https://generativelanguage.googleapis.com/v1beta/openai/",
             "gemini-2.5-flash", "AIza..."),
    Provider("mistral", "Mistral", "https://api.mistral.ai/v1",
             "mistral-large-latest", "API key"),
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com",
             "deepseek-chat", "sk-...", response_format="json_object"),
    Provider("groq", "Groq", "https://api.groq.com/openai/v1",
             "llama-3.3-70b-versatile", "gsk_...", response_format="json_object"),
    Provider("openrouter", "OpenRouter (any model)", "https://openrouter.ai/api/v1",
             "openai/gpt-4o", "sk-or-..."),
    Provider("custom", "Other OpenAI-compatible endpoint", "", "", "API key",
             response_format="json_object"),
)}


@dataclass(frozen=True)
class LLMConfig:
    """Which model a run is judged by, and on whose account.

    Everything blank means the operator's configuration: OPENAI_API_KEY,
    OPENAI_BASE_URL and CITECHECK_OPENAI_MODEL. A run carries its own copy
    rather than writing into the environment, where a concurrent run would pick
    it up and spend someone else's key.
    """

    provider: str = "openai"
    api_key: str = field(default="", repr=False)
    model: str = ""
    base_url: str = ""          # only read for the "custom" provider

    @property
    def spec(self) -> Provider:
        return PROVIDERS.get(self.provider, PROVIDERS["openai"])

    @property
    def uses_server_key(self) -> bool:
        """The run brought no key, so the operator's OpenAI setup applies."""
        return self.spec.id == "openai" and not self.api_key

    def key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.uses_server_key:
            return os.environ.get("OPENAI_API_KEY", "")
        # A local server (Ollama, LM Studio) usually wants no key at all, but
        # the SDK refuses to build a client without one.
        return "not-needed" if self.spec.id == "custom" and self.base_url else ""

    def endpoint(self) -> str | None:
        if self.spec.id == "custom":
            return self.base_url or None
        if self.uses_server_key:
            # The operator's gateway belongs with the operator's key; a user's
            # own OpenAI key goes to OpenAI.
            return os.environ.get("OPENAI_BASE_URL") or None
        return self.spec.base_url or None

    def model_name(self) -> str:
        if self.model:
            return self.model
        if self.spec.id == "openai":
            return (os.environ.get("CITECHECK_OPENAI_MODEL") or "").strip() or DEFAULT_MODEL
        return self.spec.default_model

    def label(self) -> str:
        """How a report names what judged it, e.g. "Anthropic (Claude) · claude-sonnet-5"."""
        if self.spec.id == "custom":
            from urllib.parse import urlparse
            host = urlparse(self.base_url).hostname or "custom endpoint"
            return f"{self.model_name()} at {host}"
        return f"{self.spec.label} · {self.model_name()}" if self.model_name() else self.spec.label


def openai_available(llm: "LLMConfig | None" = None) -> bool:
    """Whether the model tier can be called under *llm* (default: the server's)."""
    llm = llm or LLMConfig()
    if not llm.key() or not llm.model_name():
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def active_engine(llm: "LLMConfig | None" = None) -> str:
    """Which judging tier is *available* to run — not which one produced a verdict.

    A key that is present but rejected still reports "openai" here, because this
    answers "is the tier configured", which is all that is knowable before a call
    is made. What actually judged a run is a separate question, answered after
    the fact by `pipeline.run` from the per-reference engines.

    "openai" names the tier — a model reached through the OpenAI-compatible API —
    not the vendor: a run judged by Claude or Gemini reports it too, and names
    the model in its run settings. The id stays because saved reports carry it.

    CITECHECK_LLM=off forces the lexical tier and skips the model entirely,
    even for a run that brought its own key: it is the operator's switch.
    """
    if llm_forced_off():
        return "lexical"
    return "openai" if openai_available(llm) else "lexical"


def llm_forced_off() -> bool:
    """Whether the operator has switched the model tier off for every run."""
    return (os.environ.get("CITECHECK_LLM") or "").strip().lower() == "off"


def llm_available() -> bool:
    return active_engine() != "lexical"


DEFAULT_MODEL = "gpt-4o"


def model_name(llm: "LLMConfig | None" = None) -> str:
    """The model the judging tier will call, as configured.

    A name, not a dated snapshot: a hosted model behind the same name can be
    revised, which is why a run records this alongside the date it ran.
    """
    return (llm or LLMConfig()).model_name()


# Judging the same citation twice has to give the same answer. At the API's
# default temperature it does not: the identical claim against the identical
# source text comes back "related" on one call and "weak" on the next, and to
# anyone watching the report that is the tool changing its mind for no reason
# they can see. It is what makes a re-check look broken. Nothing about reading a
# citation is a creative task, so sampling is pinned off and the seed fixed.
_SAMPLING = {"temperature": 0, "top_p": 1, "seed": 20240516}


# Written into the prompt only when the service will not enforce the schema,
# so the strict OpenAI path sends exactly what it always has.
_JSON_INSTRUCTION = (
    "\n\nReply with a single JSON object and nothing else, with these keys: "
    '"verdict" (one of "supported", "related", "weak", "unrelated", "unverified"), '
    '"confidence" (a number from 0 to 1), "reason" (one or two sentences) and '
    '"evidence_quote" (a verbatim sentence from the source, or "" if none).'
)

_FORMAT_STEPS = ("json_schema", "json_object", "none")


def _complete(client, model: str, prompt: str, spec: "Provider | None" = None):
    """One judging call, with sampling pinned wherever the model allows it.

    The model is configurable, and reasoning models reject `temperature`
    outright rather than ignoring it. A rejection falls back to a plain call —
    losing determinism is bad, but failing the reference over it is worse.

    Services differ in how far they enforce the verdict's shape, so the call
    starts at the provider's strongest supported mode and steps down — strict
    schema, then plain JSON mode, then a prompt that asks for JSON — whenever
    the service rejects the one it was sent.
    """
    spec = spec or PROVIDERS["openai"]
    sampling = _SAMPLING if spec.full_sampling else {"temperature": 0}
    for mode in _FORMAT_STEPS[_FORMAT_STEPS.index(spec.response_format):]:
        system = _JUDGE_SYSTEM if mode == "json_schema" else _JUDGE_SYSTEM + _JSON_INSTRUCTION
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "citation_verdict",
                    "strict": True,
                    "schema": _JUDGE_SCHEMA,
                },
            }
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        if spec.max_tokens:
            body["max_tokens"] = spec.max_tokens
        try:
            try:
                return client.chat.completions.create(**body, **sampling)
            except Exception as exc:
                if not _rejects_sampling(exc):
                    raise
                return client.chat.completions.create(**body)
        except Exception as exc:
            if mode == "none" or not _rejects_format(exc):
                raise
    raise AssertionError("unreachable: the last format step re-raises")


def _rejects_format(exc: Exception) -> bool:
    """Whether *exc* is the service refusing the requested output format."""
    text = str(exc).lower()
    return any(p in text for p in ("response_format", "json_schema", "json_object",
                                   "structured output", "response format"))


def _rejects_sampling(exc: Exception) -> bool:
    """Whether *exc* is the API refusing a sampling parameter, not a real failure.

    Matched on the message because the SDK raises the same BadRequestError for
    an unsupported parameter as it does for a malformed schema, and retrying a
    malformed schema without temperature would just fail again more slowly.
    """
    text = str(exc).lower()
    named = any(p in text for p in ("temperature", "top_p", "seed"))
    return named and any(
        m in text for m in ("unsupported", "not supported", "unrecognized",
                            "does not support", "cannot both", "cannot be used",
                            "not allowed")
    )


def _parse_json(text: str):
    """The verdict object out of a reply, tolerating the wrapping models add.

    Only the strict path is guaranteed bare JSON. Elsewhere a model may fence it
    in a code block or say a word first, and a reference is not worth failing
    over that.
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    first, last = text.find("{"), text.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[first:last + 1])


def check_connection(llm: "LLMConfig") -> str:
    """One tiny call to confirm the key, model and endpoint work. "" means they do.

    Returns a reader-facing sentence otherwise. Never includes the key: provider
    messages can quote part of it, so only the error type is interpreted.
    """
    try:
        import openai
    except ImportError:
        return "The openai package is not installed on this server."
    if not llm.key():
        return "Add an API key first."
    try:
        client = openai.OpenAI(api_key=llm.key(), base_url=llm.endpoint(),
                               timeout=30, max_retries=0)
        body = {"model": llm.model_name(),
                "messages": [{"role": "user", "content": "Reply with the word OK."}]}
        if llm.spec.max_tokens:
            body["max_tokens"] = 16
        client.chat.completions.create(**body)
    except openai.AuthenticationError:
        return f"{llm.spec.label} rejected this API key."
    except openai.PermissionDeniedError:
        return f"This key is not allowed to use {llm.model_name()}."
    except openai.NotFoundError:
        return f"{llm.spec.label} has no model called {llm.model_name()!r} — check the model name."
    except openai.RateLimitError:
        return f"{llm.spec.label} says this key is out of quota or rate-limited."
    except openai.APIConnectionError:
        return f"Could not reach {llm.endpoint() or 'api.openai.com'}."
    except openai.BadRequestError:
        # A bare "OK" prompt only fails on the request itself, which is almost
        # always a model name the service does not recognise.
        return f"{llm.spec.label} refused the request — check the model name."
    except Exception as exc:
        return f"The test call failed ({type(exc).__name__})."
    return ""


def openai_match(
    claim: "str | Claim",
    source_text: str,
    title: str = "",
    reference_line: str = "",
    abstract: str = "",
    model: str | None = None,
    llm: LLMConfig | None = None,
) -> MatchResult | None:
    """Ask the model to judge the citation. Returns None if the call is unusable.

    Honours OPENAI_BASE_URL, so this also covers Azure-style gateways and local
    OpenAI-compatible servers. *llm* is the provider, key and model the run
    brought with it; None means the server's own OpenAI configuration.
    """
    llm = llm or LLMConfig()
    if not openai_available(llm):
        return None

    import openai

    claim = claim if isinstance(claim, Claim) else Claim(text=str(claim))
    excerpt = (source_text or "")[:24000]
    # The abstract is passed separately and in full, so a source with nothing
    # but an abstract is still judgeable even when the fetched page was a stub.
    if len(excerpt.strip()) < 120 and len((abstract or "").strip()) < 120:
        return None

    model = model or llm.model_name()
    prompt = _judge_prompt(claim, reference_line, title, abstract, excerpt)

    try:
        client = openai.OpenAI(api_key=llm.key(), base_url=llm.endpoint())
        response = _complete(client, model, prompt, llm.spec)
    except Exception as exc:
        return MatchResult(
            engine="openai-error",
            verdict="unverified",
            reason=f"OpenAI judging unavailable ({type(exc).__name__}); used lexical scoring.",
        )

    choice = response.choices[0] if response.choices else None
    if choice is None or choice.finish_reason == "content_filter":
        return MatchResult(
            engine="openai-refusal",
            verdict="unverified",
            reason="The model declined to judge this reference.",
        )

    try:
        data = _parse_json(choice.message.content or "")
        if not isinstance(data, dict):
            return None
    except (ValueError, TypeError):
        return None

    return _result_from_payload(data, engine="openai")


def _result_from_payload(data: dict, engine: str) -> MatchResult:
    verdict = data.get("verdict", "unverified")
    if verdict not in VERDICTS:
        verdict = "unverified"
    quote = (data.get("evidence_quote") or "").strip()
    return MatchResult(
        engine=engine,
        verdict=verdict,
        score=float(data.get("confidence") or 0.0),
        reason=(data.get("reason") or "").strip(),
        # offset 0 (not -1) marks this as real page text, so the screenshot
        # step will treat the verbatim quote as a highlight target.
        best_passage=Passage(text=quote, score=1.0, offset=0) if quote else None,
    )


_UNJUDGED_REASON = (
    "This citing sentence could not be judged — the retrieved text was too thin "
    "to settle it, or the judging call did not come back. It is counted as "
    "unverified rather than dropped, so the reference's headline does not move "
    "depending on which calls happened to succeed."
)


# Verdicts that accuse the citing author of something, and so are not allowed
# to stand on a single reading. See `_second_look`.
_NEEDS_CONFIRMING = ("unrelated", "weak")


def _second_look(
    claim: Claim,
    abstract: str,
    source_text: str,
    title: str,
    reference_line: str,
    llm: LLMConfig | None = None,
) -> MatchResult | None:
    """Re-judge a harsh verdict against the cited work's own abstract.

    "Unrelated" and "weak" are accusations: they say the author cited something
    that does not say what they claimed. The commonest way to reach one wrongly
    is to judge against retrieved text that buries or misses the abstract — a
    publisher landing page, a bot-check stub, or forty pages of body text in
    which the one relevant paragraph never rose to the top. The abstract is the
    cited work stating its own subject and findings, so an accusation has to
    survive a reading of that before it is reported.

    Returns None when there is no second reading to be had: no abstract, or an
    abstract that is already the whole of what was judged the first time.
    """
    abstract = (abstract or "").strip()
    if len(abstract) < 120:
        return None
    if abstract == (source_text or "").strip():
        return None
    return openai_match(
        claim, abstract, title=title, reference_line=reference_line, abstract=abstract,
        llm=llm,
    )


def judge(
    claims: "str | list[str] | list[Claim]",
    source_text: str,
    title: str = "",
    abstract: str = "",
    reference_line: str = "",
    use_model: bool = True,
    max_claims: int = 6,
    llm: LLMConfig | None = None,
) -> MatchResult:
    """Judge every citing sentence separately, then roll up to one verdict.

    A reference cited in five places is making five different claims, and the
    cited work may back some and not others. Judging the five together — as a
    single concatenated blob — returns one verdict that is right about none of
    them, and quietly hides the one claim that was oversold. That mismatch is
    the whole finding, so each sentence gets its own judgement.

    The two tiers roll up differently, on purpose:

    * The **model tier** takes the *most concerning* per-claim verdict. When a
      model that has read both texts says a claim is not supported, that is
      evidence, and evidence about one claim is not cancelled by four others.
    * The **lexical tier** keeps its existing best-case headline. Low word
      overlap is not evidence of anything (see `_verdict_from_score`), so
      letting the weakest sentence set the headline would mean a heavily cited
      reference looks worse the more often it is cited — for no real reason.

    Before a harsh model verdict is reported it gets a second reading against
    the cited work's abstract; see `_second_look`.
    """
    claim_list = _as_claims(claims)
    lexical = lexical_match(claim_list, source_text, title=title, abstract=abstract)

    engine = active_engine(llm) if use_model else "lexical"
    if engine == "lexical" or not claim_list:
        return lexical

    capped = claim_list[:max_claims]

    # Indexed alongside `capped`, so a claim the model could not judge keeps its
    # place instead of vanishing. See below for why that matters.
    outcomes: list[MatchResult | None] = []
    failure: MatchResult | None = None
    reconsidered: set[int] = set()
    for index, claim in enumerate(capped):
        result = openai_match(
            claim, source_text, title=title, reference_line=reference_line,
            abstract=abstract, llm=llm,
        )
        if result is not None and "-" in result.engine:   # "<engine>-error" / "-refusal"
            failure = result
            result = None

        if result is not None and result.verdict in _NEEDS_CONFIRMING:
            again = _second_look(
                claim, abstract, source_text, title, reference_line, llm=llm
            )
            if (
                again is not None
                and "-" not in again.engine
                and _CONCERN.get(again.verdict, 0) < _CONCERN.get(result.verdict, 0)
            ):
                again.reason = (
                    f"{again.reason} (First read of the retrieved page said "
                    f"'{result.verdict}'; re-checked against the abstract, which "
                    "covers the claim more directly.)"
                )
                result = again
                reconsidered.add(index)

        outcomes.append(result)

    # Nothing usable came back: the lexical verdict stands, with the reason why.
    if not any(outcomes):
        if failure is not None:
            lexical.reason = f"{lexical.reason} {failure.reason}".strip()
        return lexical

    # A claim the model could not return on still happened, and the headline is
    # the most concerning claim beneath it. Dropping the unjudged ones would let
    # the headline depend on how many calls came back rather than on what the
    # source says — the same reference judged twice reading "unrelated" once and
    # "related" the next time, because on the second run the claim that scored
    # worst was the one whose call failed. They are kept, as what they are.
    per_claim = [
        ClaimVerdict(
            claim=claim.text,
            verdict=result.verdict if result else "unverified",
            score=round(result.score, 3) if result else 0.0,
            reason=result.reason if result else _UNJUDGED_REASON,
            evidence_quote=(
                result.best_passage.text if result and result.best_passage else ""
            ),
            page=claim.page,
            context=claim.context if claim.context != claim.text else "",
            reconsidered=index in reconsidered,
        )
        for index, (claim, result) in enumerate(zip(capped, outcomes))
    ]

    worst = max(per_claim, key=lambda c: _CONCERN.get(c.verdict, 0))
    out = MatchResult(
        engine=next(r.engine for r in outcomes if r is not None),
        verdict=worst.verdict,
        score=worst.score,
        reason=_rollup_reason(worst.reason, per_claim, len(claim_list), len(capped)),
        claim_verdicts=per_claim,
        matched_claim=worst.claim,
    )

    # Keep the lexical passages — they carry the page offsets the screenshot
    # step needs, which a verbatim quote alone does not provide. Every claim's
    # quote is a candidate highlight, best-first by concern.
    out.shared_terms = lexical.shared_terms
    quotes = [
        Passage(text=c.evidence_quote, score=1.0, offset=0)
        for c in sorted(per_claim, key=lambda c: -_CONCERN.get(c.verdict, 0))
        if c.evidence_quote
    ]
    out.best_passage = quotes[0] if quotes else lexical.best_passage
    out.passages = quotes + lexical.passages
    return out


def _rollup_reason(
    reason: str,
    per_claim: list[ClaimVerdict],
    total_claims: int,
    judged_claims: int,
) -> str:
    """Explain the headline verdict without hiding the claims that were fine."""
    if len(per_claim) <= 1:
        base = reason
    else:
        tally = {}
        for claim in per_claim:
            tally[claim.verdict] = tally.get(claim.verdict, 0) + 1
        summary = ", ".join(f"{n} {verdict}" for verdict, n in sorted(
            tally.items(), key=lambda kv: -_CONCERN.get(kv[0], 0)
        ))
        base = (
            f"Judged separately for each of the {len(per_claim)} places this "
            f"reference is cited ({summary}). Most serious: {reason}"
        )
    if judged_claims < total_claims:
        base += (
            f" Only the first {judged_claims} of {total_claims} citing sentences "
            "were judged individually."
        )
    revisited = sum(1 for claim in per_claim if claim.reconsidered)
    if revisited:
        base += (
            f" {revisited} verdict{'s were' if revisited != 1 else ' was'} softened "
            "after re-reading the cited work's abstract."
        )
    return base
