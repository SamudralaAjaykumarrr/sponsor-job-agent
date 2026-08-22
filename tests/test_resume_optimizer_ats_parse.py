"""CLAUDE.md Phase 14 sections 29-32, 73: ATS DOCX/PDF/TXT parse validation
against deterministic resume fixtures."""

from pathlib import Path

from app.resume.docx_writer import write_docx
from app.resume.generator import EducationBlock, ExperienceBlock, ProjectBlock, ResumeContent
from app.resume.pdf_writer import write_pdf
from app.resume.txt_writer import write_txt
from app.resume_optimizer import ats_parse
from app.resume_optimizer.models import ATSParseStatus


def _fixture_resume() -> ResumeContent:
    return ResumeContent(
        full_name="Jordan Rivera",
        email="jordan.rivera@example.com",
        phone="555-123-4567",
        location="Austin, TX",
        linkedin_url="https://linkedin.com/in/jordanrivera",
        github_url="https://github.com/jordanrivera",
        portfolio_url="",
        summary="Software engineer with 3 years of experience; verified strengths include Python, FastAPI.",
        skills_ordered=["python", "fastapi", "postgresql", "docker"],
        experience=[
            ExperienceBlock(
                company="Widget Software Inc", title="Backend Software Engineer",
                start_date="2022-06", end_date="Present", location="Remote",
                bullets=["Built and maintained REST APIs in Python using FastAPI."],
            )
        ],
        projects=[ProjectBlock(name="Job Tracker CLI", description="A CLI tool.", bullets=["Implemented in Python."], url="")],
        education=[EducationBlock(school="State University", degree="B.S.", field_of_study="Computer Science", graduation_date="2022-05")],
    )


def test_docx_pdf_txt_all_pass(tmp_path):
    resume = _fixture_resume()
    docx_path = write_docx(resume, tmp_path / "resume.docx")
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    txt_path = write_txt(resume, tmp_path / "resume.txt")

    report = ats_parse.validate_all(docx_path, pdf_path, txt_path, resume)
    assert report.overall == ATSParseStatus.PASS
    assert report.docx.status == ATSParseStatus.PASS
    assert report.pdf.status == ATSParseStatus.PASS
    assert report.txt.status == ATSParseStatus.PASS


def test_docx_extraction_finds_name_employer_title(tmp_path):
    resume = _fixture_resume()
    docx_path = write_docx(resume, tmp_path / "resume.docx")
    result = ats_parse.validate_docx(docx_path, resume)
    assert result.status == ATSParseStatus.PASS
    assert not result.reasons


def test_pdf_extraction_finds_expected_text(tmp_path):
    resume = _fixture_resume()
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    result = ats_parse.validate_pdf(pdf_path, resume)
    assert result.status == ATSParseStatus.PASS


def test_txt_extraction_finds_expected_text(tmp_path):
    resume = _fixture_resume()
    txt_path = write_txt(resume, tmp_path / "resume.txt")
    result = ats_parse.validate_txt(txt_path, resume)
    assert result.status == ATSParseStatus.PASS


def test_missing_docx_file_fails_gracefully(tmp_path):
    resume = _fixture_resume()
    result = ats_parse.validate_docx(tmp_path / "does_not_exist.docx", resume)
    assert result.status == ATSParseStatus.FAIL
    assert result.reasons


def test_empty_txt_fails(tmp_path):
    resume = _fixture_resume()
    empty_path = tmp_path / "empty.txt"
    empty_path.write_text("")
    result = ats_parse.validate_txt(empty_path, resume)
    assert result.status == ATSParseStatus.FAIL


def test_warn_when_some_expected_text_missing(tmp_path):
    resume = _fixture_resume()
    txt_path = tmp_path / "partial.txt"
    txt_path.write_text(f"{resume.full_name}\nSkills\nSummary\n")  # missing employer/education
    result = ats_parse.validate_txt(txt_path, resume)
    assert result.status in (ATSParseStatus.WARN, ATSParseStatus.FAIL)
    assert result.reasons
