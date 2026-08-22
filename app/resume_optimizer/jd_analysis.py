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

ANALYZER_VERSION = "jd-analysis-v1"

# (skill phrase, category) -- deliberately overlapping/synonymous with
# app.matching.skills.SKILL_VOCAB but categorized for diagnostics grouping
# (CLAUDE.md section 25). This is a keyword vocabulary only -- it never
# claims the candidate has any of these.
SKILL_VOCAB: list[tuple[str, RequirementCategory]] = [
    ("python", RequirementCategory.LANGUAGE),
    ("java", RequirementCategory.LANGUAGE),
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
    ("test automation", RequirementCategory.TESTING),
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
    ("monitoring", RequirementCategory.DEVOPS),
    ("observability", RequirementCategory.DEVOPS),
    ("grafana", RequirementCategory.DEVOPS),
    ("prometheus", RequirementCategory.DEVOPS),
]

RESPONSIBILITY_SIGNALS = [
    "rest apis", "distributed systems", "message queues", "message queue",
    "sql optimization", "query optimization", "ci/cd", "continuous integration",
    "cloud deployment", "debugging", "testing", "code review", "on-call",
    "mentoring", "architecture", "scalability", "performance", "production",
    "incident", "cross-functional", "design and implement", "build and maintain",
]

DOMAIN_SIGNALS = [
    "payments", "banking", "fintech", "financial services", "healthcare", "health tech",
    "retail", "e-commerce", "ecommerce", "insurance", "logistics", "supply chain",
    "gaming", "adtech", "advertising", "media", "streaming", "cybersecurity",
    "biotech", "life sciences", "government", "public sector", "education", "edtech",
    "real estate", "travel", "telecom", "energy", "manufacturing",
]

_REQUIRED_MARKERS = [
    "required qualifications", "required skills", "requirements:", "required:", "must have",
    "must-have", "minimum qualifications", "minimum requirements", "you have",
    "you must have", "basic qualifications", "what you'll need", "what you need",
]
_PREFERRED_MARKERS = [
    "preferred qualifications", "preferred skills", "preferred:", "nice to have", "nice-to-have",
    "bonus points", "bonus if", "desired", "a plus", "pluses", "would be great",
    "ideally you", "it's a plus",
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
]

_EDU_PATTERNS = [
    (r"\bph\.?d\.?\b", "PhD"),
    (r"\bmaster'?s? degree\b", "Master's degree"),
    (r"\bm\.?s\.?\b(?!\w)", "Master's degree"),
    (r"\bbachelor'?s? degree\b", "Bachelor's degree"),
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
    re.compile(r"(\d+)\s*\+?\s*-\s*(\d+)\s*\+?\s*years", re.IGNORECASE),
    re.compile(r"(\d+)\s*\+?\s*years", re.IGNORECASE),
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


_LOCAL_WINDOW_CHARS = 45
_LOCAL_REQUIRED_PHRASES = [r"\bis required\b", r"\brequired\b", r"\bmust\b", r"\bmandatory\b"]
_LOCAL_PREFERRED_PHRASES = [
    r"\bis a plus\b", r"\ba plus\b", r"\bpreferred\b", r"\bnice to have\b", r"\bbonus\b",
    r"\boptional\b", r"\bideally\b", r"\bdesired\b",
]


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


def _extract_skill_requirements(text: str, text_lower: str) -> list[JDRequirementItem]:
    items: list[JDRequirementItem] = []
    for phrase, category in SKILL_VOCAB:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        for m in re.finditer(pattern, text_lower):
            if _is_negated(text_lower, m.start(), m.end()):
                continue
            conditional = _is_conditional(text_lower, m.start(), m.end())
            priority = _local_priority_override(text_lower, m.start(), m.end()) or _section_priority_for_offset(text_lower, m.start())
            span_start = max(0, m.start() - 40)
            span_end = min(len(text), m.end() + 40)
            items.append(JDRequirementItem(
                text=phrase, normalized_value=phrase, category=category,
                priority=priority, evidence_span=text[span_start:span_end].strip(),
                confidence=0.9 if not conditional else 0.6, conditional=conditional,
            ))
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
            items.append(JDRequirementItem(
                text=label, normalized_value=label, category=RequirementCategory.EDUCATION,
                priority=priority, evidence_span=text_lower[max(0, m.start() - 30):m.end() + 30].strip(),
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


def _extract_responsibilities(text_lower: str) -> list[str]:
    found = []
    for phrase in RESPONSIBILITY_SIGNALS:
        if re.search(r"\b" + re.escape(phrase) + r"\b", text_lower):
            found.append(phrase)
    return found


def _extract_domain_signals(text_lower: str) -> list[str]:
    return [d for d in DOMAIN_SIGNALS if re.search(r"\b" + re.escape(d) + r"\b", text_lower)]


def _extract_sponsorship_language(text_lower: str) -> bool:
    return bool(re.search(r"\bsponsor(ship)?\b|\bvisa\b|\bh-?1b\b|\bwork authorization\b", text_lower))


def _extract_salary_mentioned(text_lower: str) -> bool:
    return bool(re.search(r"\$\s?\d{2,3}[,.]?\d{0,3}\s*(k|,000)?", text_lower))


def analyze_jd(job_title: str, description: str) -> JDAnalysisResult:
    """Pure function: JD text in, normalized requirements model out. Never
    reads candidate data (CLAUDE.md section 3)."""
    text = f"{job_title}\n{description or ''}"
    text_lower = text.lower()

    requirements: list[JDRequirementItem] = []
    requirements.extend(_extract_skill_requirements(text, text_lower))

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

    responsibilities = _extract_responsibilities(text_lower)
    for r in responsibilities:
        requirements.append(JDRequirementItem(
            text=r, normalized_value=r, category=RequirementCategory.RESPONSIBILITY,
            priority=RequirementPriority.REQUIRED, evidence_span=r,
        ))

    return JDAnalysisResult(
        job_title=job_title,
        required_years=years,
        domain_signals=_extract_domain_signals(text_lower),
        responsibilities=responsibilities,
        education_requirements=education_found,
        certification_requirements=cert_found,
        sponsorship_language_present=_extract_sponsorship_language(text_lower),
        salary_mentioned=_extract_salary_mentioned(text_lower),
        requirements=requirements,
        analyzer_version=ANALYZER_VERSION,
    )
