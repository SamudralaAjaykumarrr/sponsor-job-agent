from dataclasses import dataclass
from enum import Enum
from typing import Optional

NON_FULL_TIME_SIGNALS = [
    "part-time", "part time", "internship", "intern position", "contract-to-hire",
    "contractor", "temporary position", "seasonal", "1099", "c2c", "corp-to-corp",
]


def is_full_time(employment_type_raw: str, description: str) -> bool:
    """Full-time-only per CLAUDE.md search rules. Conservative: only rejects on
    an EXPLICIT non-full-time signal; ambiguous/missing employment-type data is
    treated as full-time rather than rejected (consistent with the salary rule
    of never rejecting for missing information). Used by the discovery-time
    filter (app.agent.cycle) -- deliberately unchanged by Phase 8. For the
    application EXECUTOR's stricter positive-classification gate, see
    classify_employment_type() below."""
    combined = f"{employment_type_raw} {description}".lower()
    return not any(signal in combined for signal in NON_FULL_TIME_SIGNALS)


# --- Phase 8 (CLAUDE.md Phase 8 section 1): positive classification for the
# application executor's hard gate. Unlike is_full_time() above (permissive:
# "not explicitly non-full-time"), the executor must never auto-submit
# without an EXPLICIT FULL_TIME signal -- an ambiguous/silent job is UNKNOWN,
# never FULL_TIME, here.
from app.models import EmploymentType  # noqa: E402

_STRUCTURED_FULL_TIME_TOKENS = ("full_time", "full-time", "fulltime", "full time", "permanent")

# Ordered so a more specific/severe signal is checked before a weaker one
# that might also appear in the same text (e.g. "contract-to-hire" contains
# "contract").
_NEGATIVE_TYPE_SIGNALS: list[tuple[EmploymentType, tuple[str, ...]]] = [
    (EmploymentType.C2C, ("c2c", "corp-to-corp", "corp to corp")),
    (EmploymentType.INTERNSHIP, ("internship", "intern position", "co-op", "coop position")),
    (EmploymentType.TEMPORARY, ("temporary position", "temp position", "temporary role")),
    (EmploymentType.SEASONAL, ("seasonal",)),
    (EmploymentType.FREELANCE, ("freelance", "1099")),
    (EmploymentType.PART_TIME, ("part-time", "part time", "parttime")),
    (EmploymentType.CONTRACT, (
        "contract-to-hire", "contract to hire", "contractor", "w2 contract",
        "contract position", "contract role", "this is a contract", "contract",
    )),
]


def _scan_signal(text: str) -> Optional[EmploymentType]:
    """Shared token scan used by BOTH classify_employment_type() (unchanged
    contract, below) and the newer resolve_employment_type_evidence() --
    the one and only place negative/positive keyword tokens are matched, so
    the two functions can never silently diverge on what counts as a
    signal. Returns None (no signal / abstain), never a guess."""
    lower = (text or "").strip().lower()
    if not lower:
        return None
    # Real bug caught live during canary-candidate preflight: the bare
    # "contract" catch-all token (last entry of the CONTRACT tuple, meant to
    # cover a raw structured field or a plain-English "the role is a
    # contract" sentence) also fires on "smart contract(s)" -- ordinary
    # blockchain/crypto-industry terminology with nothing to do with
    # employment type, e.g. a Web3 team's JD saying "familiarity with smart
    # contracts" silently misclassified an otherwise-ordinary full-time
    # corporate posting as CONTRACT. Strip that specific phrase before the
    # scan; every other "contract" phrase (raw "Contract", "contract
    # position", "the role is a contract", etc.) is unaffected.
    lower = lower.replace("smart contract", "")
    for etype, tokens in _NEGATIVE_TYPE_SIGNALS:
        if any(tok in lower for tok in tokens):
            return etype
    if any(tok in lower for tok in _STRUCTURED_FULL_TIME_TOKENS):
        return EmploymentType.FULL_TIME
    return None


def classify_employment_type(employment_type_raw: str, title: str = "", description: str = "") -> EmploymentType:
    """Positive employment-type classification for the Phase 8 executor gate.

    Order of evidence: an explicit structured `employment_type_raw` value is
    trusted first (a provider that reports one is the strongest signal we
    have and is never fabricated -- see app/providers/*). Only when that is
    empty/ambiguous do we fall back to scanning title+description text, and
    only for an EXPLICIT signal either way. Silence in every source is
    UNKNOWN, never FULL_TIME -- "if employment type is unknown: do not
    auto-submit" (CLAUDE.md Phase 8 section 1).

    UNCHANGED behavior/signature since Phase 8 -- every existing caller
    (eligibility.py, approval.py, doctor.py, pipeline_dashboard.py,
    canary_feasibility.py's fallback) keeps depending on exactly this
    contract. Employment Type Evidence Hardening V1 adds a separate, richer
    function below (resolve_employment_type_evidence) rather than changing
    this one, precisely so nothing here is disturbed."""
    raw_signal = _scan_signal(employment_type_raw)
    if raw_signal is not None:
        return raw_signal
    text_signal = _scan_signal(f"{title or ''} {description or ''}")
    return text_signal if text_signal is not None else EmploymentType.UNKNOWN


# --- Employment Type Evidence Hardening V1 --------------------------------
#
# Adds a THIRD independent evidence source -- a job's real public posting
# page's schema.org JobPosting JSON-LD `employmentType` field (see
# app.applications.employment_type_evidence for the bounded, read-only fetch
# + parse) -- alongside the two classify_employment_type() already reads
# (provider structured field, JD text), with explicit provenance and
# deterministic conflict handling. classify_employment_type() itself is
# UNCHANGED (see docstring above); this is purely additive.

class EmploymentTypeEvidenceSource(str, Enum):
    JD_TEXT = "JD_TEXT"
    PROVIDER_STRUCTURED = "PROVIDER_STRUCTURED"
    STRUCTURED_PAGE_JSONLD = "STRUCTURED_PAGE_JSONLD"
    CONFLICT = "CONFLICT"
    NONE = "NONE"


@dataclass(frozen=True)
class EmploymentTypeDecision:
    """Provenance-carrying employment-type decision: WHAT was decided, WHICH
    source(s) decided it, the raw evidence value behind it, and a
    human-readable reason -- never just a bare enum. `value` uses the same
    app.models.EmploymentType this whole project already uses everywhere, so
    nothing downstream needs a second enum."""
    value: EmploymentType
    source: EmploymentTypeEvidenceSource
    raw_value: str
    reason: str


# schema.org's JobPosting.employmentType enumeration
# (https://schema.org/employmentType). VOLUNTEER/PER_DIEM/OTHER have no
# corresponding app.models.EmploymentType value and are deliberately left
# unmapped (None = no signal) rather than approximated into an unrelated
# type.
_JSONLD_EMPLOYMENT_TYPE_MAP: dict[str, Optional[EmploymentType]] = {
    "FULL_TIME": EmploymentType.FULL_TIME,
    "FULLTIME": EmploymentType.FULL_TIME,
    "PART_TIME": EmploymentType.PART_TIME,
    "PARTTIME": EmploymentType.PART_TIME,
    "CONTRACTOR": EmploymentType.CONTRACT,
    "CONTRACT": EmploymentType.CONTRACT,
    "TEMPORARY": EmploymentType.TEMPORARY,
    "SEASONAL": EmploymentType.SEASONAL,
    "INTERN": EmploymentType.INTERNSHIP,
    "INTERNSHIP": EmploymentType.INTERNSHIP,
    "VOLUNTEER": None,
    "PER_DIEM": None,
    "PERDIEM": None,
    "OTHER": None,
}


def normalize_structured_page_employment_type(raw_value: str) -> Optional[EmploymentType]:
    """Normalizes a genuine schema.org JobPosting.employmentType STRUCTURED
    FIELD VALUE (never arbitrary page text -- the caller is responsible for
    only ever passing the isolated JSON-LD field value, see
    app.applications.employment_type_evidence.extract_jobposting_employment_type)
    into app.models.EmploymentType. Returns None for empty/unrecognized
    input -- never a guess."""
    if not raw_value:
        return None
    key = raw_value.strip().upper().replace(" ", "_").replace("-", "_")
    return _JSONLD_EMPLOYMENT_TYPE_MAP.get(key)


def resolve_employment_type_evidence(
    employment_type_raw: str,
    title: str = "",
    description: str = "",
    structured_page_value: str = "",
) -> EmploymentTypeDecision:
    """Evidence-based FULL_TIME resolution (Employment Type Evidence
    Hardening V1). Combines three independent, genuinely-sourced signals --
    explicit JD text, the provider's own structured employment-type field,
    and a JobPosting JSON-LD `employmentType` value read from the real
    posting page -- under one safety-first policy: ANY explicit NEGATIVE
    signal (CONTRACT/PART_TIME/C2C/TEMPORARY/SEASONAL/FREELANCE/
    INTERNSHIP), from ANY source, wins outright and is never overridden by
    a positive FULL_TIME signal from another source. This is CLAUDE.md
    conflict rule D's explicitly-sanctioned "safer negative" alternative to
    a bare CONFLICT/UNKNOWN result -- and, empirically, the ONLY policy
    consistent with this project's own pre-existing tests (e.g.
    tests/test_canary_feasibility.py::test_contract_employment_type_rejects
    requires a structured `employment_type="contract"` field to reject even
    though the JD text used across that whole test file's fixture also
    contains generic "Full-time, permanent position" boilerplate -- an
    earlier version of this function that let JD text unconditionally
    decide, in either direction, broke that pre-existing test live).

    When more than one source votes negative but they disagree on the
    SPECIFIC subtype (e.g. JD text implies C2C while the provider field
    says plain "contract"), a deterministic source-priority tie-break
    (JD_TEXT > PROVIDER_STRUCTURED > STRUCTURED_PAGE_JSONLD) picks which
    one is reported -- it is never downgraded to UNKNOWN. That downgrade
    was tried and reverted after it broke a second pre-existing test
    (tests/test_apply_automation_settings.py::
    test_non_full_time_cannot_be_enabled_for_unattended_via_preferences):
    app.applications.eligibility hard_skips a SPECIFIC negative type but
    treats bare UNKNOWN as ASSIST-only/enters_queue=True, so silently
    turning "definitely not full-time, just unsure which flavor" into
    UNKNOWN would perversely WEAKEN the hard-skip gate, not strengthen it.

    EmploymentTypeEvidenceSource.CONFLICT remains a defined, documented
    outcome (matching CLAUDE.md's own "REVIEW REQUIRED / Conflicting
    employment-type evidence" UI example) but is not reachable under this
    policy today, precisely because the safer-negative alternative rule D
    itself sanctions is the one this project's tests actually require
    universally, not only for the one JD-vs-provider case rule D
    describes literally. A future, narrower conflict case could still use
    it without changing this function's public contract.

    Only when NO source votes negative does a lone or mutually-agreeing
    FULL_TIME signal (JD text, provider field, or JSON-LD page) produce
    FULL_TIME; no source voting anything at all produces UNKNOWN,
    source=NONE. Never infers FULL_TIME from salary, benefits, office
    location, company reputation, or a title merely lacking the word
    "contract" -- those are simply never inputs to this function at all."""
    jd_text = f"{title or ''} {description or ''}".strip()
    votes: list[tuple[EmploymentTypeEvidenceSource, str, Optional[EmploymentType]]] = [
        (EmploymentTypeEvidenceSource.JD_TEXT, jd_text, _scan_signal(jd_text)),
        (EmploymentTypeEvidenceSource.PROVIDER_STRUCTURED, (employment_type_raw or "").strip(),
         _scan_signal(employment_type_raw)),
        (EmploymentTypeEvidenceSource.STRUCTURED_PAGE_JSONLD, (structured_page_value or "").strip(),
         normalize_structured_page_employment_type(structured_page_value)),
    ]

    negative_votes = [(s, r, sig) for s, r, sig in votes if sig is not None and sig != EmploymentType.FULL_TIME]
    positive_votes = [(s, r, sig) for s, r, sig in votes if sig == EmploymentType.FULL_TIME]

    if negative_votes:
        src, raw, sig = negative_votes[0]
        agreeing = [s.value for s, _, sg in negative_votes if sg == sig]
        reason = f"explicit non-full-time signal ({sig.value}) from {'/'.join(agreeing)}"
        disagreeing = [(s, sg) for s, _, sg in negative_votes if sg != sig]
        if disagreeing:
            detail = "; ".join(f"{s.value}={sg.value}" for s, sg in disagreeing)
            reason += f"; other source(s) reported a different non-full-time subtype ({detail}) -- still non-FULL_TIME either way"
        if positive_votes:
            overridden = "/".join(s.value for s, _, _ in positive_votes)
            reason += f"; overrides a conflicting FULL_TIME signal from {overridden} (safer-negative policy)"
        return EmploymentTypeDecision(sig, src, raw, reason)

    if positive_votes:
        src, raw, sig = positive_votes[0]
        agreeing = "/".join(s.value for s, _, _ in positive_votes)
        return EmploymentTypeDecision(EmploymentType.FULL_TIME, src, raw,
                                       f"explicit FULL_TIME signal from {agreeing}")

    return EmploymentTypeDecision(
        EmploymentType.UNKNOWN, EmploymentTypeEvidenceSource.NONE, "",
        "no reliable employment-type evidence found (no explicit JD text, no provider structured field, "
        "no JobPosting JSON-LD)",
    )
