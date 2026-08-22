# Authoritative Provider Capability Matrix (CLAUDE.md Phase 15 section 71)

This is the one place that claims to be the merged, authoritative view across the three
capability truths this project maintains -- each of which remains its own independent
source of truth, never hand-duplicated here:

| Dimension | Single source of truth | What it answers |
|---|---|---|
| Discovery support | `app.providers.capabilities` / `app.providers.registry.all_capabilities()` | Can this project fetch jobs from this ATS at all, and how reliably? |
| Form/assist automation support | `app.applications.capability_matrix` (over `app.applications.provider_registry`) | Can this project prepare/fill/submit an application through this ATS? |
| Live browser verification evidence | `app.applications.browser_capability_matrix` | Has the generic browser-assist DOM engine actually been opened against a real, live posting on this provider? |

**This document does not hand-maintain any of that data.** Regenerate the live merge at
any time:

```bash
python scripts/generate_provider_matrix.py            # human-readable text
python scripts/generate_provider_matrix.py --json      # machine-readable
```

## The one fact that matters most

```
Providers with auto-submit=True: ['mock_ats']
```

`mock_ats` is an in-process, deterministic test fixture -- never a real employer's ATS.
**Every real ATS provider connector in this project is ASSIST_ONLY.** No code path exists
today that automatically submits an application to a real employer. See CLAUDE.md Phase 15
sections 5 and 72, and `docs/application-safety.md`.

## CAPTCHA / auth restrictions

Not a per-provider static property -- CAPTCHA/login/MFA presence is detected live, per
page, per session (`app.applications.browser_runtime`'s DOM-element-based CAPTCHA check;
`app.applications.domain_allowlist`/`apply_entry` for login/auth walls) and always pauses
for a human (`PAUSED_CAPTCHA`, `PAUSED_LOGIN_REQUIRED`), regardless of which provider is
involved. See `docs/ats-canary-validation.md`, `docs/real-ats-validation.md` for what real
public postings have actually shown a CAPTCHA during this project's own bounded, read-only
validation runs.

## Regenerating this snapshot

The output above (and a full per-provider table) is always live-computed, never frozen
into this file -- run `python scripts/generate_provider_matrix.py` yourself before relying
on it for a specific decision; a copy pasted into a doc would go stale the moment a
provider's declared support level changes.
