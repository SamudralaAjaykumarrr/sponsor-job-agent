# Troubleshooting (CLAUDE.md Phase 15 section 80)

Factual fixes only, for issues actually observed during this project's own development
and Phase 15 acceptance testing.

## Chromium fails to launch: `libnspr4.so: cannot open shared object file`

Playwright's bundled Chromium needs a few Linux shared libraries
(`libnspr4`, `libnss3`, `libnssutil3`, `libasound.so.2` and similar) that a minimal/
sandboxed host may not have. The documented fix, `playwright install-deps chromium`,
needs root.

**Non-root workaround** (genuinely used to run the real-browser suite during Phase 10 and
again during Phase 15 acceptance, in an environment with no root access):

```bash
mkdir -p /tmp/chromium-libs && cd /tmp/chromium-libs
apt-get download libnspr4 libnss3 libasound2t64   # package names as of Ubuntu 24.04 "noble"
for f in *.deb; do dpkg-deb -x "$f" extracted; done
export LD_LIBRARY_PATH="/tmp/chromium-libs/extracted/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
pytest -m browser
```

`apt-get download` (unlike `apt-get install`) only fetches the `.deb` into the current
directory and needs no root; `dpkg-deb -x` extracts it as a plain archive. Package names
vary by distro/version -- run `ldd` against the failing binary
(`~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/
chrome-headless-shell`) to see exactly which `.so` names are missing, then `apt-cache
search <name>` to find the current package that ships each one (e.g. `libasound2` was
renamed `libasound2t64` on Ubuntu 24.04).

Default `pytest` (no `-m` filter) never depends on this -- every browser-marked test skips
cleanly with a precise reason if Chromium can't launch.

## PostgreSQL unavailable

`pytest -m postgres` uses `pgserver` (a bundled, unmodified real Postgres binary, no
Docker/root needed) -- install it via `pip install -r requirements-dev.txt`. If that
package itself fails to install or run in your environment, `pytest -m postgres` is
skipped automatically (not a code defect); default `pytest` never requires it.

For a real deployment against your own PostgreSQL: verify `DATABASE_URL` is a genuine
`postgresql://` DSN and the server is reachable (`psql "$DATABASE_URL" -c 'select 1'`)
before starting the app -- `/readiness` will otherwise correctly report `database_reachable:
false`.

## Port already in use

`./start.sh` binds `127.0.0.1:8000`. Find and stop whatever's already bound
(`lsof -i :8000` / `ss -ltnp | grep 8000`), or override the port by running uvicorn
directly: `uvicorn app.main:app --host 127.0.0.1 --port 8001`.

## A provider shows a CAPTCHA during browser-assist

Expected and by design (CLAUDE.md Phase 15 section 52) -- the session pauses
(`PAUSED_CAPTCHA`, surfaced as `NEEDS_USER_ACTION`) for you to solve it in the visible
browser window, then resume. This project never attempts to solve or bypass a CAPTCHA.

## Workday behavior looks inconsistent between two tenants (or two runs of the same tenant)

Expected -- `app.applications.workday_tenant` tracks capability per `(tenant, site)`, never
as one blanket "Workday works" claim, and reports `VARIABLE` honestly when repeated runs of
the same URL disagree (CLAUDE.md Phase 12 section 27). See `docs/workday-observation-model.md`.

## Provider schema drift warning

A provider's response shape changed since the connector was written (e.g. a renamed JSON
field). The connector fails safely for that tenant (isolated, doesn't abort the discovery
cycle) and records a structural signature in `provider_schema_drift` -- see
`docs/schema_drift` handling in `docs/production-observability.md`. Fix requires a code
change to the specific provider connector once the new shape is confirmed.

## A resume shows STALE / a job shows a resume/JD mismatch

The JD changed after the resume was generated. This is the intended safety behavior
(CLAUDE.md Phase 13 sections 43-45) -- regenerate the resume from the dashboard's
Generate/Regenerate action before proceeding with that job's application.

## An execution is stuck at `SUBMISSION_STATUS_UNKNOWN`

A submission request may or may not have reached the provider (e.g. a dropped connection
mid-request). This is never auto-retried. Resolve it explicitly via
`python -m app.applications.cli reconcile <execution_id> --resolution ...` or the
dashboard's reconciliation action, after checking independently (e.g. the employer's own
site/email) whether the application actually went through. See
`docs/application-reconciliation.md`.
