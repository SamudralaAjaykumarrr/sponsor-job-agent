"""CLAUDE.md Phase 8 sections 1-2: FULL_TIME hard gate + eligibility gate."""

import json

import pytest

from app.applications.eligibility import evaluate_executor_eligibility
from app.matching.employment_type import classify_employment_type
from app.models import ApplicationState, EmploymentType, Job, SponsorshipStatus


@pytest.fixture
def job_factory(tmp_path):
    answers_path = tmp_path / "application_answers.json"
    answers_path.write_text(json.dumps({
        "full_name": "Test Candidate", "email": "test@example.com", "phone": "555-0100",
        "do_you_require_sponsorship": "Yes, I will require H-1B sponsorship.",
    }))

    def _job(**overrides) -> Job:
        defaults = dict(
            title="Backend Software Engineer",
            company="Acme Corp",
            location="Remote - US",
            description="We are hiring a full-time backend engineer to build APIs.",
            employment_type="Full-time",
            sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
            application_state=ApplicationState.READY_TO_APPLY,
            technical_match_score=80.0,
            resume_docx_path="/tmp/out/1/resume.docx",
            resume_pdf_path="/tmp/out/1/resume.pdf",
            application_answers_path=str(answers_path),
        )
        defaults.update(overrides)
        return Job(**defaults)

    return _job


# --- classify_employment_type -----------------------------------------------

def test_classify_full_time_explicit_structured_field():
    assert classify_employment_type("Full-time", "Engineer", "") == EmploymentType.FULL_TIME


def test_classify_full_time_from_description_text():
    assert classify_employment_type("", "Engineer", "This is a full-time position.") == EmploymentType.FULL_TIME


def test_classify_contract_hard_signal():
    assert classify_employment_type("Contract", "Engineer", "") == EmploymentType.CONTRACT


def test_classify_c2c_signal():
    assert classify_employment_type("", "Engineer", "C2C only, no full time.") == EmploymentType.C2C


def test_classify_internship_signal():
    assert classify_employment_type("", "Software Engineering Intern", "internship position") == EmploymentType.INTERNSHIP


def test_classify_part_time_signal():
    assert classify_employment_type("Part-time", "Engineer", "") == EmploymentType.PART_TIME


def test_classify_unknown_when_silent():
    assert classify_employment_type("", "Backend Engineer", "Build great software with our team.") == EmploymentType.UNKNOWN


def test_classify_negative_signal_wins_even_with_structured_full_time_text():
    # A contract signal in the free text must not be masked by a coincidental
    # "full time" phrase elsewhere -- negative signals are checked first.
    assert classify_employment_type(
        "", "Engineer", "This is a contract-to-hire, full time equivalent hours role.",
    ) == EmploymentType.CONTRACT


# --- eligibility gate ---------------------------------------------------------

def test_eligibility_full_time_confirmed_sponsor_enters_queue_and_auto_eligible(job_factory):
    job = job_factory()
    result = evaluate_executor_eligibility(job)
    assert result.enters_queue
    assert result.auto_submit_eligible
    assert result.employment_type == EmploymentType.FULL_TIME
    assert not result.hard_skip


def test_eligibility_contract_hard_skips_before_executor(job_factory):
    job = job_factory(employment_type="Contract", description="This is a contract position.")
    result = evaluate_executor_eligibility(job)
    assert not result.enters_queue
    assert not result.auto_submit_eligible
    assert result.hard_skip
    assert result.blocking_category == "EMPLOYMENT_TYPE"
    assert "CONTRACT" in result.reasons[0]


def test_eligibility_unknown_employment_type_enters_queue_but_never_auto(job_factory):
    job = job_factory(employment_type="", description="Build great APIs with our platform team.")
    result = evaluate_executor_eligibility(job)
    assert result.employment_type == EmploymentType.UNKNOWN
    assert result.enters_queue
    assert not result.auto_submit_eligible


def test_eligibility_no_sponsorship_hard_skip(job_factory):
    job = job_factory(sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP)
    result = evaluate_executor_eligibility(job)
    assert not result.enters_queue
    assert result.hard_skip
    assert result.blocking_category == "SPONSORSHIP"


def test_eligibility_unknown_sponsorship_never_enters_queue_but_not_hard_skip(job_factory):
    job = job_factory(sponsorship_status=SponsorshipStatus.UNKNOWN)
    result = evaluate_executor_eligibility(job)
    assert not result.enters_queue
    assert not result.hard_skip
    assert result.blocking_category == "SPONSORSHIP"


def test_eligibility_likely_sponsor_enters_queue_never_auto_submit(job_factory):
    job = job_factory(sponsorship_status=SponsorshipStatus.LIKELY_SPONSOR, application_state=ApplicationState.REVIEW_REQUIRED)
    result = evaluate_executor_eligibility(job)
    assert result.enters_queue
    assert not result.auto_submit_eligible


def test_eligibility_non_us_location_hard_skip(job_factory):
    job = job_factory(location="London, United Kingdom")
    result = evaluate_executor_eligibility(job)
    assert not result.enters_queue
    assert result.hard_skip
    assert result.blocking_category == "LOCATION"


def test_eligibility_missing_resume_artifacts_blocks_queue(job_factory):
    job = job_factory(resume_docx_path=None, resume_pdf_path=None)
    result = evaluate_executor_eligibility(job)
    assert not result.enters_queue
    assert not result.hard_skip
    assert result.blocking_category == "RESUME"


def test_eligibility_terminal_applied_state_hard_blocks(job_factory):
    job = job_factory(application_state=ApplicationState.APPLIED)
    result = evaluate_executor_eligibility(job)
    assert not result.enters_queue
    assert result.hard_skip
    assert result.blocking_category == "STATE"
