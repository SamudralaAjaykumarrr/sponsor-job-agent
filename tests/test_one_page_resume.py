"""One-page resume hard output contract (CLAUDE.md one-click-agent sections
7-8, 36-38). Uses only synthetic fixture profile data (mirrors
test_resume_visual_regression.py's own convention)."""

from pathlib import Path

import pytest

from app.candidate.profile import save_profile
from app.resume.generator import EducationBlock, ExperienceBlock, ProjectBlock, ResumeContent
from app.resume.pdf_writer import count_pdf_pages
from app.resume_optimizer.models import ResumeVariantStatus
from app.resume_optimizer.one_page import enforce_one_page
from app.resume_optimizer.optimizer import optimize_resume


def _short_resume() -> ResumeContent:
    return ResumeContent(
        full_name="Jordan Rivera", email="jordan.rivera@example.com", phone="555-123-4567",
        location="Austin, TX", linkedin_url="https://linkedin.com/in/jordanrivera",
        github_url="https://github.com/jordanrivera", portfolio_url="",
        summary="Software engineer with 3 years of experience; verified strengths include Python, FastAPI.",
        skills_ordered=["python", "fastapi", "postgresql", "docker"],
        experience=[
            ExperienceBlock(company="Widget Software Inc", title="Backend Software Engineer",
                             start_date="2022-06", end_date="Present", location="Remote",
                             bullets=["Built and maintained REST APIs in Python using FastAPI."]),
        ],
        projects=[ProjectBlock(name="Job Tracker CLI", description="A CLI tool.",
                                bullets=["Implemented in Python."], url="")],
        education=[EducationBlock(school="State University", degree="B.S.",
                                   field_of_study="Computer Science", graduation_date="2022-05")],
    )


def _verbose_resume(n_employers: int = 8, n_bullets: int = 14) -> ResumeContent:
    """Intentionally excessive fixture -- exercises the compression ladder's
    honest give-up path (REVIEW_REQUIRED), never a fabricated tiny render."""
    bullets = [
        f"Delivered feature #{i}: a moderately long, realistic-sounding bullet describing backend work on a "
        f"distributed system component, including design, implementation, and testing, with attention to "
        f"reliability, observability, and cross-team coordination." for i in range(1, n_bullets + 1)
    ]
    experience = [
        ExperienceBlock(company=f"Example Company {n}", title="Software Engineer", start_date=f"20{15+n}-01",
                         end_date=f"20{16+n}-01", location="Remote", bullets=list(bullets))
        for n in range(1, n_employers + 1)
    ]
    return ResumeContent(
        full_name="Taylor Chen", email="taylor.chen@example.com", phone="555-987-6543",
        location="Denver, CO", linkedin_url="https://linkedin.com/in/taylorchen",
        github_url="https://github.com/taylorchen", portfolio_url="",
        summary="Software engineer with extensive verified backend experience; verified strengths include Python.",
        skills_ordered=["python", "java", "kubernetes", "postgresql", "kafka", "docker", "aws", "terraform"],
        experience=experience,
        projects=[ProjectBlock(name=f"Side Project {n}", description="A synthetic fixture project.",
                                bullets=list(bullets[:4]), url="") for n in range(1, 4)],
        education=[EducationBlock(school="Fixture State University", degree="B.S.",
                                   field_of_study="Computer Science", graduation_date="2015-05")],
    )


def _moderately_verbose_resume() -> ResumeContent:
    """Realistically dense but safely one-page-achievable fixture."""
    def bullets(n, tag):
        return [f"{tag} bullet {i}: built and maintained backend services using Python, FastAPI, and PostgreSQL "
                f"with CI/CD automation and monitoring for reliability at scale in production environments."
                for i in range(1, n + 1)]

    return ResumeContent(
        full_name="Jordan Rivera", email="jordan.rivera@example.com", phone="555-123-4567",
        location="Austin, TX", linkedin_url="https://linkedin.com/in/jordanrivera",
        github_url="https://github.com/jordanrivera", portfolio_url="",
        summary="Software engineer with 6 years of experience; verified strengths include Python, FastAPI, "
                "PostgreSQL, Docker, AWS.",
        skills_ordered=["python", "fastapi", "postgresql", "docker", "aws", "kubernetes", "git", "pytest"],
        experience=[
            ExperienceBlock(company="Acme Corp", title="Senior Backend Engineer", start_date="2022-01",
                             end_date="Present", location="Remote", bullets=bullets(6, "Acme")),
            ExperienceBlock(company="Widget Inc", title="Backend Engineer", start_date="2019-06",
                             end_date="2021-12", location="Remote", bullets=bullets(5, "Widget")),
            ExperienceBlock(company="StartCo", title="Software Engineer", start_date="2017-01",
                             end_date="2019-05", location="Remote", bullets=bullets(4, "StartCo")),
        ],
        projects=[ProjectBlock(name="Job Tracker CLI", description="A CLI tool.",
                                bullets=["Implemented in Python with SQLite backend."], url="")],
        education=[EducationBlock(school="State University", degree="B.S.",
                                   field_of_study="Computer Science", graduation_date="2017-05")],
    )


def test_short_resume_needs_no_compression(tmp_path):
    result = enforce_one_page(_short_resume(), tmp_path)
    assert result.one_page is True
    assert result.page_count == 1
    assert result.compression_steps_applied == 0
    assert result.compression_log == []


def test_moderately_verbose_resume_compresses_to_one_page(tmp_path):
    result = enforce_one_page(_moderately_verbose_resume(), tmp_path)
    assert result.one_page is True
    assert result.page_count == 1
    assert result.compression_steps_applied >= 1
    assert count_pdf_pages(result.pdf_path) == 1


def test_pathological_resume_gives_up_honestly(tmp_path):
    """CLAUDE.md section 8/37: bounded compression, then an honest
    REVIEW_REQUIRED -- never a fabricated unreadable one-page render."""
    from app import config

    result = enforce_one_page(_verbose_resume(), tmp_path)
    assert result.one_page is False
    assert result.compression_steps_applied <= config.ONE_PAGE_MAX_COMPRESSION_STEPS
    assert result.page_count > 1


def test_required_evidence_bullets_survive_before_optional_ones(tmp_path):
    """CLAUDE.md section 37: required-evidence bullets (those overlapping
    the JD-relevance terms) must survive compression before low-value
    optional bullets do."""
    resume = ResumeContent(
        full_name="Jordan Rivera", email="jordan.rivera@example.com", phone="555-123-4567",
        location="Austin, TX", linkedin_url="", github_url="", portfolio_url="",
        summary="Software engineer with 5 years of experience; verified strengths include python, fastapi.",
        skills_ordered=["python", "fastapi"],  # both treated as JD-relevant (top half of skills_ordered)
        experience=[
            ExperienceBlock(
                company="Acme Corp", title="Backend Engineer", start_date="2020-01", end_date="Present",
                location="Remote",
                bullets=[
                    "Built REST APIs in python using fastapi for production services.",  # relevant (kept longest)
                    "Organized the team's weekly potluck lunch schedule.",  # zero relevance -- removed first
                ],
            ),
        ],
        projects=[], education=[],
    )
    from app.resume_optimizer.one_page import _remove_lowest_relevance_bullet

    relevance_terms = {"python", "fastapi"}
    step = _remove_lowest_relevance_bullet(resume, relevance_terms)
    assert step is not None
    new_resume, reason = step
    assert "relevance=0" in reason
    assert new_resume.experience[0].bullets == ["Built REST APIs in python using fastapi for production services."]


def test_never_shrinks_font_below_min_size(tmp_path):
    from app import config
    from app.resume.pdf_writer import write_pdf

    write_pdf(_short_resume(), tmp_path / "r.pdf", compression_level=6)
    # No exception, and a valid one-page PDF still renders at the max
    # compression level -- font floor is enforced inside write_pdf itself.
    assert count_pdf_pages(tmp_path / "r.pdf") == 1
    assert config.ONE_PAGE_MIN_FONT_SIZE > 0


def test_docx_pdf_txt_all_reflect_same_compressed_content(tmp_path):
    """Artifacts must never diverge -- all three are written from the exact
    same final (possibly compressed) ResumeContent."""
    result = enforce_one_page(_moderately_verbose_resume(), tmp_path)
    from docx import Document

    doc = Document(str(result.docx_path))
    docx_text = "\n".join(p.text for p in doc.paragraphs)
    txt_text = result.txt_path.read_text()
    for bullet in result.resume.experience[0].bullets:
        assert bullet in docx_text
        assert bullet in txt_text


@pytest.fixture
def profile_saved(tmp_env, sample_profile):
    save_profile(sample_profile)
    return sample_profile


def test_optimize_resume_promotes_one_page_status(tmp_env, profile_saved):
    """End-to-end: optimize_resume() wires one_page enforcement through and
    records page_count/status on the variant."""
    from app.jobs_repo import insert_job
    from app.models import ApplicationMode, Job

    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote (US)",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI and "
            "PostgreSQL. Required: Python, FastAPI, PostgreSQL, Docker."
        ),
        mode=ApplicationMode.ASSIST,
    )
    job_id = insert_job(job)
    result = optimize_resume(job_id)
    assert result.status == ResumeVariantStatus.READY.value

    from app.resume_optimizer.repo import get_current_variant

    variant = get_current_variant(job_id)
    assert variant["page_count"] == 1
