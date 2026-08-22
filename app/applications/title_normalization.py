"""Deterministic job-title normalization (CLAUDE.md Phase 13 section 6).

Purely a WORD-ORDER / PUNCTUATION canonicalizer -- "Senior Software Engineer"
and "Software Engineer, Senior" are the same title written two ways, so they
normalize to the same base role + the same seniority marker set. It never
collapses a MATERIALLY different position: "Software Engineer" and "Software
Engineer II" keep different seniority markers (level "ii" is present on one,
absent on the other) and "Backend Software Engineer" keeps a different base
role than plain "Software Engineer" (the "backend" qualifier is part of the
role, not noise to strip). Title similarity/equivalence is NEVER, by itself,
used as identity proof (CLAUDE.md Phase 13 section 6) -- this module only
feeds one signal into `app.applications.job_identity`'s multi-signal check."""

import re
from dataclasses import dataclass

# Seniority/level words this module treats as a MODIFIER, tracked separately
# from the base role tokens so two titles differing only in modifier POSITION
# ("Senior Software Engineer" vs "Software Engineer, Senior") normalize
# identically, while two titles differing in which modifiers are PRESENT
# ("Software Engineer" vs "Senior Software Engineer") do not.
_SENIORITY_WORDS = frozenset({
    "senior", "sr", "junior", "jr", "staff", "principal", "lead", "associate",
    "entry", "entry-level", "intern",
})
# Roman/arabic numeral level suffixes -- "Software Engineer II" vs "Software
# Engineer 2" are the same level written two ways, but "Software Engineer"
# (no level at all) is a DIFFERENT, materially unspecified level.
_ROMAN_LEVELS = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
_PUNCT_RE = re.compile(r"[,\-/]+")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedTitle:
    base_role: str                    # canonical, order-independent role tokens joined by a space
    seniority_markers: frozenset       # e.g. {"senior"}, {"2"} (from "II"), or empty
    raw: str = ""

    def as_dict(self) -> dict:
        return {"base_role": self.base_role, "seniority_markers": sorted(self.seniority_markers), "raw": self.raw}


def normalize_title(title: str) -> NormalizedTitle:
    """Lowercases, strips punctuation, splits off seniority/level modifiers
    (wherever they appear in the string) from the remaining base-role tokens,
    and sorts the base-role tokens so word order never matters. Never
    fabricates a level that isn't actually present in the text."""
    text = (title or "").strip().lower()
    if not text:
        return NormalizedTitle(base_role="", seniority_markers=frozenset(), raw=title or "")
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t]

    base_tokens: list[str] = []
    markers: set[str] = set()
    for tok in tokens:
        if tok in _SENIORITY_WORDS:
            markers.add("senior" if tok in ("sr",) else "junior" if tok in ("jr",) else tok)
            continue
        if tok in _ROMAN_LEVELS:
            markers.add(_ROMAN_LEVELS[tok])
            continue
        if tok.isdigit() and len(tok) <= 2:
            # A bare trailing level number ("Engineer 2") -- but never a
            # 3+ digit token, which is far more likely to be something else
            # entirely (never seen in a real title, so never guessed here).
            markers.add(tok)
            continue
        base_tokens.append(tok)

    base_role = " ".join(sorted(base_tokens))
    return NormalizedTitle(base_role=base_role, seniority_markers=frozenset(markers), raw=title or "")


def titles_equivalent(title_a: str, title_b: str) -> bool:
    """True only when both the base role AND the seniority/level marker set
    match exactly. CLAUDE.md Phase 13 section 6: never uses fuzzy similarity
    -- "Software Engineer" and "Software Engineer II" are NOT equivalent
    (different, if either non-empty, marker sets), and "Backend Software
    Engineer" and "Software Engineer" are NOT equivalent (different base
    role -- "backend" survives normalization as a real role token)."""
    a = normalize_title(title_a)
    b = normalize_title(title_b)
    if not a.base_role or not b.base_role:
        return False
    return a.base_role == b.base_role and a.seniority_markers == b.seniority_markers
