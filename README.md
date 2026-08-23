# Sponsor Job Agent

A local, AI-assisted job application system for a U.S. software engineer who needs
H-1B/employment-based sponsorship. It finds fresh technical job postings, filters hard
for sponsorship and full-time eligibility, generates a truthful JD-specific resume for
each strong match, and assists (never blindly automates) the application itself — through
one local dashboard.

Release candidate: **15.0.0-rc1** (see `/version`). Default pytest baseline: **1160
passing** (0 failing). See `docs/release-candidate-audit.md` for the full Phase 15
acceptance record.

**One-click agent**: the dashboard now has a single START AGENT / STOP AGENT control
that coordinates discovery, one-page resume generation, application preparation, and
execution end to end — see "The one-click agent" below and `docs/one-click-agent.md`.

## What this does

- Discovers U.S. technical jobs from public, unauthenticated ATS job-board APIs
  (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, and more — see
  `docs/provider-development.md`) plus manual JD paste.
- Classifies **sponsorship**: `CONFIRMED_SPONSOR` / `LIKELY_SPONSOR` (review only) /
  `UNKNOWN` (never auto-applied) / `NO_SPONSORSHIP` (hard skip). Historical H-1B filing
  data can only ever promote `UNKNOWN → LIKELY_SPONSOR`, never create `CONFIRMED_SPONSOR`
  and never override a current job's explicit `NO_SPONSORSHIP`. See `docs/sponsorship_rules.md`.
- Classifies **employment type**: only a positively-classified `FULL_TIME` job may reach
  automated application preparation/submission. `CONTRACT`/`C2C`/`PART_TIME`/`INTERNSHIP`/
  `TEMPORARY`/`SEASONAL`/`FREELANCE` are hard-skipped for that path; `UNKNOWN` stays visible
  for manual review, never auto-progressed.
- Prioritizes **Remote > Hybrid > Onsite**, weighted by sponsorship confidence and JD match.
- Analyzes each JD's required/preferred skills against a **verified candidate evidence
  graph** (`candidate_data/profile.json`) and generates a **truthful, JD-specific resume**
  (DOCX/PDF/TXT) — every claim is checked against verified evidence before it can reach
  the document; unsupported skills become a documented gap, never a claim.
- Validates that generated resumes actually **parse cleanly** (ATS text extraction) and
  **lay out sanely** (page count, no blank pages, headings/bullets render, contact info
  readable) before marking a resume READY.
- Prepares applications and, for real ATS providers, drives an **ASSIST-only browser
  session** (Playwright) that fills forms and stops for the user at CAPTCHA, login/MFA,
  legal/attestation questions, and the final submit click — it never submits on your
  behalf against a real employer.
- Tracks every job/application through one **unified local dashboard**.

## What this does NOT do

- **No automatic submission to a real employer**, ever, in this codebase as shipped.
  `mock_ats` (an in-process deterministic test fixture) is the only provider with
  `submission_supported=True`. Every real ATS connector is `ASSIST_ONLY`. Run
  `python scripts/generate_provider_matrix.py` any time to see this recomputed live from
  the code itself, not a stale claim.
- No CAPTCHA bypass, no MFA interception, no anti-bot evasion, no credential capture, no
  scraping behind a login wall.
- No fabricated resume content — no invented skills, employers, titles, dates, years of
  experience, certifications, metrics, or immigration status. A skill you haven't verified
  becomes a documented gap, never a claim.
- No guaranteed ATS score, interview, or job offer. Internal JD-alignment diagnostics
  (`docs/resume-quality-diagnostics.md`) are transparency tooling, not a proprietary ATS's
  real scoring engine and not a probability of anything.
- No LinkedIn/Indeed automation.

## Quick start

```bash
git clone <this-repo>
cd sponsor-job-agent
cp .env.example .env          # optional -- sane defaults work with no .env at all
./start.sh                    # creates .venv, installs deps, runs migrations, starts the dashboard
```

Open <http://127.0.0.1:8000>. On first run, `candidate_data/profile.json` is created with
every personal field set to `NEEDS_USER_INPUT` — edit it with your own real, verifiable
facts (see `candidate_data/README.txt`, written alongside it). The agent, application
executor, browser assist, and auto-submit are all **off by default** — nothing in this
project starts submitting or even discovering jobs until you explicitly enable it in `.env`.

To manually try the pipeline without any live discovery: open the dashboard, use "Ingest
Job" to paste a JD, and watch it flow through sponsorship/employment-type classification,
scoring, and (for an eligible job) resume generation.

## Configuration

All configuration is environment variables, loaded from `.env` (see `.env.example` for
every documented option) with safe defaults if unset. Three usage profiles:

| Profile | `DATABASE_URL` | Notes |
|---|---|---|
| `LOCAL_DEVELOPMENT` | unset | SQLite at `data/app.db` — zero setup, the default |
| `LOCAL_POSTGRES` | `postgresql://...` (local) | exercises the same code path production uses, still on your machine |
| `PRODUCTION` | `postgresql://...` (managed) | recommended backend for any shared/always-on deployment — see `docs/deployment-postgres.md`, `docs/postgres-backend.md` |

Dangerous capabilities stay off until you deliberately opt in, and stay off across
upgrades (a `git pull` never silently turns one on): `AGENT_ENABLED`,
`APPLICATION_EXECUTOR_ENABLED`, `APPLICATION_AUTO_PREPARE_ENABLED`,
`BROWSER_ASSIST_ENABLED`, **`AUTO_SUBMIT_ENABLED`**, `REAL_ATS_CANARY_ENABLED` all default
to `false`. Run `python -m app.config_doctor` (or the global doctor, below) to sanity-check
your configuration.

## The one-click agent

```
OPEN WEBSITE -> click START AGENT -> leave it running
```

One button on the dashboard starts a background orchestrator that continuously finds
eligible jobs, generates a unique one-page truthful resume per job, prepares the
application, and executes it as far as the provider safely/permittedly supports —
auto-submitting only where a legitimate, verified submission capability actually exists
(today, only the deterministic `mock_ats` test fixture; every real ATS stays ASSIST-only
until proven otherwise). Everything else pauses at `NEEDS_USER_ACTION`/
`READY_FOR_FINAL_SUBMIT` and shows up in the dashboard's "Needs Your Action" queue. A
separate **START AGENT (TEST MODE)** button proves the entire loop end to end — discover,
analyze, one-page resume, prepare, submit, confirm, `APPLIED` — against the safe
`mock_ats` fixture only, never a real employer. See `docs/one-click-agent.md`,
`docs/one-page-resume-contract.md`, and `docs/autonomous-orchestration.md`.

Every automatically-generated resume is enforced to render as exactly one PDF page, via a
bounded, truthfulness-preserving compression ladder (never a font shrunk below
readability, never a fabricated claim) — see `docs/one-page-resume-contract.md`.

## The dashboard

`/` is the primary, single-page workflow — company, role, freshness, work arrangement,
FULL_TIME status, sponsorship, JD coverage, resume status, ATS/provider, application
status, required user action, and priority, all in one table with summary counts at the
top (discovered / eligible / sponsor-confirmed / high-alignment / resume-ready /
application-ready / needs-user-action / applied). Specialist pages (`/registry`,
`/applications`, `/fleet`, `/sponsorship/review-queue`, the various doctor/capability-matrix
pages) exist for deeper operational drill-down but are never required for everyday use.
See `docs/unified-dashboard.md`.

## Doctors and health

- `python -m app.doctor` — global doctor: aggregates every subsystem doctor (registry,
  sponsorship, applications, resume optimizer, agent orchestrator) plus database/schema,
  candidate-profile, configuration, and job-integrity checks. Exits nonzero on any serious
  issue. Read-only.
- `GET /health` — liveness only, never touches the database.
- `GET /readiness` — database reachable + schema compatible.
- `GET /version` — app/schema/optimizer/classifier/provider-capability version identifiers.
- `GET /metrics` — Prometheus text exposition, no PII.
- `python scripts/secret_scan.py` — deterministic local secret/private-artifact scan.
- `./scripts/release_acceptance.sh` (or `python -m app.acceptance`) — the full
  release-candidate acceptance run: compile check, gitignore audit, secret scan, a fresh
  throwaway-DB migration check, the global doctor, and the default/PostgreSQL/browser
  pytest suites (the latter two skip honestly if their optional dependency isn't
  available). Never submits a real application, never requires internet, never touches or
  destroys real data.
- `python scripts/phase15_release_benchmark.py [--include-100k]` — synthetic-data
  performance benchmark (registry queries, JD analysis, resume generation, ATS parse
  validation, dashboard queries, application-queue claim) against an isolated temp
  database. Engineering latency/throughput only — never a claim about interview or hiring
  outcomes. See `docs/release-candidate-audit.md` §5-6 for the last recorded results,
  including a genuine N+1 query fix and a dashboard result-set cap it uncovered.

## Tests

```bash
pytest                       # default suite -- 1160 passing, no network/DB server/browser required
pytest -m postgres           # requires `pip install -r requirements-dev.txt` (bundles a local pgserver binary)
pytest -m browser            # requires `pip install -r requirements-dev.txt` && playwright install chromium
```

Both optional suites skip cleanly with a precise reason if their dependency isn't present.
See `docs/troubleshooting.md` if Chromium fails to launch on a Linux host with no root.

## Privacy

`candidate_data/`, `data/app.db` and friends, `output/`, `data/private/`, browser-assist
runtime/session directories, and `.env` are all gitignored — see `docs/data-retention.md`
for exactly what's persisted (and what deliberately never is: credentials, MFA codes,
CAPTCHA tokens, long-lived cookies, raw form HTML). `python scripts/secret_scan.py` checks
this mechanically before every release-candidate pass.

## Documentation

Start at `docs/README.md` — the full documentation index (architecture, setup,
configuration, each subsystem, operations, deployment, troubleshooting, acceptance
verification).

## Limitations

See `docs/release-candidate-audit.md` "Release-candidate limitations" for the complete,
current list. Highlights: real-ATS automatic final submission remains unsupported unless
explicitly proven otherwise per-provider; CAPTCHA/MFA/login/legal questions always require
a human; Workday behavior is tenant-specific and tracked per-tenant, never generalized from
one; historical sponsorship evidence is prioritization signal, never current-job proof; no
guarantee of interview, job, or offer; live provider interfaces can and do change.

## Status

This is a completed release-candidate build (Phase 15 of its own development plan — see
`docs/release-candidate-audit.md`). No further large numbered phase is planned; ongoing
work is normal maintenance, bug fixes, and optional future features tracked as such.
