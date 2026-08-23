"""Agent orchestrator / one-page-resume metrics (CLAUDE.md one-click-agent
section 46). Follows this project's existing 'never an in-process counter,
always a live query over persisted state' convention (see
app/observability/metrics.py's own module docstring): every counter here is
computed by summing app.agent.run_state's durable, append-only
agent_cycle_log table (or, for agent_start_total/agent_stop_total, the
accumulating counter columns on the single-row agent_run_state table -- the
same accumulating-counter-column pattern app.workers.repo.heartbeat_worker
already uses for portals_processed/jobs_processed/errors). No PII labels."""

from app.agent import run_state


def collect() -> dict:
    run = run_state.get_run_state()
    totals = run_state.totals_since(hours=None)
    return {
        "agent_start_total": run["start_count"],
        "agent_stop_total": run["stop_count"],
        "agent_actual_state": run["actual_state"],
        "agent_cycles_total": totals["cycles"],
        "agent_jobs_processed_total": totals["jobs_processed"],
        "agent_resumes_generated_total": totals["resumes_generated"],
        "agent_applications_prepared_total": totals["applications_prepared"],
        "agent_applications_submitted_total": totals["applications_submitted"],
        "agent_user_action_total": totals["needs_user_action"],
        "agent_skipped_total": totals["skipped"],
        "one_page_resume_success_total": totals["one_page_success"],
        "one_page_resume_overflow_total": totals["one_page_overflow"],
        "one_page_resume_compression_total": totals["one_page_compression_events"],
    }
