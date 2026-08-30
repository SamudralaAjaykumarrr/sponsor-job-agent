"""Fingerprinting helpers for idempotency/staleness (CLAUDE.md Phase 14
sections 35-39, 58-59). Reuses the same style of fingerprint the sponsorship
decision engine already established (app.sponsorship.decision
.compute_jd_fingerprint) rather than inventing a second scheme."""

import hashlib
import json

from app.candidate.schema import CandidateProfile

OPTIMIZER_VERSION = "resume-optimizer-v4"


def compute_jd_fingerprint(title: str, company: str, description: str) -> str:
    raw = f"{title.strip().lower()}|{company.strip().lower()}|{(description or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def compute_profile_version(profile: CandidateProfile) -> str:
    """CLAUDE.md section 59: a profile-content hash so a change to verified
    candidate data (skills/employment/education/etc.) invalidates any resume
    variant generated against the old content, without needing an explicit
    version counter maintained elsewhere."""
    raw = json.dumps(profile.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def compute_artifact_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:32]
