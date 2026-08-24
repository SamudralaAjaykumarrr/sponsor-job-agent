"""One-page resume hard output contract (see docs/one-page-resume-contract.md).

Every automatically-generated, job-specific resume must render as exactly
one PDF page. After a normal render, if the PDF is more than one page, this
module applies a BOUNDED compression ladder and re-renders, repeating until
either one page is achieved or app.config.ONE_PAGE_MAX_COMPRESSION_STEPS is
exhausted -- never shrinking font below app.config.ONE_PAGE_MIN_FONT_SIZE,
and never producing a tiny/unreadable render as a substitute for an honest
`REVIEW_REQUIRED` outcome.

Truthfulness constraint that shapes this module's design: app.resume.
claim_checker.check_resume_claims() (CLAUDE.md's unmodified, single
truthfulness firewall) requires every resume bullet to be an EXACT string
match against the candidate's verified_bullets. That means a bullet can
never be truncated/reworded as a compression step -- any character-level
edit would turn it into text the claim checker correctly rejects as
unverified. So this ladder only ever REMOVES whole (already-verified)
bullets/skills/projects, regenerates the (never claim-checked) free-text
summary, or adjusts pure rendering (font/spacing) -- it never rewrites a
verified claim's wording. This is a deliberate adaptation of the general
"shorten verbose bullet" idea to this codebase's stricter, non-negotiable
truthfulness invariant.

Compression always removes the LOWEST-relevance optional content first
(zero JD-relevance-term overlap), so required-evidence bullets (which by
construction overlap the JD's own relevance terms) are the last things ever
touched -- see docs/one-page-resume-contract.md's overflow acceptance
scenario."""

import copy
from dataclasses import dataclass, field
from pathlib import Path

from app import config
from app.resume.docx_writer import write_docx
from app.resume.generator import ResumeContent
from app.resume.pdf_writer import count_pdf_pages, write_pdf
from app.resume.txt_writer import write_txt


@dataclass
class OnePageResult:
    resume: ResumeContent
    docx_path: Path
    pdf_path: Path
    txt_path: Path
    page_count: int
    one_page: bool
    compression_steps_applied: int
    compression_log: list[str] = field(default_factory=list)


def _bullet_relevance(bullet: str, relevance_terms) -> float:
    """`relevance_terms` is either a plain `set[str]` (unweighted membership
    count -- the original behavior, still exercised directly by
    tests/test_one_page_resume.py's private-function test) or a
    `dict[str, float]` (JD-intelligence-v3 weighted model from
    app.resume_optimizer.relevance.RelevanceModel.weights) -- summing each
    matched term's weight instead of a flat 1 per match."""
    b = bullet.lower()
    if isinstance(relevance_terms, dict):
        return sum(w for t, w in relevance_terms.items() if t and t in b)
    return sum(1 for t in relevance_terms if t and t in b)


def _remove_lowest_relevance_bullet(resume: ResumeContent, relevance_terms, entry_recency: dict | None = None):
    """Step 1: remove the single lowest-relevance OPTIONAL bullet across all
    experience/project entries -- never an entry's last remaining bullet
    (every included role/project always keeps at least one piece of
    evidence). `entry_recency` (company -> 0..1, optional) adds a small
    recency tiebreak so an older role's bullets are removed slightly before
    an equally-relevant recent one's -- never applied to projects, which
    have no dates in the candidate schema."""
    candidates = []
    for i, e in enumerate(resume.experience):
        if len(e.bullets) > 1:
            recency_bonus = (entry_recency or {}).get(e.company, 0.0) * 0.25
            for j, b in enumerate(e.bullets):
                candidates.append((_bullet_relevance(b, relevance_terms) + recency_bonus, "experience", i, j, b))
    for i, p in enumerate(resume.projects):
        if len(p.bullets) > 1:
            for j, b in enumerate(p.bullets):
                candidates.append((_bullet_relevance(b, relevance_terms), "project", i, j, b))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    rel, kind, ei, bi, text = candidates[0]
    new = copy.deepcopy(resume)
    if kind == "experience":
        removed = new.experience[ei].bullets.pop(bi)
        where = f"{new.experience[ei].company}"
    else:
        removed = new.projects[ei].bullets.pop(bi)
        where = f"{new.projects[ei].name}"
    return new, f"removed lowest-relevance optional bullet (relevance={rel}) from '{where}'"


def _remove_lowest_relevance_project(resume: ResumeContent, relevance_terms):
    """Step 2 (secondary to bullet removal, applied once bullet-trimming is
    exhausted): drop the single weakest whole project entry -- projects are
    already optional inclusions (CLAUDE.md 'project inclusion if valuable')."""
    if not resume.projects:
        return None
    scored = [
        (sum(_bullet_relevance(b, relevance_terms) for b in p.bullets), i)
        for i, p in enumerate(resume.projects)
    ]
    scored.sort(key=lambda s: s[0])
    _score, idx = scored[0]
    new = copy.deepcopy(resume)
    removed = new.projects.pop(idx)
    return new, f"removed lowest-relevance project entry '{removed.name}'"


def _remove_lowest_relevance_skill(resume: ResumeContent, protected_lower: set[str]):
    """Step 3: drop the lowest-priority OPTIONAL skill from the tail of the
    ordered skill list -- never a JD-matched/priority skill (protected_lower)."""
    removable_indices = [i for i, s in enumerate(resume.skills_ordered) if s.lower() not in protected_lower]
    if not removable_indices:
        return None
    idx = removable_indices[-1]
    target = resume.skills_ordered[idx]
    new = copy.deepcopy(resume)
    del new.skills_ordered[idx]
    return new, f"removed lowest-relevance optional skill '{target}'"


def _shorten_summary(resume: ResumeContent):
    """Step 4: shorten the free-text summary. Never claim-checked (it is not
    a bullet/skill/employer claim), so this is the one place actual text
    editing is safe -- and it only ever DROPS a trailing clause, never adds
    new wording, so it can't introduce an unverified claim."""
    summary = resume.summary
    if ";" in summary:
        head = summary.split(";", 1)[0].strip()
        new_summary = (head if head.endswith(".") else head + ".")
    else:
        sentences = [s for s in summary.split(". ") if s]
        if len(sentences) <= 1:
            return None
        new_summary = sentences[0].strip().rstrip(".") + "."
    if new_summary == summary or not new_summary:
        return None
    new = copy.deepcopy(resume)
    new.summary = new_summary
    return new, "shortened summary"


def _build_relevance_terms(resume: ResumeContent) -> set[str]:
    """Deliberately conservative: without direct access to the JD analysis
    here (this module only sees the already-built ResumeContent), relevance
    is approximated by which skills were promoted to the front of
    skills_ordered by the optimizer's own JD-aware ordering (CLAUDE.md
    sections 9-10) -- the leading half of skills_ordered is treated as
    JD-relevant, the trailing half as filler. This keeps required-evidence
    bullets (which mention JD-relevant skills) protected without this module
    needing to re-run JD analysis itself."""
    if not resume.skills_ordered:
        return set()
    cutoff = max(1, len(resume.skills_ordered) // 2)
    return {s.lower() for s in resume.skills_ordered[:cutoff]}


def enforce_one_page(
    resume: ResumeContent, out_dir: Path, *, protected_skills_lower: set[str] | None = None,
    relevance_weights: dict[str, float] | None = None, entry_recency: dict[str, float] | None = None,
) -> OnePageResult:
    """Renders `resume` to DOCX/PDF/TXT under `out_dir`, applying the bounded
    compression ladder until the PDF is exactly one page or
    ONE_PAGE_MAX_COMPRESSION_STEPS is exhausted. Returns the FINAL
    ResumeContent actually rendered (so callers persist/claim-check/ATS-parse
    the same content that produced the artifacts -- DOCX/PDF/TXT are always
    generated from the identical final ResumeContent, never allowed to
    diverge).

    `relevance_weights` (JD intelligence v3, optional): a term->weight map
    from app.resume_optimizer.relevance.RelevanceModel.weights -- when
    given, overflow removal ranks by the SAME weighted required/
    responsibility/domain/keyword signal the optimizer used for initial
    bullet selection, instead of this module's own unweighted top-half-of-
    skills_ordered fallback (`_build_relevance_terms`, still used when this
    is None -- e.g. a direct enforce_one_page() call with no JD context, as
    every existing test in tests/test_one_page_resume.py exercises).
    `entry_recency` (optional): company -> 0..1, a small recency tiebreak on
    bullet removal (see `_remove_lowest_relevance_bullet`)."""
    protected = protected_skills_lower or _build_relevance_terms(resume)
    current = resume
    log: list[str] = []
    steps_applied = 0
    max_steps = config.ONE_PAGE_MAX_COMPRESSION_STEPS

    docx_path = out_dir / "resume.docx"
    pdf_path = out_dir / "resume.pdf"
    txt_path = out_dir / "resume.txt"

    while True:
        compression_level = min(steps_applied, 6)
        write_docx(current, docx_path, compression_level=compression_level)
        write_pdf(current, pdf_path, compression_level=compression_level)
        write_txt(current, txt_path)
        page_count = count_pdf_pages(pdf_path)

        if page_count <= 1:
            return OnePageResult(
                resume=current, docx_path=docx_path, pdf_path=pdf_path, txt_path=txt_path,
                page_count=page_count, one_page=True, compression_steps_applied=steps_applied,
                compression_log=log,
            )

        if steps_applied >= max_steps:
            return OnePageResult(
                resume=current, docx_path=docx_path, pdf_path=pdf_path, txt_path=txt_path,
                page_count=page_count, one_page=False, compression_steps_applied=steps_applied,
                compression_log=log,
            )

        relevance_terms = relevance_weights if relevance_weights is not None else _build_relevance_terms(current)
        step_result = (
            _remove_lowest_relevance_bullet(current, relevance_terms, entry_recency)
            or _remove_lowest_relevance_project(current, relevance_terms)
            or _remove_lowest_relevance_skill(current, protected)
            or _shorten_summary(current)
        )
        if step_result is None:
            # Bounded: no more safe content-removal steps available -- try
            # pure spacing/font compression (already applied via
            # compression_level above) one more time before giving up
            # honestly, never fabricating a smaller-than-readable render.
            next_level = min(steps_applied + 1, 6)
            if next_level == compression_level:
                return OnePageResult(
                    resume=current, docx_path=docx_path, pdf_path=pdf_path, txt_path=txt_path,
                    page_count=page_count, one_page=False, compression_steps_applied=steps_applied,
                    compression_log=log,
                )
            log.append("reduced spacing/typography (no further content removal available)")
            steps_applied += 1
            continue

        current, reason = step_result
        log.append(reason)
        steps_applied += 1
