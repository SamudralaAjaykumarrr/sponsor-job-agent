import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.candidate.schema import CandidateProfile


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Redirect all app data/output/candidate/db paths into a tmp dir so tests
    never touch the real project's data/output/candidate_data."""
    import app.config as config

    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    candidate_dir = tmp_path / "candidate_data"
    data_dir.mkdir()
    output_dir.mkdir()
    candidate_dir.mkdir()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "CANDIDATE_DIR", candidate_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "app.db")
    monkeypatch.setattr(config, "KNOWN_SPONSORS_PATH", data_dir / "known_h1b_sponsors.json")

    known_sponsors = {"employers": ["Acme Corp", "Globex", "Initech"]}
    (data_dir / "known_h1b_sponsors.json").write_text(json.dumps(known_sponsors))

    import app.db as db
    import app.jobs_repo as jobs_repo
    import app.candidate.profile as profile_mod
    import app.sponsorship.classifier as sponsorship_classifier
    import app.pipeline as pipeline

    monkeypatch.setattr(db, "DB_PATH", data_dir / "app.db")
    monkeypatch.setattr(jobs_repo, "db_session", db.db_session)
    monkeypatch.setattr(profile_mod, "CANDIDATE_DIR", candidate_dir)
    monkeypatch.setattr(profile_mod, "PROFILE_PATH", candidate_dir / "profile.json")
    monkeypatch.setattr(sponsorship_classifier, "KNOWN_SPONSORS_PATH", data_dir / "known_h1b_sponsors.json")
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", output_dir)

    db.init_db()

    return {
        "data_dir": data_dir,
        "output_dir": output_dir,
        "candidate_dir": candidate_dir,
    }


@pytest.fixture
def mock_httpx(monkeypatch):
    """Globally routes every httpx.Client constructed for the duration of a
    test through an httpx.MockTransport handler -- needed for Phase 5 worker
    tests, where app.workers.runner and app.providers.registry each build
    their own httpx.Client internally (no injection point), unlike the
    Phase 3/4 tests that pass a client= directly to a provider/probe."""

    def _install(handler):
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)

    return _install


@pytest.fixture
def sample_profile() -> CandidateProfile:
    """Synthetic, clearly-fake candidate profile used ONLY as a test fixture --
    not real candidate data. Exercises the 'verified evidence only' pipeline."""
    return CandidateProfile.model_validate(
        {
            "contact": {
                "full_name": "Test Candidate",
                "email": "test.candidate@example.com",
                "phone": "555-000-1111",
                "city": "Austin",
                "state": "TX",
                "linkedin_url": "https://linkedin.com/in/testcandidate",
                "github_url": "https://github.com/testcandidate",
                "portfolio_url": "",
            },
            "employment": [
                {
                    "company": "Widget Software Inc",
                    "title": "Backend Software Engineer",
                    "start_date": "2022-06",
                    "end_date": "Present",
                    "location": "Remote",
                    "verified_bullets": [
                        "Built and maintained REST APIs in Python using FastAPI serving 2M requests/day.",
                        "Designed PostgreSQL schema migrations for a multi-tenant billing system.",
                        "Automated deployment pipelines with Docker and GitHub Actions CI/CD.",
                    ],
                    "skills_used": ["python", "fastapi", "rest api", "postgresql", "docker", "ci/cd", "git"],
                }
            ],
            "skills": [
                "python", "fastapi", "django", "rest api", "postgresql", "docker",
                "ci/cd", "git", "aws", "sql", "unit testing", "pytest",
            ],
            "projects": [
                {
                    "name": "Job Tracker CLI",
                    "description": "A command-line tool to track personal job applications.",
                    "verified_bullets": [
                        "Implemented a SQLite-backed CLI in Python with pytest test coverage.",
                    ],
                    "skills_used": ["python", "sqlite", "pytest"],
                    "url": "https://github.com/testcandidate/job-tracker-cli",
                }
            ],
            "education": [
                {
                    "school": "State University",
                    "degree": "B.S.",
                    "field_of_study": "Computer Science",
                    "graduation_date": "2022-05",
                }
            ],
            "work_authorization": {
                "current_status": "F-1 OPT",
                "requires_sponsorship": True,
                "sponsorship_type_needed": "H-1B",
                "years_us_experience": 3,
            },
            "preferences": {
                "relocation_open": False,
                "preferred_locations": ["Remote"],
                "salary_min_usd": 110000,
                "salary_preference_notes": "",
                "work_arrangement_priority": ["REMOTE", "HYBRID", "ONSITE"],
            },
            "standard_answers": {
                "years_of_experience": 3,
                "notice_period": "2 weeks",
                "willing_to_relocate": False,
                "requires_sponsorship_answer": "Yes, I will require H-1B sponsorship.",
                "veteran_status": "I am not a veteran",
                "disability_status": "I do not have a disability",
                "race_ethnicity": "Prefer not to say",
                "gender": "Prefer not to say",
            },
        }
    )
