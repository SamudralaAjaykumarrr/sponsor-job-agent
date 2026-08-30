import re

# Canonical backend/software-engineering skill registry used to extract JD
# requirements and to normalize candidate skill strings onto the SAME fixed
# id space. This is only a keyword vocabulary for matching -- it does not
# claim the candidate has any of these; verified candidate skills come
# solely from the candidate profile.
#
# Each canonical id maps to an explicit, evidence-backed list of surface-form
# aliases. Detection of an alias in text always requires it to appear as an
# ISOLATED token (see `_boundary_pattern`), and candidate-side matching is
# always canonical-id-SET-equality after both sides are normalized through
# the identical extraction path -- never raw substring containment (`a in
# b`). That is what prevents "go" from matching inside "Django" or "java"
# from matching inside "JavaScript": "go" and "django" are different
# canonical ids, "java" and "javascript" are different canonical ids, and
# ids are only ever compared for exact equality.
SKILL_REGISTRY: dict[str, list[str]] = {
    "python": ["python"],
    "java": ["java"],
    "javascript": ["javascript"],
    "typescript": ["typescript"],
    "go": ["go", "golang"],
    "c": ["c"],
    "c++": ["c++", "cpp"],
    "c#": ["c#", "csharp", "c-sharp"],
    ".net": [".net", "dotnet", ".net core", ".net framework"],
    "r": ["r"],
    "django": ["django"],
    "flask": ["flask"],
    "fastapi": ["fastapi"],
    "spring": ["spring", "spring boot"],
    "node.js": ["node.js", "nodejs", "node"],
    "react": ["react", "reactjs", "react.js"],
    "angular": ["angular"],
    "vue": ["vue", "vue.js", "vuejs"],
    "rest": ["rest", "rest api", "rest apis", "restful"],
    "graphql": ["graphql"],
    "grpc": ["grpc"],
    "microservices": ["microservices"],
    "sql": ["sql"],
    "postgresql": ["postgresql", "postgres"],
    "mysql": ["mysql"],
    "sqlite": ["sqlite"],
    "mongodb": ["mongodb"],
    "redis": ["redis"],
    "dynamodb": ["dynamodb"],
    "cassandra": ["cassandra"],
    "elasticsearch": ["elasticsearch"],
    "aws": ["aws", "amazon web services"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "ec2": ["ec2"],
    "s3": ["s3"],
    "lambda": ["lambda"],
    "cloudformation": ["cloudformation"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "terraform": ["terraform"],
    "ansible": ["ansible"],
    "jenkins": ["jenkins"],
    "ci/cd": ["ci/cd"],
    "git": ["git"],
    "github": ["github"],
    "gitlab": ["gitlab"],
    "linux": ["linux"],
    "unix": ["unix"],
    "bash": ["bash"],
    "shell scripting": ["shell scripting"],
    "kafka": ["kafka"],
    "rabbitmq": ["rabbitmq"],
    "sqs": ["sqs"],
    "sns": ["sns"],
    "unit testing": ["unit testing"],
    "pytest": ["pytest"],
    "junit": ["junit"],
    "tdd": ["tdd"],
    "test automation": ["test automation"],
    "agile": ["agile"],
    "scrum": ["scrum"],
    "api design": ["api design"],
    "system design": ["system design"],
    "distributed systems": ["distributed systems"],
    "multithreading": ["multithreading"],
    "concurrency": ["concurrency"],
    "html": ["html"],
    "css": ["css"],
    "machine learning": ["machine learning"],
    "data pipelines": ["data pipelines"],
    "etl": ["etl"],
    "spark": ["spark"],
    "hadoop": ["hadoop"],
    "oauth": ["oauth"],
    "jwt": ["jwt"],
    "security": ["security"],
    "monitoring": ["monitoring"],
    "observability": ["observability"],
    "grafana": ["grafana"],
    "prometheus": ["prometheus"],
}


def _boundary_pattern(alias: str) -> re.Pattern:
    """An isolated-token match for `alias`: requires a non-alphanumeric
    character (or start/end of string) on both sides. Unlike plain `\\b`,
    this behaves correctly for aliases that themselves end in a symbol
    (`c++`, `c#`) -- `\\b` is a transition between \\w and \\W, so
    `\\bc\\+\\+\\b` never matches "C++" followed by whitespace or
    punctuation (both sides of that trailing boundary are \\W, so no \\w/\\W
    transition exists there at all). This lookaround form instead directly
    asserts "the character just outside the match, if any, is not
    alphanumeric" on each side, which is exactly the boundary condition we
    want regardless of what the alias itself starts/ends with."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])")


# Aliases needing a narrower trailing boundary than "any non-alphanumeric":
# bare "c" is itself a valid boundary-isolated prefix of "c++"/"c#" (a `+`/
# `#` immediately after "c" is non-alphanumeric, so the generic boundary
# check alone would credit "C" -- a different, separate canonical id --
# from a JD that only ever said "C++" or "C#"). Excluding those two
# specific following characters keeps every other "C" usage (space, comma,
# end of string, etc.) matching normally.
_NARROWED_TRAILING_EXCLUSIONS: dict[str, str] = {
    "c": r"[+#]",
}


def _pattern_for_alias(alias: str) -> re.Pattern:
    extra_exclusion = _NARROWED_TRAILING_EXCLUSIONS.get(alias)
    if extra_exclusion is None:
        return _boundary_pattern(alias)
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?!" + extra_exclusion + r")(?![A-Za-z0-9])"
    )


_ALIAS_PATTERNS: dict[str, list[re.Pattern]] = {
    canonical: [_pattern_for_alias(alias) for alias in aliases]
    for canonical, aliases in SKILL_REGISTRY.items()
}


def contains_term(text: str, term: str) -> bool:
    """True if `term` appears in `text` as an isolated token/phrase --
    never as a fragment of a longer, unrelated word. A single shared
    primitive so every caller that needs "does this text genuinely mention
    this term" (this module's own extraction, and
    app.resume_optimizer.relevance's bullet-relevance scoring) applies the
    identical, correct boundary rule rather than each hand-rolling its own
    (a real bug caught live: raw `term in text` substring checks matched
    "go" inside "Django" and "java" inside "JavaScript")."""
    return _boundary_pattern(term).search((text or "").lower()) is not None


def extract_skill_ids(text: str) -> set[str]:
    """Canonical skill ids genuinely present in `text` as isolated tokens,
    per SKILL_REGISTRY. The shared normalization step: JD description text
    and a candidate's own free-text skill strings (e.g. "AWS Lambda",
    "Amazon SQS") are both run through this SAME function, so matching
    downstream is always canonical-id-set equality/intersection -- never
    substring containment against the raw strings."""
    lower = (text or "").lower()
    return {cid for cid, patterns in _ALIAS_PATTERNS.items() if any(p.search(lower) for p in patterns)}


def extract_jd_keywords(description: str) -> list[str]:
    """Canonical skill ids found in `description`, in SKILL_REGISTRY's
    (stable, deterministic) iteration order."""
    ids = extract_skill_ids(description)
    return [cid for cid in SKILL_REGISTRY if cid in ids]


def match_candidate_skills(
    jd_keywords: list[str], candidate_skills: list[str]
) -> tuple[float, list[str], list[str]]:
    """Returns (match_score 0-100, matched_skills, gap_skills).
    Both `jd_keywords` (already canonical ids, from extract_jd_keywords) and
    each candidate skill string are normalized onto the SAME canonical id
    space (see extract_skill_ids); a JD keyword counts as matched only when
    its exact canonical id is present in the union of the candidate's own
    canonical ids -- never via `kw in cs or cs in kw` substring containment,
    which is what previously let "go" credit falsely off "Django" and
    "java" falsely off "JavaScript" (real bug, caught during a canary-
    candidate integrity re-screen: neither term is a genuine substring
    match a truthful matcher should ever count)."""
    candidate_ids: set[str] = set()
    for s in candidate_skills:
        if not s or s == "NEEDS_USER_INPUT":
            continue
        candidate_ids |= extract_skill_ids(s)

    if not jd_keywords:
        return 0.0, [], []

    matched = [kw for kw in jd_keywords if kw in candidate_ids]
    gaps = [kw for kw in jd_keywords if kw not in candidate_ids]

    score = round(100.0 * len(matched) / len(jd_keywords), 1) if jd_keywords else 0.0
    return score, matched, gaps
