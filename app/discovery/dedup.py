import hashlib
import re


def normalize_str(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def fingerprint(company: str, title: str, location: str) -> str:
    """Cross-provider dedup fingerprint. Stable IDs (provider + external_job_id)
    are the primary dedup key; this fingerprint catches the same role posted
    to multiple sources (e.g. a company's own site + Greenhouse)."""
    key = "|".join([normalize_str(company), normalize_str(title), normalize_str(location)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
