"""Truthful provider capability matrix (CLAUDE.md Phase 9 section 44). Pure
presentation over app.applications.provider_registry.all_application_capabilities()
-- never invents or infers a capability; every row is exactly what each
provider's `ApplicationCapabilities` dataclass truthfully declares."""

from app.applications.provider_registry import all_application_capabilities

_COLUMNS = [
    ("provider", "Provider"),
    ("form_discovery_supported", "Application page / form schema detected"),
    ("field_mapping_supported", "Field mapping"),
    ("draft_fill_supported", "Draft assist"),
    ("file_upload_supported", "Upload assist"),
    ("submission_supported", "Auto-submit"),
    ("confirmation_detection_supported", "Confirmation detection"),
    ("live_validated", "Live tested"),
    ("support_level", "Support level"),
    ("automation_policy", "Automation policy"),
    ("confirmation_recheck_supported", "Reconciliation recheck"),
    ("notes", "Reason / limitation"),
]


def build_matrix() -> dict:
    return {"columns": _COLUMNS, "rows": all_application_capabilities()}


def render_text() -> str:
    caps = all_application_capabilities()
    lines = ["Application Provider Capability Matrix", "=" * 40]
    for c in caps:
        lines.append(f"\nProvider: {c['provider']} (v{c['provider_version']})")
        lines.append(f"  Support level:            {c['support_level']}")
        lines.append(f"  Automation policy:        {c['automation_policy']}")
        lines.append(f"  Form discovery:           {c['form_discovery_supported']}")
        lines.append(f"  Field mapping:            {c['field_mapping_supported']}")
        lines.append(f"  Draft fill:               {c['draft_fill_supported']}")
        lines.append(f"  File upload assist:       {c['file_upload_supported']}")
        lines.append(f"  Auto-submit:              {c['submission_supported']}")
        lines.append(f"  Confirmation detection:   {c['confirmation_detection_supported']}")
        lines.append(f"  Reconciliation recheck:   {c['confirmation_recheck_supported']}")
        lines.append(f"  Live tested:              {c['live_validated']}")
        lines.append(f"  Notes:                    {c['notes']}")
    return "\n".join(lines) + "\n"
