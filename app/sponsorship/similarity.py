"""Deterministic role/occupation/location similarity (CLAUDE.md Phase 7
sections 13-14). Pure token/category matching -- no opaque ML score, no fake
probability. Every result carries a human-readable explanation string."""

import re

from app.sponsorship.schema import RoleSimilarityTier

_STOPWORDS = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "ii", "iii", "i"}

# Broad "software/technical engineering" family tokens -- deliberately wide
# (matches the candidate's target-role list in CLAUDE.md) so title variants
# like "Backend Software Engineer" and occupation strings like "Software
# Developers, Applications" both land in the same family.
_SOFTWARE_FAMILY_TOKENS = {
    "software", "developer", "development", "engineer", "engineering", "programmer",
    "backend", "frontend", "fullstack", "full-stack", "platform", "cloud", "devops",
    "api", "python", "systems", "infrastructure", "sdet", "qa", "automation",
    "application", "applications", "data",
}

# DOL/USCIS SOC occupation codes in the 15-11xx/15-12xx/15-13xx range are the
# "Computer Occupations" family (e.g. 15-1252 Software Developers). Matching
# on the 5-digit prefix is deterministic and doesn't require a full SOC table.
_TECH_SOC_PREFIXES = ("15-11", "15-12", "15-13", "15-20", "15-21")


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]*", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def is_software_family_soc(occupation_code: str) -> bool:
    code = (occupation_code or "").strip()
    return any(code.startswith(p) for p in _TECH_SOC_PREFIXES)


def role_similarity(current_title: str, occupation_title: str, occupation_code: str = "") -> tuple[RoleSimilarityTier, list[str]]:
    reasons: list[str] = []
    current_tokens = _tokenize(current_title)
    occ_tokens = _tokenize(occupation_title)

    if not occ_tokens:
        return RoleSimilarityTier.NONE, ["no occupation title on record"]

    overlap = current_tokens & occ_tokens
    current_is_software = bool(current_tokens & _SOFTWARE_FAMILY_TOKENS)
    occ_is_software = bool(occ_tokens & _SOFTWARE_FAMILY_TOKENS) or is_software_family_soc(occupation_code)

    if occ_is_software and is_software_family_soc(occupation_code):
        reasons.append(f"occupation SOC code '{occupation_code}' is in the software/computer occupations family")

    if current_tokens == occ_tokens or (overlap and len(overlap) == len(current_tokens)):
        reasons.append("strong title similarity -- current role title is a near-exact match to occupation title")
        return RoleSimilarityTier.STRONG, reasons

    if current_is_software and occ_is_software and len(overlap) >= 2:
        reasons.append(f"same occupation family (software engineering); {len(overlap)} overlapping title tokens")
        return RoleSimilarityTier.STRONG, reasons

    if current_is_software and occ_is_software:
        reasons.append("employer has recent software-engineering-family filings; current role is also software engineering")
        return RoleSimilarityTier.MODERATE, reasons

    if overlap:
        reasons.append(f"{len(overlap)} overlapping title token(s): {', '.join(sorted(overlap))}")
        return RoleSimilarityTier.WEAK, reasons

    reasons.append("no title token overlap and different occupation family")
    return RoleSimilarityTier.NONE, reasons


def location_similarity(current_state: str, evidence_states: set[str]) -> tuple[bool, str]:
    current_state = (current_state or "").strip().upper()
    evidence_states = {s.strip().upper() for s in evidence_states if s}
    if not current_state or not evidence_states:
        return False, "no location data available to compare"
    if current_state in evidence_states:
        return True, f"employer has recent H-1B activity in {current_state}; current role is also {current_state}"
    return False, f"current role location ({current_state}) does not match employer's recent filing states"
