# Sponsor Job Agent — Master Build Specification

Build a local AI-assisted U.S. job application system for a software engineer with about 3 years of backend/software engineering experience.

Main goals:

- Find fresh U.S. technical jobs
- Prioritize Remote > Hybrid > Onsite
- Process only CS/STEM-related jobs
- Require H-1B sponsorship compatibility
- Prefer explicit sponsorship
- Allow likely sponsors only for review
- Hard-skip jobs that say no sponsorship
- Analyze every JD
- Generate a new truthful resume for each JD
- Use only verified candidate experience and skills
- Never invent skills, employers, metrics, years of experience, certifications, or immigration details
- Generate DOCX and PDF resumes
- Generate screener-answer suggestions
- Track applications
- Build a local FastAPI dashboard
- Default mode should be ASSIST, not blind auto-apply

## Sponsorship Rules

CONFIRMED_SPONSOR:
The job or employer explicitly says sponsorship is available.

LIKELY_SPONSOR:
Employer has recent H-1B history but the specific job does not explicitly confirm it.

UNKNOWN:
Not enough evidence.

NO_SPONSORSHIP:
The job says no sponsorship, unable to sponsor, or must not require sponsorship now or in the future.

Rules:

- CONFIRMED_SPONSOR -> eligible
- LIKELY_SPONSOR -> review only
- UNKNOWN -> do not apply
- NO_SPONSORSHIP -> hard skip

Historical sponsorship alone is not proof that a specific role sponsors.

## Job Priority

Highest priority:

Remote + confirmed sponsor + strong technical match

Then:

Remote + likely sponsor
Hybrid + confirmed sponsor
Hybrid + likely sponsor
Onsite + confirmed sponsor
Onsite + likely sponsor

A remote job with no sponsorship must still be skipped.

## Target Roles

Primary:

- Software Engineer
- Software Engineer II
- Backend Engineer
- Backend Software Engineer
- Python Engineer
- Python Developer
- API Engineer
- Platform Engineer
- Cloud Software Engineer
- Application Engineer

Secondary if strongly related to the candidate's background:

- DevOps Engineer
- Cloud Engineer
- Infrastructure Engineer
- SDET
- QA Automation Engineer
- Systems Engineer
- Data Platform Engineer

Do not process unrelated non-STEM jobs.

## Resume Rules

For each strong job:

JD
-> extract requirements
-> match verified candidate evidence
-> select strongest relevant experience
-> select strongest projects
-> reorder skills
-> rewrite truthful bullets for the JD
-> check all claims
-> generate DOCX/PDF/text resume

Never fabricate anything.

Every material resume claim must be supported by the verified candidate profile.

If a skill is not verified, mark it as a gap instead of claiming it.

## Freshness

Track:

- published_at when reliable
- first_seen_at always

Priority:

- 0–60 minutes: maximum
- 1–3 hours: very high
- 3–12 hours: high
- 12–24 hours: moderate
- older: lower

## Candidate Data

Create private candidate files for:

- contact information
- employment history
- skills
- projects
- education
- work authorization
- sponsorship requirement
- relocation preference
- salary preference
- standard application answers

Missing personal facts must become:

NEEDS_USER_INPUT

Do not guess them.

## Application Modes

ANALYZE:
analyze only

ASSIST:
generate everything and mark READY_TO_APPLY

AUTO:
future use only for interfaces where automation is explicitly permitted

Default mode:

ASSIST

Do not automate LinkedIn or Indeed submissions.

Do not bypass CAPTCHA, MFA, authentication, rate limits, or anti-bot protections.

## Tech Stack

Use:

- Python 3.12
- FastAPI
- Pydantic
- SQLite
- Jinja
- httpx
- python-docx
- ReportLab or equivalent
- pytest

Use a modular monolith.

Do not add React, Kafka, Redis, Kubernetes, or microservices for the MVP.

## Dashboard

Show:

- company
- role
- location
- remote/hybrid/onsite
- freshness
- sponsorship status
- technical match
- overall priority
- application state

Filters:

- Remote
- Hybrid
- Onsite
- Confirmed Sponsor
- Likely Sponsor
- Fresh < 1 hour
- High Priority
- Ready To Apply
- Applied
- Interview

## Output Per Job

Create:

- resume.docx
- resume.pdf
- resume.txt
- job_analysis.json
- application_answers.json
- cover_letter.txt when useful

Track exactly which resume belongs to which application.

## Acceptance Criteria

Do not claim the project is complete unless:

- app starts
- dashboard loads
- manual JD ingestion works
- sponsorship classification works
- no-sponsorship jobs are skipped
- work arrangement classification works
- freshness tracking works
- scoring works
- high-priority jobs are identified
- tailored resume generation works
- DOCX works
- PDF works
- unsupported claims are blocked
- application answers work
- unknown factual answers become NEEDS_USER_INPUT
- tracking works
- tests pass
- ./start.sh works
- no secrets are committed

## Build Behavior

First create planning documents under docs/.

Then immediately continue into implementation.

Do not stop after planning.

Create files, install normal dependencies, run tests, fix failures, and continue until the MVP works.

Do not ask routine coding questions.

Only stop if a genuinely personal factual answer is required.

Never report a feature as working unless it was actually tested.