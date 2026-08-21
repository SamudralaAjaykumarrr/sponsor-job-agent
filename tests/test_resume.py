from app.resume.claim_checker import check_resume_claims
from app.resume.docx_writer import write_docx
from app.resume.generator import ExperienceBlock, generate_resume_content
from app.resume.pdf_writer import write_pdf
from app.resume.txt_writer import write_txt


JD = """We are hiring a Backend Software Engineer to build REST APIs in Python
using FastAPI and PostgreSQL, deployed via Docker with CI/CD pipelines."""


def test_generate_resume_only_uses_verified_data(sample_profile):
    resume = generate_resume_content(sample_profile, "Backend Software Engineer", JD)
    assert resume.full_name == "Test Candidate"
    assert "python" in resume.skills_ordered
    assert resume.experience[0].company == "Widget Software Inc"
    for bullet in resume.experience[0].bullets:
        assert bullet in sample_profile.employment[0].verified_bullets


def test_claim_checker_passes_for_generated_resume(sample_profile):
    resume = generate_resume_content(sample_profile, "Backend Software Engineer", JD)
    violations = check_resume_claims(resume, sample_profile)
    assert violations == []


def test_claim_checker_blocks_fabricated_bullet(sample_profile):
    resume = generate_resume_content(sample_profile, "Backend Software Engineer", JD)
    resume.experience[0].bullets.append("Led a team of 50 engineers to launch a rocket to Mars.")
    violations = check_resume_claims(resume, sample_profile)
    assert any("rocket to Mars" in v for v in violations)


def test_claim_checker_blocks_fabricated_skill(sample_profile):
    resume = generate_resume_content(sample_profile, "Backend Software Engineer", JD)
    resume.skills_ordered.append("Kubernetes")
    violations = check_resume_claims(resume, sample_profile)
    assert any("Kubernetes" in v for v in violations)


def test_claim_checker_blocks_fabricated_employer(sample_profile):
    resume = generate_resume_content(sample_profile, "Backend Software Engineer", JD)
    resume.experience.append(
        ExperienceBlock(company="Fake Corp", title="Staff Engineer", start_date="2020", end_date="2021", location="", bullets=[])
    )
    violations = check_resume_claims(resume, sample_profile)
    assert any("Fake Corp" in v for v in violations)


def test_docx_pdf_txt_generation(sample_profile, tmp_path):
    resume = generate_resume_content(sample_profile, "Backend Software Engineer", JD)

    docx_path = write_docx(resume, tmp_path / "resume.docx")
    pdf_path = write_pdf(resume, tmp_path / "resume.pdf")
    txt_path = write_txt(resume, tmp_path / "resume.txt")

    assert docx_path.exists() and docx_path.stat().st_size > 0
    assert pdf_path.exists() and pdf_path.stat().st_size > 0
    assert txt_path.exists() and "Test Candidate" in txt_path.read_text()
