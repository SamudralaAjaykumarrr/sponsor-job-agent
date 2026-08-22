"""Phase 14: JD/resume optimization engine.

Top rule (CLAUDE.md Phase 14 section 1): every generated resume claim must
trace back to `app.candidate.schema.CandidateProfile` verified data and pass
`app.resume.claim_checker.check_resume_claims` -- this package NEVER bypasses
that checker, only feeds it better-selected, better-ordered, still-100%-
verified content. No universal ATS score is ever produced (section 2) --
only itemized, transparent coverage diagnostics.
"""
