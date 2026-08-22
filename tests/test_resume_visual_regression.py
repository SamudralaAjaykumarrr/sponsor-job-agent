"""CLAUDE.md Phase 15 sections 34-36: practical layout/visual regression
checks for generated resume DOCX/PDF artifacts, beyond text extraction
alone. Uses only synthetic, non-identifying fixture profile data (section
35) -- never the real candidate's generated resume."""

from app.resume.docx_writer import write_docx
from app.resume.generator import EducationBlock, ExperienceBlock, ProjectBlock, ResumeContent
from app.resume.pdf_writer import write_pdf
from app.resume_optimizer import visual_regression


def _short_fixture_resume() -> ResumeContent:
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


def _long_fixture_resume() -> ResumeContent:
    """Synthetic fixture with many verbose bullets across several roles --
    used to exercise the pathological-page-count warning path without
    claiming any real candidate has this much content."""
    many_bullets = [f"Delivered feature #{i}: a moderately long, realistic-sounding bullet point describing backend "
                     f"work on a distributed system component, including design, implementation, and testing, "
                     f"with attention to reliability, observability, and cross-team coordination." for i in range(1, 15)]
    experience = [
        ExperienceBlock(
            company=f"Example Company {n}", title="Software Engineer", start_date=f"20{15+n}-01", end_date=f"20{16+n}-01",
            location="Remote", bullets=list(many_bullets),
        )
        for n in range(1, 9)
    ]
    return ResumeContent(
        full_name="Taylor Chen", email="taylor.chen@example.com", phone="555-987-6543",
        location="Denver, CO", linkedin_url="https://linkedin.com/in/taylorchen",
        github_url="https://github.com/taylorchen", portfolio_url="",
        summary="Software engineer with extensive verified backend experience.",
        skills_ordered=["python", "java", "kubernetes", "postgresql", "kafka", "docker", "aws", "terraform"],
        experience=experience,
        projects=[ProjectBlock(name=f"Side Project {n}", description="A synthetic fixture project.",
                                bullets=list(many_bullets[:4]), url="") for n in range(1, 4)],
        education=[EducationBlock(school="Fixture State University", degree="B.S.", field_of_study="Computer Science",
                                   graduation_date="2015-05")],
    )


def test_short_resume_layout_passes(tmp_path):
    resume = _short_fixture_resume()
    docx_path = write_docx(resume, tmp_path / "resume.docx")
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    report = visual_regression.validate_layout(pdf_path, docx_path, resume)
    assert report.overall == "PASS", report.as_dict()
    assert report.pdf.page_count == 1


def test_pdf_headings_and_bullets_render(tmp_path):
    resume = _short_fixture_resume()
    docx_path = write_docx(resume, tmp_path / "resume.docx")
    result = visual_regression.check_docx_structure(docx_path, resume)
    assert result.status == "PASS", result.reasons


def test_no_blank_pdf_pages(tmp_path):
    resume = _short_fixture_resume()
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    result = visual_regression.check_pdf_layout(pdf_path)
    assert "likely blank" not in " ".join(result.reasons)


def test_long_resume_warns_on_pathological_page_count(tmp_path):
    resume = _long_fixture_resume()
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    result = visual_regression.check_pdf_layout(pdf_path)
    assert result.page_count >= visual_regression.PATHOLOGICAL_PAGE_COUNT
    assert result.status == "WARN"
    assert any("unusually long" in r for r in result.reasons)


def test_long_resume_is_warn_not_fail(tmp_path):
    """CLAUDE.md section 36: no arbitrary one-page hard requirement --
    legitimately dense content warns, never fails."""
    resume = _long_fixture_resume()
    docx_path = write_docx(resume, tmp_path / "resume.docx")
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    report = visual_regression.validate_layout(pdf_path, docx_path, resume)
    assert report.overall in ("PASS", "WARN")
    assert report.overall != "FAIL"


def test_missing_contact_info_is_flagged(tmp_path):
    resume = _short_fixture_resume()
    docx_path = write_docx(resume, tmp_path / "resume.docx")
    # Simulate a generation bug: check against a resume claiming a phone
    # number that was never actually written into the document.
    import dataclasses

    tampered = dataclasses.replace(resume, phone="555-000-0000")
    result = visual_regression.check_docx_structure(docx_path, tampered)
    assert result.status == "FAIL"
    assert any("contact field" in r for r in result.reasons)


def test_empty_pdf_pages_fail():
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    from io import BytesIO
    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "blank.pdf"
        path.write_bytes(buf.getvalue())
        result = visual_regression.check_pdf_layout(path)
        assert result.status == "FAIL"
