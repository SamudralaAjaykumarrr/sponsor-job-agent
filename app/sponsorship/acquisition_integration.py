"""Wires real employer sponsorship evidence into registry acquisition
priority (CLAUDE.md Phase 7 section 32; the mechanism itself was built in
Phase 6 as `app.registry.acquisition_priority`, deliberately left
disconnected from any real evidence source until Phase 7 built one).

Durable rule, unchanged from Phase 6: this only ever affects which
companies/portals the registry verifies/polls SOONER. It is never consulted
by app.sponsorship.classifier or app.sponsorship.decision, and it never
touches a job's sponsorship_status."""

from app.db import db_session
from app.registry import store as registry_store
from app.sponsorship.profile import EmployerProfile
from app.sponsorship.schema import HistoricalStrength

# A company with SOME/STRONG_RECENT evidence is worth prioritizing sooner;
# OLD/NONE never receives the signal bonus, but critically is also never
# excluded/starved (CLAUDE.md Phase 7 section 32) -- every other existing
# app.registry.acquisition_priority input (US employer, support level,
# historical job yield, etc.) is portal/company-scoped data owned by the
# registry sync process, not by this module -- this function only ever
# writes the ONE boolean signal column that compute_priority() already
# knows how to read; it never recomputes/overwrites the full priority_score
# itself, since it doesn't have those other inputs available here.
_SIGNAL_STRENGTHS = {HistoricalStrength.STRONG_RECENT, HistoricalStrength.SOME}


def sync_acquisition_signal(company_id: int, profile: EmployerProfile) -> None:
    has_signal = profile.historical_strength in _SIGNAL_STRENGTHS
    with db_session() as conn:
        row = conn.execute("SELECT id FROM registry_companies WHERE id = ?", (company_id,)).fetchone()
        if row is None:
            return  # evidence company not (yet) a registry company -- nothing to sync
    registry_store.update_company(company_id, has_sponsorship_history_signal=has_signal)
