"""Structured logging (CLAUDE.md Phase 6 sections 35-36).

Emits one JSON object per log line with: timestamp, level, component
(logger name), message, and whatever correlation fields the call site
attaches via `extra={...}` -- worker_id, attempt_id, portal_id, provider,
tenant, duration_ms, event, error_type. The correlation id that ties a
polling attempt -> provider request -> job normalization -> pipeline ->
application package generation together is `poll_attempts.attempt_id`
itself (also stored on the resulting `jobs.correlation_id` column) -- no
second, parallel id scheme.

Hard rule, enforced by NEVER passing these fields through `extra=` anywhere
in this codebase (see the module docstring's own audit note): never log
candidate email, candidate phone, resume contents, secrets, or database
passwords/DSNs. This module doesn't (and structurally can't) redact
arbitrary message content -- the discipline is that call sites never pass
candidate data into a log call in the first place, same as before Phase 6."""

import json
import logging
import sys
from datetime import datetime, timezone

# Fields a caller may attach via `extra={...}` that get promoted into the
# structured JSON output. Anything else in `extra` is ignored -- this is a
# deliberate allowlist, not a denylist, so a future accidental
# `extra={"candidate_email": ...}` doesn't silently start appearing in logs.
_STRUCTURED_FIELDS = (
    "worker_id", "attempt_id", "correlation_id", "portal_id", "portal_type",
    "provider", "tenant", "duration_ms", "event", "error_type", "component",
)


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_structured_logging(level: int = logging.INFO) -> None:
    """Idempotent: safe to call more than once (e.g. once from app.main's
    startup and once from a worker CLI entrypoint in the same process) --
    clears any handlers this function itself previously installed rather
    than stacking duplicate handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        if getattr(existing, "_sponsor_job_agent_structured", False):
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(StructuredFormatter())
    handler._sponsor_job_agent_structured = True
    root.addHandler(handler)
