"""JD requirements extraction (CLAUDE.md Phase 14 sections 3-6, 18-20).

Extracts a normalized, evidence-spanned requirements list from a job
title/description: required vs preferred (section 4), negation/context aware
(section 5), years/education/certification/domain/responsibilities (sections
15-17, 12-13). This module never reads the candidate profile -- it only
describes the JD. Matching against verified evidence happens in
app.resume_optimizer.matching.
"""

import re

from app.resume_optimizer.models import (
    JDAnalysisResult,
    JDRequirementItem,
    RequirementCategory,
    RequirementPriority,
)

ANALYZER_VERSION = "jd-analysis-v2"

# (skill phrase, category) -- deliberately overlapping/synonymous with
# app.matching.skills.SKILL_VOCAB but categorized for diagnostics grouping
# (CLAUDE.md section 25). This is a keyword vocabulary only -- it never
# claims the candidate has any of these.
SKILL_VOCAB: list[tuple[str, RequirementCategory]] = [
    ("python", RequirementCategory.LANGUAGE),
    ("java", RequirementCategory.LANGUAGE),
    ("kotlin", RequirementCategory.LANGUAGE),
    ("go", RequirementCategory.LANGUAGE),
    ("golang", RequirementCategory.LANGUAGE),
    ("c++", RequirementCategory.LANGUAGE),
    ("c#", RequirementCategory.LANGUAGE),
    ("javascript", RequirementCategory.LANGUAGE),
    ("typescript", RequirementCategory.LANGUAGE),
    ("django", RequirementCategory.FRAMEWORK),
    ("flask", RequirementCategory.FRAMEWORK),
    ("fastapi", RequirementCategory.FRAMEWORK),
    ("spring", RequirementCategory.FRAMEWORK),
    ("spring boot", RequirementCategory.FRAMEWORK),
    ("node.js", RequirementCategory.FRAMEWORK),
    ("nodejs", RequirementCategory.FRAMEWORK),
    ("rest", RequirementCategory.BACKEND),
    ("rest api", RequirementCategory.BACKEND),
    ("rest apis", RequirementCategory.BACKEND),
    ("restful", RequirementCategory.BACKEND),
    ("graphql", RequirementCategory.BACKEND),
    ("grpc", RequirementCategory.BACKEND),
    ("microservices", RequirementCategory.ARCHITECTURE),
    ("distributed systems", RequirementCategory.ARCHITECTURE),
    ("system design", RequirementCategory.ARCHITECTURE),
    ("multithreading", RequirementCategory.ARCHITECTURE),
    ("concurrency", RequirementCategory.ARCHITECTURE),
    ("sql", RequirementCategory.DATABASE),
    ("postgresql", RequirementCategory.DATABASE),
    ("postgres", RequirementCategory.DATABASE),
    ("mysql", RequirementCategory.DATABASE),
    ("sqlite", RequirementCategory.DATABASE),
    ("mongodb", RequirementCategory.DATABASE),
    ("redis", RequirementCategory.DATABASE),
    ("dynamodb", RequirementCategory.DATABASE),
    ("cassandra", RequirementCategory.DATABASE),
    ("elasticsearch", RequirementCategory.DATABASE),
    ("aws", RequirementCategory.CLOUD),
    ("azure", RequirementCategory.CLOUD),
    ("gcp", RequirementCategory.CLOUD),
    ("google cloud", RequirementCategory.CLOUD),
    ("ec2", RequirementCategory.CLOUD),
    ("s3", RequirementCategory.CLOUD),
    ("lambda", RequirementCategory.CLOUD),
    ("cloudformation", RequirementCategory.CLOUD),
    ("docker", RequirementCategory.DEVOPS),
    ("kubernetes", RequirementCategory.DEVOPS),
    ("k8s", RequirementCategory.DEVOPS),
    ("terraform", RequirementCategory.DEVOPS),
    ("ansible", RequirementCategory.DEVOPS),
    ("jenkins", RequirementCategory.DEVOPS),
    ("ci/cd", RequirementCategory.DEVOPS),
    ("git", RequirementCategory.TOOL),
    ("github", RequirementCategory.TOOL),
    ("gitlab", RequirementCategory.TOOL),
    ("linux", RequirementCategory.TOOL),
    ("unix", RequirementCategory.TOOL),
    ("bash", RequirementCategory.TOOL),
    ("shell scripting", RequirementCategory.TOOL),
    ("kafka", RequirementCategory.MESSAGING),
    ("rabbitmq", RequirementCategory.MESSAGING),
    ("sqs", RequirementCategory.MESSAGING),
    ("sns", RequirementCategory.MESSAGING),
    ("unit testing", RequirementCategory.TESTING),
    ("pytest", RequirementCategory.TESTING),
    ("junit", RequirementCategory.TESTING),
    ("tdd", RequirementCategory.TESTING),
    ("bdd", RequirementCategory.TESTING),
    ("test automation", RequirementCategory.TESTING),
    # Testing terminology expansion (JD intelligence v3): QA/SDET-shaped JDs
    # commonly name specific test types/tools rather than the bare word
    # "testing" -- extracting them as their own requirement items (instead
    # of silently folding into the generic "testing" responsibility signal)
    # is what makes QA/SDET-role JDs classify and match meaningfully.
    ("integration testing", RequirementCategory.TESTING),
    ("end-to-end testing", RequirementCategory.TESTING),
    ("e2e testing", RequirementCategory.TESTING),
    ("regression testing", RequirementCategory.TESTING),
    ("performance testing", RequirementCategory.TESTING),
    ("load testing", RequirementCategory.TESTING),
    ("stress testing", RequirementCategory.TESTING),
    ("smoke testing", RequirementCategory.TESTING),
    ("acceptance testing", RequirementCategory.TESTING),
    ("manual testing", RequirementCategory.TESTING),
    ("exploratory testing", RequirementCategory.TESTING),
    ("test coverage", RequirementCategory.TESTING),
    ("test cases", RequirementCategory.TESTING),
    ("test plans", RequirementCategory.TESTING),
    ("quality assurance", RequirementCategory.TESTING),
    ("selenium", RequirementCategory.TESTING),
    ("cypress", RequirementCategory.TESTING),
    ("playwright", RequirementCategory.TESTING),
    ("postman", RequirementCategory.TESTING),
    ("cucumber", RequirementCategory.TESTING),
    ("soapui", RequirementCategory.TESTING),
    ("k6", RequirementCategory.TESTING),
    ("locust", RequirementCategory.TESTING),
    ("jmeter", RequirementCategory.TESTING),
    ("gatling", RequirementCategory.TESTING),
    ("loadrunner", RequirementCategory.TESTING),
    ("agile", RequirementCategory.METHODOLOGY),
    ("scrum", RequirementCategory.METHODOLOGY),
    ("api design", RequirementCategory.BACKEND),
    ("html", RequirementCategory.FRONTEND),
    ("css", RequirementCategory.FRONTEND),
    ("react", RequirementCategory.FRONTEND),
    ("angular", RequirementCategory.FRONTEND),
    ("vue", RequirementCategory.FRONTEND),
    ("machine learning", RequirementCategory.DATA_ML),
    ("data pipelines", RequirementCategory.DATA_ML),
    ("etl", RequirementCategory.DATA_ML),
    ("spark", RequirementCategory.DATA_ML),
    ("hadoop", RequirementCategory.DATA_ML),
    ("oauth", RequirementCategory.SECURITY),
    ("jwt", RequirementCategory.SECURITY),
    ("security", RequirementCategory.SECURITY),
    # Post-release bug fix: kept OUT of DEVOPS deliberately. Docker/
    # Kubernetes/Terraform are container/IaC tools, not monitoring
    # evidence -- sharing a category with them let a candidate with only
    # container-deployment experience get an unearned TRANSFERABLE
    # "observability" claim (a real Airbnb Payments JD caught this). See
    # RequirementCategory.OBSERVABILITY / TRANSFERABLE_ELIGIBLE_CATEGORIES.
    ("monitoring", RequirementCategory.OBSERVABILITY),
    ("observability", RequirementCategory.OBSERVABILITY),
    ("grafana", RequirementCategory.OBSERVABILITY),
    ("prometheus", RequirementCategory.OBSERVABILITY),
    # Observability terminology expansion (JD intelligence v3): more of the
    # concrete tool/practice names JDs actually use, kept in the same
    # OBSERVABILITY category (deliberately excluded from
    # TRANSFERABLE_ELIGIBLE_CATEGORIES, same rationale as the existing
    # monitoring/grafana/prometheus entries -- see models.py).
    ("datadog", RequirementCategory.OBSERVABILITY),
    ("new relic", RequirementCategory.OBSERVABILITY),
    ("splunk", RequirementCategory.OBSERVABILITY),
    ("elk stack", RequirementCategory.OBSERVABILITY),
    ("logstash", RequirementCategory.OBSERVABILITY),
    ("kibana", RequirementCategory.OBSERVABILITY),
    ("opentelemetry", RequirementCategory.OBSERVABILITY),
    ("jaeger", RequirementCategory.OBSERVABILITY),
    ("zipkin", RequirementCategory.OBSERVABILITY),
    ("cloudwatch", RequirementCategory.OBSERVABILITY),
    ("distributed tracing", RequirementCategory.OBSERVABILITY),
    ("log aggregation", RequirementCategory.OBSERVABILITY),
    ("pagerduty", RequirementCategory.OBSERVABILITY),
    ("sentry", RequirementCategory.OBSERVABILITY),
    ("site reliability", RequirementCategory.OBSERVABILITY),
    ("sre", RequirementCategory.OBSERVABILITY),
    ("alerting", RequirementCategory.OBSERVABILITY),
    # AI/ML terminology (role-aware "AI/backend when evidence supports it" --
    # CLAUDE.md JD intelligence v3): kept in the existing DATA_ML category,
    # never claimed unless directly verified (DATA_ML stays TRANSFERABLE-
    # eligible only via genuinely analogous verified data/ML work).
    ("ai", RequirementCategory.DATA_ML),
    ("artificial intelligence", RequirementCategory.DATA_ML),
    ("llm", RequirementCategory.DATA_ML),
    ("nlp", RequirementCategory.DATA_ML),
    ("generative ai", RequirementCategory.DATA_ML),
]

# Fast lookups built once from SKILL_VOCAB -- used by alternative-group
# detection and by the longest-phrase-wins overlap dedup in
# _extract_skill_requirements (CLAUDE.md post-release bug-fix section:
# "deduplicate semantically duplicated requirements").
_SKILL_VOCAB_CATEGORY: dict[str, RequirementCategory] = {phrase: category for phrase, category in SKILL_VOCAB}
_SKILL_VOCAB_BY_LENGTH_DESC: list[tuple[str, RequirementCategory]] = sorted(SKILL_VOCAB, key=lambda pc: -len(pc[0]))

RESPONSIBILITY_SIGNALS = [
    "rest apis", "distributed systems", "message queues", "message queue",
    "sql optimization", "query optimization", "ci/cd", "continuous integration",
    "cloud deployment", "debugging", "testing", "code review", "on-call",
    "mentoring", "architecture", "scalability", "performance", "production",
    "incident", "cross-functional", "design and implement", "build and maintain",
    # Responsibility extraction expansion (JD intelligence v3).
    "root cause analysis", "capacity planning", "performance tuning",
    "database design", "api integration", "third-party integrations",
    "requirements gathering", "documentation", "technical design",
    "code quality", "post-mortem", "incident response", "troubleshooting",
    "root cause", "roadmap", "stakeholder", "collaborate with",
    "automated testing", "deployment automation", "infrastructure as code",
]

DOMAIN_SIGNALS = [
    "payments", "banking", "fintech", "financial services", "healthcare", "health tech",
    "retail", "e-commerce", "ecommerce", "insurance", "logistics", "supply chain",
    "gaming", "adtech", "advertising", "media", "streaming", "cybersecurity",
    "biotech", "life sciences", "government", "public sector", "education", "edtech",
    "real estate", "travel", "telecom", "energy", "manufacturing",
    # Domain extraction expansion (JD intelligence v3): fraud/risk is a
    # common payments-adjacent domain signal (e.g. "fraud detection" nice-
    # to-have on a payments JD) distinct from the ML/DATA_ML skill category
    # itself -- purely informational, never a blocker either way.
    "fraud", "risk management",
]

# Post-release bug fix (real Airbnb Payments JD): a JD commonly says
# "Payments" while a candidate's own verified bullets describe the same
# domain with the singular/adjectival form ("payment routing", "payment-
# processing systems") -- a bare literal-word match on "payments" (plural
# only) missed genuine payments-domain evidence on the candidate side and
# reported Domain alignment = NO_EVIDENCE despite real evidence existing.
# This is a general singular/plural pattern override, not anything specific
# to Airbnb or any one candidate -- applied identically to both the JD-side
# and candidate-side domain scan (see `domain_signal_present`).
_DOMAIN_SIGNAL_PATTERN_OVERRIDES: dict[str, str] = {
    "payments": r"\bpayments?\b",
}


def _domain_pattern(domain: str) -> str:
    return _DOMAIN_SIGNAL_PATTERN_OVERRIDES.get(domain, r"\b" + re.escape(domain) + r"\b")


def domain_signal_present(text_lower: str, domain: str) -> bool:
    """Shared by JD-side (`_extract_domain_signals`) and candidate-side
    (`app.resume_optimizer.evidence.build_evidence_graph`) domain scanning so
    both sides recognize the same singular/plural forms consistently."""
    return bool(re.search(_domain_pattern(domain), text_lower))


_REQUIRED_MARKERS = [
    "required qualifications", "required skills", "requirements:", "required:", "must have",
    "must-have", "minimum qualifications", "minimum requirements", "you have",
    "you must have", "basic qualifications", "what you'll need", "what you need",
]
_PREFERRED_MARKERS = [
    "preferred qualifications", "preferred skills", "preferred:", "nice to have", "nice-to-have",
    "bonus points", "bonus if", "desired", "a plus", "pluses", "would be great",
    "ideally you", "it's a plus", "preferably", "familiarity with", "exposure to",
    "huge plus", "big plus", "great plus", "not required but",
]

_NEGATION_WINDOW_CHARS = 60
_NEGATION_PATTERNS = [
    r"\bnot required\b", r"\bnot necessary\b", r"\bno experience necessary\b",
    r"\bnot mandatory\b", r"\bisn't required\b", r"\bis not required\b",
    r"\bnot a requirement\b", r"\bwithout .{0,20}experience\b",
]
_CONDITIONAL_PATTERNS = [
    r"\bcase.by.case\b", r"\bmay be considered\b", r"\bdepending on\b",
    r"\bif available\b", r"\bwhere applicable\b",
    # Education extraction expansion (JD intelligence v3): "...or equivalent
    # (practical) experience" is a genuinely conditional qualifier -- the
    # degree requirement is softened, not dropped -- same semantics as the
    # existing "may be considered depending on" patterns above.
    r"\bor equivalent (?:practical )?experience\b",
]

_EDU_PATTERNS = [
    (r"\bph\.?d\.?\b", "PhD"),
    (r"\bmaster'?s? degree\b", "Master's degree"),
    (r"\bm\.?s\.?\b(?!\w)", "Master's degree"),
    (r"\bbachelor'?s? degree\b", "Bachelor's degree"),
    (r"\bb\.?a\.?\b(?!\w)", "Bachelor's degree"),
    (r"\bb\.?s\.?\b(?!\w)", "Bachelor's degree"),
    (r"\bcomputer science degree\b", "Computer Science degree"),
]

_CERT_PATTERNS = [
    r"\baws certified(?: [a-z0-9]{2,20}){0,3}",
    r"\bazure certified(?: [a-z0-9]{2,20}){0,3}",
    r"\bcertified kubernetes(?: [a-z0-9]{2,20}){0,3}",
    r"\bpmp\b",
    r"\bcissp\b",
    r"\bcisa\b",
    r"\bcompt[i1]a(?: [a-z0-9\+]{2,20}){0,3}",
    r"\b[a-z][a-z0-9]* certification\b",
    r"\bcertified(?: [a-z0-9]{2,20}){1,3}",
]

# CLAUDE.md section 5 applied to certification extraction too: a broad
# "certified <word> <word> <word>" match commonly runs on into surrounding
# sentence filler ("...certified developer is a plus") -- trailing filler
# words are trimmed off the captured span rather than kept as part of the
# certification name.
_CERT_TRAILING_STOPWORDS = {
    "is", "a", "an", "the", "and", "or", "plus", "preferred", "required", "nice", "to",
    "have", "with", "who", "that", "you", "your", "for", "of", "in", "on",
}


def _trim_cert_label(label: str) -> str:
    words = label.split()
    while words and words[-1].lower() in _CERT_TRAILING_STOPWORDS:
        words.pop()
    return " ".join(words)

_YEARS_PATTERNS = [
    # Range first ("3-5 years", "3 to 5 years") so a range's lower bound is
    # never mistaken for a lone hyphenated-adjective match below.
    re.compile(r"(\d+)\s*\+?\s*(?:-|to)\s*(\d+)\s*\+?\s*years?", re.IGNORECASE),
    # Plural/plus form ("5+ years", "5 years").
    re.compile(r"(\d+)\s*\+?\s*years\b", re.IGNORECASE),
    # Years intelligence v3: hyphenated singular adjective form ("7-year
    # requirement", "7-year software engineering experience") -- a real JD
    # phrasing the plural-only pattern above never matched.
    re.compile(r"(\d+)\s*\+?\s*-\s*year\b", re.IGNORECASE),
]


def _clause_window(text: str, match_start: int, match_end: int, max_chars: int) -> str:
    """Bounds a local text window to the current CLAUSE (stops at the
    nearest '.'/';' boundary in either direction) so a negation/conditional/
    priority phrase in an ADJACENT sentence is never picked up as if it
    applied to this match -- a real bug caught by this phase's own JD
    extraction acceptance tests ('Java is not required. Python is
    required.' was incorrectly negating 'Python' with a blind character-
    count window before this fix)."""
    back_stop = 0
    idx = text.rfind(".", 0, match_start)
    if idx == -1:
        idx = text.rfind(";", 0, match_start)
    if idx != -1:
        back_stop = idx + 1
    fwd_stop = len(text)
    idx = text.find(".", match_end)
    if idx == -1:
        idx = text.find(";", match_end)
    if idx != -1:
        fwd_stop = idx
    start = max(back_stop, match_start - max_chars)
    end = min(fwd_stop, match_end + max_chars)
    return text[start:end]


def _is_negated(text: str, match_start: int, match_end: int | None = None) -> bool:
    window = _clause_window(text, match_start, match_end if match_end is not None else match_start, _NEGATION_WINDOW_CHARS)
    return any(re.search(p, window, re.IGNORECASE) for p in _NEGATION_PATTERNS)


def _is_conditional(text: str, match_start: int, match_end: int | None = None) -> bool:
    window = _clause_window(text, match_start, match_end if match_end is not None else match_start, _NEGATION_WINDOW_CHARS)
    return any(re.search(p, window, re.IGNORECASE) for p in _CONDITIONAL_PATTERNS)


# Post-release bug fix (real Airbnb Payments JD): 45 chars was too small to
# reach a trailing preference phrase across a parenthetical aside --
# "React (or any equivalent JS library) would be nice to have." has ~60
# chars between the matched term and "nice to have". Widened to 80; the
# clause-boundary stop ('.'/';'/':' in `_local_priority_override` itself)
# still prevents this from reaching into an unrelated adjacent sentence.
_LOCAL_WINDOW_CHARS = 80
_LOCAL_REQUIRED_PHRASES = [r"\bis required\b", r"\brequired\b", r"\bmust\b", r"\bmandatory\b"]
# Post-release bug fix (real Airbnb Payments JD): "preferably", "familiarity",
# "exposure", "huge/big plus", and hyphenated "nice-to-have" were previously
# missing from this list, so a preference phrase right next to the matched
# term fell through to the (REQUIRED-by-default) section priority instead of
# overriding it -- e.g. "(preferably Java/Kotlin/Python)" and "would be nice
# to have" were both silently treated as REQUIRED. `\bplus\b` alone
# (replacing the narrower "a plus"/"is a plus") catches "a plus", "huge
# plus", "big plus", etc. without needing to enumerate every intensifier.
_LOCAL_PREFERRED_PHRASES = [
    r"\bplus\b", r"\bpreferred\b", r"\bnice[- ]to[- ]have\b", r"\bbonus\b",
    r"\boptional\b", r"\bideally\b", r"\bdesired\b", r"\bpreferably\b",
    r"\bfamiliarity\b", r"\bfamiliar with\b", r"\bexposure\b",
]


def _negated_after_override(
    text_lower: str, start: int, end: int, priority_override: "RequirementPriority | None",
) -> bool:
    """A local preference phrase (e.g. "would be a huge plus, but not
    required") always wins over a co-occurring hard-negation phrase --
    "not required" there means "optional", not "this JD item does not
    exist at all" (the case the negation patterns exist for, e.g. "Java is
    not required. Python is required."). Only treat a match as hard-negated
    (dropped entirely) when no preference phrase is already overriding it
    to PREFERRED. Real Airbnb Payments JD regression: "huge plus, but not
    required" was previously dropped entirely instead of kept as PREFERRED."""
    if priority_override == RequirementPriority.PREFERRED:
        return False
    return _is_negated(text_lower, start, end)


def _local_priority_override(text_lower: str, match_start: int, match_end: int) -> RequirementPriority | None:
    """A local required/preferred phrase right next to the specific matched
    term (CLAUDE.md section 4) is a more precise signal than a distant
    section header and overrides it -- e.g. 'AWS Certified Developer is a
    plus' inside an otherwise-Required section is still preferred. Bounded
    to the current CLAUSE (stops at the nearest '.'/';'/':' boundary) so a
    comma-separated list item never picks up the NEXT sentence's/section's
    marker just because it happens to fall within a fixed character count."""
    fwd_stop = len(text_lower)
    for ch in (".", ";", ":"):
        idx = text_lower.find(ch, match_end)
        if idx != -1:
            fwd_stop = min(fwd_stop, idx)
    fwd_window = text_lower[match_end:min(fwd_stop, match_end + _LOCAL_WINDOW_CHARS)]

    back_stop = 0
    for ch in (".", ";", ":"):
        idx = text_lower.rfind(ch, 0, match_start)
        if idx != -1:
            back_stop = max(back_stop, idx + 1)
    back_window = text_lower[max(back_stop, match_start - _LOCAL_WINDOW_CHARS):match_start]

    combined = back_window + " " + fwd_window
    if any(re.search(p, combined) for p in _LOCAL_PREFERRED_PHRASES):
        return RequirementPriority.PREFERRED
    if any(re.search(p, combined) for p in _LOCAL_REQUIRED_PHRASES):
        return RequirementPriority.REQUIRED
    return None


def _section_priority_for_offset(text_lower: str, offset: int) -> RequirementPriority:
    """CLAUDE.md section 4: whichever marker (required/preferred) most
    recently precedes this offset determines the section. Defaults to
    REQUIRED when no section marker precedes it at all -- a bare skill
    mention outside any explicit section is treated conservatively as
    required rather than silently dropped from coverage counting."""
    last_required_idx = max((text_lower.rfind(m, 0, offset) for m in _REQUIRED_MARKERS), default=-1)
    last_preferred_idx = max((text_lower.rfind(m, 0, offset) for m in _PREFERRED_MARKERS), default=-1)
    if last_preferred_idx > last_required_idx:
        return RequirementPriority.PREFERRED
    return RequirementPriority.REQUIRED


def _extract_years(text: str) -> float | None:
    for pattern in _YEARS_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if _is_negated(text, m.start(), m.end()):
            continue
        groups = m.groups()
        if len(groups) == 2:
            return float(groups[0])
        return float(groups[0])
    return None


# Post-release bug fix (real Airbnb Payments JD): "Proficient in at least
# one major programming language (preferably Java/Kotlin/Python)" must
# become ONE alternative requirement (satisfied by ANY verified alternative),
# never three separate mandatory REQUIRED items. Matches a slash-separated
# run of 2+ tokens (e.g. "Java/Kotlin/Python", "AWS/Azure/GCP",
# "SQL/PostgreSQL/MySQL") where at least 2 of the tokens are recognized
# SKILL_VOCAB phrases -- a single recognized token in a slash list (e.g.
# "Node.js/Express" where only "node.js" is vocab) is left to the ordinary
# single-term extraction path instead of being force-grouped.
_ALT_GROUP_PATTERN = re.compile(r"\b[a-z][a-z0-9+#.]*(?:\s*/\s*[a-z][a-z0-9+#.]*)+\b")

# Alternative requirements expansion (JD intelligence v3): the same "one
# requirement satisfied by any verified alternative" semantics as the
# slash-group pattern above, for the equally common prose form "Java OR
# Kotlin OR Python" / "Java, Kotlin, or Python". Requires >=1 comma-joined
# lead token(s) followed by >=1 "or <token>" tail so a lone, unrelated
# "X or Y" in ordinary prose is captured too -- but exactly like the slash
# pattern, it is only ever promoted to an alternative-group ITEM when >=2 of
# its tokens are recognized SKILL_VOCAB phrases (see the `recognized`
# guard in `_build_alt_item` below), so "review code or documentation"
# never becomes a fabricated alternative-requirement group.
_ALT_OR_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+#.]*(?:\s*,\s*[a-z][a-z0-9+#.]*)*(?:\s*,?\s+or\s+[a-z][a-z0-9+#.]*)+\b",
    re.IGNORECASE,
)


def _build_alt_item(text: str, text_lower: str, start: int, end: int, tokens: list[str]) -> JDRequirementItem | None:
    recognized = [(t, _SKILL_VOCAB_CATEGORY[t]) for t in tokens if t in _SKILL_VOCAB_CATEGORY]
    if len(recognized) < 2:
        return None
    priority_override = _local_priority_override(text_lower, start, end)
    if _negated_after_override(text_lower, start, end, priority_override):
        return None
    conditional = _is_conditional(text_lower, start, end)
    priority = priority_override or _section_priority_for_offset(text_lower, start)
    categories = [c for _, c in recognized]
    category = max(set(categories), key=categories.count)
    alt_names = [t for t, _ in recognized]
    span_start = max(0, start - 40)
    span_end = min(len(text), end + 40)
    return JDRequirementItem(
        text=text[start:end], normalized_value="|".join(alt_names), category=category,
        priority=priority, evidence_span=text[span_start:span_end].strip(),
        confidence=0.9 if not conditional else 0.6, conditional=conditional, alternatives=alt_names,
    )


def _extract_alternative_groups(text: str, text_lower: str) -> tuple[list[JDRequirementItem], list[tuple[int, int]]]:
    items: list[JDRequirementItem] = []
    consumed: list[tuple[int, int]] = []

    for m in _ALT_GROUP_PATTERN.finditer(text_lower):
        tokens = [t.strip() for t in m.group(0).split("/")]
        item = _build_alt_item(text, text_lower, m.start(), m.end(), tokens)
        if item is None:
            continue
        consumed.append((m.start(), m.end()))
        items.append(item)

    for m in _ALT_OR_PATTERN.finditer(text_lower):
        # Never re-consume a span already claimed by the slash pattern (or
        # by an earlier OR-pattern match) above.
        if any(s <= m.start() and m.end() <= e for s, e in consumed):
            continue
        tokens = [t.strip() for t in re.split(r",|\bor\b", m.group(0)) if t.strip()]
        item = _build_alt_item(text, text_lower, m.start(), m.end(), tokens)
        if item is None:
            continue
        consumed.append((m.start(), m.end()))
        items.append(item)

    return items, consumed


def _extract_skill_requirements(
    text: str, text_lower: str, consumed_spans: list[tuple[int, int]] = (),
) -> list[JDRequirementItem]:
    items: list[JDRequirementItem] = []
    # Post-release bug fix: iterate longest phrase first and track occupied
    # spans so an overlapping shorter phrase (e.g. "rest" inside an
    # already-matched "rest apis") never produces a second, semantically
    # duplicated requirement for the same JD text -- "deduplicate
    # semantically duplicated requirements". Spans already claimed by an
    # alternative group (e.g. "python" inside "Java/Kotlin/Python") are
    # excluded here too, so that term is never ALSO extracted as its own
    # separate mandatory requirement.
    occupied: list[tuple[int, int]] = list(consumed_spans)
    for phrase, category in _SKILL_VOCAB_BY_LENGTH_DESC:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        for m in re.finditer(pattern, text_lower):
            if any(s <= m.start() and m.end() <= e for s, e in occupied):
                continue
            priority_override = _local_priority_override(text_lower, m.start(), m.end())
            if _negated_after_override(text_lower, m.start(), m.end(), priority_override):
                continue
            conditional = _is_conditional(text_lower, m.start(), m.end())
            priority = priority_override or _section_priority_for_offset(text_lower, m.start())
            span_start = max(0, m.start() - 40)
            span_end = min(len(text), m.end() + 40)
            items.append(JDRequirementItem(
                text=phrase, normalized_value=phrase, category=category,
                priority=priority, evidence_span=text[span_start:span_end].strip(),
                confidence=0.9 if not conditional else 0.6, conditional=conditional,
            ))
            occupied.append((m.start(), m.end()))
            break  # one requirement item per distinct skill phrase per JD
    return items


def _extract_education(text_lower: str) -> tuple[list[str], list[JDRequirementItem]]:
    found: list[str] = []
    items: list[JDRequirementItem] = []
    for pattern, label in _EDU_PATTERNS:
        m = re.search(pattern, text_lower)
        if m and not _is_negated(text_lower, m.start(), m.end()) and label not in found:
            found.append(label)
            priority = _local_priority_override(text_lower, m.start(), m.end()) or _section_priority_for_offset(text_lower, m.start())
            # Education extraction v3: "...or equivalent (practical)
            # experience" is a genuinely conditional qualifier on the degree
            # requirement (see _CONDITIONAL_PATTERNS) -- previously this
            # extractor never checked/set `conditional` at all, unlike every
            # other extraction function in this module.
            conditional = _is_conditional(text_lower, m.start(), m.end())
            items.append(JDRequirementItem(
                text=label, normalized_value=label, category=RequirementCategory.EDUCATION,
                priority=priority, evidence_span=text_lower[max(0, m.start() - 30):m.end() + 30].strip(),
                confidence=0.9 if not conditional else 0.6, conditional=conditional,
            ))
    return found, items


def _extract_certifications(text: str, text_lower: str) -> tuple[list[str], list[JDRequirementItem]]:
    found: list[str] = []
    found_word_sets: list[set[str]] = []
    items: list[JDRequirementItem] = []
    for pattern in _CERT_PATTERNS:
        for m in re.finditer(pattern, text_lower):
            if _is_negated(text_lower, m.start(), m.end()):
                continue
            raw_label = text[m.start():m.end()].strip()
            label = _trim_cert_label(raw_label)
            if not label:
                continue
            words = set(label.lower().split())
            # Dedup: skip if this span's words are a subset of an already-found
            # (broader or equal) certification -- e.g. "certified kubernetes
            # administrator" already covers "kubernetes administrator".
            if any(words <= existing for existing in found_word_sets):
                continue
            # A NEW, broader match supersedes any narrower ones already found.
            keep = [(f, w) for f, w in zip(found, found_word_sets) if not w < words]
            found, found_word_sets = [f for f, _ in keep], [w for _, w in keep]
            found.append(label)
            found_word_sets.append(words)
            priority = _local_priority_override(text_lower, m.start(), m.end()) or _section_priority_for_offset(text_lower, m.start())
            items.append(JDRequirementItem(
                text=label, normalized_value=label.lower(), category=RequirementCategory.CERTIFICATION,
                priority=priority, evidence_span=text[max(0, m.start() - 30):m.end() + 30].strip(),
            ))
    items = [i for i in items if i.text in found]
    return found, items


def _extract_responsibilities(text: str, text_lower: str) -> tuple[list[str], list[JDRequirementItem]]:
    found: list[str] = []
    items: list[JDRequirementItem] = []
    for phrase in RESPONSIBILITY_SIGNALS:
        m = re.search(r"\b" + re.escape(phrase) + r"\b", text_lower)
        if not m:
            continue
        priority_override = _local_priority_override(text_lower, m.start(), m.end())
        # Post-release bug fix (real Airbnb Payments JD, section 2/3):
        # responsibility items were previously hardcoded REQUIRED regardless
        # of surrounding text -- a responsibility mentioned in a "nice to
        # have" section or clause was still forced REQUIRED. Now respects
        # the same local/section priority signal every other requirement
        # category already uses.
        if _negated_after_override(text_lower, m.start(), m.end(), priority_override):
            continue
        found.append(phrase)
        priority = priority_override or _section_priority_for_offset(text_lower, m.start())
        conditional = _is_conditional(text_lower, m.start(), m.end())
        span_start = max(0, m.start() - 40)
        span_end = min(len(text), m.end() + 40)
        items.append(JDRequirementItem(
            text=phrase, normalized_value=phrase, category=RequirementCategory.RESPONSIBILITY,
            priority=priority, evidence_span=text[span_start:span_end].strip(),
            confidence=0.9 if not conditional else 0.6, conditional=conditional,
        ))
    return found, items


def _extract_domain_signals(text_lower: str) -> list[str]:
    return [d for d in DOMAIN_SIGNALS if domain_signal_present(text_lower, d)]


def _extract_sponsorship_language(text_lower: str) -> bool:
    return bool(re.search(r"\bsponsor(ship)?\b|\bvisa\b|\bh-?1b\b|\bwork authorization\b", text_lower))


def _extract_salary_mentioned(text_lower: str) -> bool:
    return bool(re.search(r"\$\s?\d{2,3}[,.]?\d{0,3}\s*(k|,000)?", text_lower))


# Semantic requirement deduplication (JD intelligence v3): a JD frequently
# names the SAME underlying technology more than once, in different phrasings
# ("build REST APIs" ... "solid understanding of RESTful services"). Left
# alone, that produces two separate requirement items for one real skill,
# inflating required/preferred coverage denominators and double-weighting the
# same signal during resume relevance scoring. Deliberately a short, EXPLICIT
# allowlist of unambiguous same-technology phrasings -- never a broad
# same-category merge: RequirementCategory.OBSERVABILITY's "monitoring" and
# "observability" are near-synonyms but are NOT merged here, because this
# codebase's own regression suite (test_resume_optimizer_airbnb_regression
# .test_huge_plus_but_not_required_kept_as_preferred_not_dropped) already
# depends on them staying two independently-tracked requirement items.
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("rest apis", "rest api", "restful", "rest"),
    ("kubernetes", "k8s"),
    ("postgresql", "postgres"),
    ("node.js", "nodejs"),
    ("google cloud", "gcp"),
    ("golang", "go"),
)
_SYNONYM_CANONICAL: dict[str, str] = {alt: group[0] for group in _SYNONYM_GROUPS for alt in group}


def _deduplicate_semantic_requirements(items: list[JDRequirementItem]) -> list[JDRequirementItem]:
    """Merges duplicate same-technology single-term items (never alternative
    groups -- those already represent one requirement by construction) that
    both refer to the same `_SYNONYM_GROUPS` entry WITHIN THE SAME CATEGORY.
    Keyed on (canonical, category) rather than canonical alone -- the same
    phrase can legitimately appear as both a SKILL_CATEGORIES item (coverage
    tracking) and a RESPONSIBILITY item (responsibility-alignment tracking,
    e.g. "rest apis" is in both SKILL_VOCAB and RESPONSIBILITY_SIGNALS); those
    are two independently-meaningful signals and must never be collapsed into
    one, or app.resume_optimizer.matching's RESPONSIBILITY-category matching
    would silently lose coverage. Keeps the FIRST-seen item's text/position
    but promotes it to REQUIRED if any duplicate was REQUIRED, and keeps the
    longer/more-specific phrase text when merging."""
    seen: dict[tuple[str, RequirementCategory], int] = {}  # (canonical, category) -> index into `out`
    out: list[JDRequirementItem] = []
    for item in items:
        if item.alternatives:
            out.append(item)
            continue
        canonical = _SYNONYM_CANONICAL.get(item.normalized_value)
        if canonical is None:
            out.append(item)
            continue
        key = (canonical, item.category)
        existing_idx = seen.get(key)
        if existing_idx is None:
            seen[key] = len(out)
            out.append(item)
            continue
        existing = out[existing_idx]
        merged_text = item.text if len(item.text) > len(existing.text) else existing.text
        merged_priority = (
            RequirementPriority.REQUIRED
            if RequirementPriority.REQUIRED in (existing.priority, item.priority)
            else RequirementPriority.PREFERRED
        )
        out[existing_idx] = JDRequirementItem(
            text=merged_text, normalized_value=existing.normalized_value, category=existing.category,
            priority=merged_priority, evidence_span=existing.evidence_span,
            confidence=max(existing.confidence, item.confidence),
            conditional=existing.conditional and item.conditional,
        )
        # duplicate item itself is dropped (not appended to `out`)
    return out


# Compensation parsing (JD intelligence v3, "where appropriate"): purely
# informational -- never a matching/eligibility signal, never persisted as a
# resume claim. A range/ceiling not genuinely present in the JD text is left
# as None rather than guessed (same "never fabricate a field the source
# doesn't provide" principle CLAUDE.md's registry/provider rules already
# establish for structurally absent data elsewhere in this codebase).
_COMP_RANGE_PATTERN = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?\s*(?:-|to|–|—)\s*\$?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?",
    re.IGNORECASE,
)
_COMP_CEILING_PATTERN = re.compile(r"\bup to\s*\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?", re.IGNORECASE)
_COMP_YEAR_PERIOD_PATTERN = re.compile(r"\bper year\b|/\s*year\b|\bannually\b|\bannual salary\b|\byearly\b", re.IGNORECASE)
_COMP_HOUR_PERIOD_PATTERN = re.compile(r"\bper hour\b|/\s*hr\b|/\s*hour\b|\bhourly\b", re.IGNORECASE)


def _parse_comp_amount(num_str: str, k_flag: str | None) -> float:
    value = float(num_str.replace(",", ""))
    return value * 1000 if k_flag else value


def _extract_compensation(text: str) -> tuple[float | None, float | None, str, str]:
    """Returns (min, max, period, currency). period is "year"/"hour"/"" (""
    when the JD gives a figure but no explicit period marker -- never
    guessed). currency is always "USD" when any figure was found (the only
    currency symbol this parser recognizes), else ""."""
    m = _COMP_RANGE_PATTERN.search(text)
    if m:
        lo = _parse_comp_amount(m.group(1), m.group(2))
        hi = _parse_comp_amount(m.group(3), m.group(4))
        lo, hi = (lo, hi) if lo <= hi else (hi, lo)
    else:
        m2 = _COMP_CEILING_PATTERN.search(text)
        if not m2:
            return None, None, "", ""
        lo, hi = None, _parse_comp_amount(m2.group(1), m2.group(2))
    period = "year" if _COMP_YEAR_PERIOD_PATTERN.search(text) else ("hour" if _COMP_HOUR_PERIOD_PATTERN.search(text) else "")
    return lo, hi, period, "USD"


def analyze_jd(job_title: str, description: str) -> JDAnalysisResult:
    """Pure function: JD text in, normalized requirements model out. Never
    reads candidate data (CLAUDE.md section 3)."""
    text = f"{job_title}\n{description or ''}"
    text_lower = text.lower()

    requirements: list[JDRequirementItem] = []
    alt_items, alt_consumed_spans = _extract_alternative_groups(text, text_lower)
    requirements.extend(alt_items)
    requirements.extend(_extract_skill_requirements(text, text_lower, consumed_spans=alt_consumed_spans))

    education_found, edu_items = _extract_education(text_lower)
    requirements.extend(edu_items)

    cert_found, cert_items = _extract_certifications(text, text_lower)
    requirements.extend(cert_items)

    years = _extract_years(text)
    if years is not None:
        requirements.append(JDRequirementItem(
            text=f"{years:g}+ years", normalized_value=str(years), category=RequirementCategory.YEARS_EXPERIENCE,
            priority=RequirementPriority.REQUIRED, evidence_span=f"{years:g}+ years experience",
        ))

    responsibilities, resp_items = _extract_responsibilities(text, text_lower)
    requirements.extend(resp_items)

    requirements = _deduplicate_semantic_requirements(requirements)

    comp_min, comp_max, comp_period, comp_currency = _extract_compensation(text)

    return JDAnalysisResult(
        job_title=job_title,
        required_years=years,
        domain_signals=_extract_domain_signals(text_lower),
        responsibilities=responsibilities,
        education_requirements=education_found,
        certification_requirements=cert_found,
        sponsorship_language_present=_extract_sponsorship_language(text_lower),
        salary_mentioned=_extract_salary_mentioned(text_lower),
        compensation_min=comp_min,
        compensation_max=comp_max,
        compensation_period=comp_period,
        compensation_currency=comp_currency,
        requirements=requirements,
        analyzer_version=ANALYZER_VERSION,
    )
