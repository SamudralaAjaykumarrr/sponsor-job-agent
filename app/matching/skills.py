import re

# Canonical backend/software-engineering skill vocabulary used to extract JD
# requirements. This is only a keyword vocabulary for matching -- it does not
# claim the candidate has any of these; verified candidate skills come solely
# from the candidate profile.
SKILL_VOCAB = [
    "python", "java", "go", "golang", "c++", "c#", "javascript", "typescript",
    "django", "flask", "fastapi", "spring", "spring boot", "node.js", "nodejs",
    "rest", "rest api", "restful", "graphql", "grpc", "microservices",
    "sql", "postgresql", "postgres", "mysql", "sqlite", "mongodb", "redis",
    "dynamodb", "cassandra", "elasticsearch",
    "aws", "azure", "gcp", "google cloud", "ec2", "s3", "lambda", "cloudformation",
    "docker", "kubernetes", "k8s", "terraform", "ansible", "jenkins", "ci/cd",
    "git", "github", "gitlab", "linux", "unix", "bash", "shell scripting",
    "kafka", "rabbitmq", "sqs", "sns",
    "unit testing", "pytest", "junit", "tdd", "test automation",
    "agile", "scrum", "rest apis", "api design", "system design",
    "distributed systems", "multithreading", "concurrency",
    "html", "css", "react", "angular", "vue",
    "machine learning", "data pipelines", "etl", "spark", "hadoop",
    "oauth", "jwt", "security", "monitoring", "observability", "grafana", "prometheus",
]


def extract_jd_keywords(description: str) -> list[str]:
    text = (description or "").lower()
    found = []
    for skill in SKILL_VOCAB:
        pattern = r"\b" + re.escape(skill) + r"\b"
        if re.search(pattern, text):
            found.append(skill)
    return found


def match_candidate_skills(
    jd_keywords: list[str], candidate_skills: list[str]
) -> tuple[float, list[str], list[str]]:
    """Returns (match_score 0-100, matched_skills, gap_skills).
    Matching is case-insensitive substring matching between JD-extracted
    keywords and the candidate's VERIFIED skill list only."""
    candidate_lower = [s.lower().strip() for s in candidate_skills if s and s != "NEEDS_USER_INPUT"]

    if not jd_keywords:
        return 0.0, [], []

    matched = []
    gaps = []
    for kw in jd_keywords:
        hit = any(kw == cs or kw in cs or cs in kw for cs in candidate_lower)
        if hit:
            matched.append(kw)
        else:
            gaps.append(kw)

    score = round(100.0 * len(matched) / len(jd_keywords), 1) if jd_keywords else 0.0
    return score, matched, gaps
