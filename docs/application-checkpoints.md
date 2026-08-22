# Application Checkpoints (Phase 13)

CLAUDE.md Phase 13 sections 37-39, 62.

## What already existed (unchanged)

`browser_assist_sessions.status`/`stage` already IS the session's current
checkpoint, and the existing reconstruct-and-resume mechanism (Phase 11) is
how this project recovers from a crash: `resume_session()` reopens the
session's saved `application_url` in a fresh browser and rediscovers from
scratch — it never claims cross-process browser reattachment. Nothing about
that mechanism changes in Phase 13.

## What's new: an ordered checkpoint HISTORY

`app.applications.checkpoints` adds an append-only log of the meaningful,
REVERSIBLE stages a session has passed through — `CheckpointStage`:
`ENTRY_REACHED`, `FORM_DISCOVERED`, `FIELDS_PREPARED`, `FILE_READY`,
`STEP_COMPLETED`, `READY_FOR_FINAL_SUBMIT`, `USER_ACTION_REQUIRED`.

This is a pure **observability** layer:

- Recording a checkpoint never itself performs recovery.
- Recording is best-effort — `record_checkpoint()` never raises into a
  real discovery/fill pass, matching `app.applications.spa_events.record`'s
  own contract.
- `browser_assist._apply_discovery_outcome()` derives the checkpoint from
  the resulting session row (status/stage/mapped-field counts) after every
  discovery pass — approximate by design: it is an audit trail of stages
  reached, not a strict one-checkpoint-per-status-transition state machine.
  `FILE_READY` is not recorded as a separate event from `FIELDS_PREPARED`
  today — a file upload field is just another mapped field from this
  layer's point of view; a future phase could split this out if finer
  granularity is needed.

## Ordering-anomaly detection

`find_ordering_anomalies(session_id)` flags a checkpoint whose rank is
LOWER than the highest rank already seen earlier in the same session
(e.g. `READY_FOR_FINAL_SUBMIT` followed by `ENTRY_REACHED`) — advisory
only, surfaced by the doctor's `checkpoint_inconsistency` check, never
blocking. `USER_ACTION_REQUIRED` is explicitly exempted from ever counting
as a regression, since it can legitimately occur at almost any point.

## Relationship to reconstruction

A reconstruction (a fresh browser reopening the saved `application_url`)
can legitimately re-land on an earlier stage than where the session left
off — this is expected, not anomalous, exactly like
`app.applications.apply_entry.is_valid_stage_transition`'s own
`after_reconstruction=True` carve-out. The checkpoint log does not
currently special-case this the way that function does; a genuine
reconstruction-driven "backward" checkpoint would still be visible in the
log for a human to read in context (which reconstruction event preceded
it), even though `find_ordering_anomalies` would flag it. This is a
deliberate, documented limitation rather than added complexity to
special-case an audit trail that is advisory-only in the first place.

## Doctor coverage

`checkpoint_inconsistency` — surfaces any ordering anomaly found across
every session with checkpoint history.
