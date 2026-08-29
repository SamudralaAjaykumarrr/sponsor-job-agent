"""Backfills a bare company-IDENTITY row (registry_companies -- name/domain
only) for an employer that already has a real job discovered in `jobs` but no
registry_companies row at all, so app.sponsorship.identity.resolve_company has
something to match evidence against.

This is deliberately NOT the ATS-portal acquisition/verification pipeline
(app.registry.importers/verification, which governs registry_portals /
company_registry and requires live verification before ACTIVE per CLAUDE.md
Phase 4). registry_companies is a separate, plain identity table with no
verification-state machine of its own -- adding a row here makes no claim
about ATS support and no claim about sponsorship; it is pure company identity
(display name + real, publicly-known primary domain), exactly like every
other registry_companies row already in this database.

Idempotent (matched on (normalized_name, primary_domain), same identity key
registry_companies' own unique index uses) and driven only by a small,
human-curated seed file (app.config.EMPLOYER_IDENTITY_SEED_PATH) -- never a
blind name->domain guess for an arbitrary company."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.registry import store as registry_store
from app.registry.models import Company


@dataclass
class IdentitySeedResult:
    created: int = 0
    already_present: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"created": self.created, "already_present": self.already_present}


def seed_missing_employer_identities(path: Optional[str | Path] = None) -> IdentitySeedResult:
    if path is None:
        from app.config import EMPLOYER_IDENTITY_SEED_PATH

        path = EMPLOYER_IDENTITY_SEED_PATH
    path = Path(path)
    result = IdentitySeedResult()
    if not path.exists():
        return result

    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("companies", []):
        existing = registry_store.get_company_by_identity(entry["normalized_name"], entry.get("primary_domain", ""))
        if existing is not None:
            result.already_present.append(entry["display_name"])
            continue
        registry_store.insert_company(Company(
            normalized_name=entry["normalized_name"],
            display_name=entry["display_name"],
            primary_domain=entry.get("primary_domain", ""),
            careers_home_url=entry.get("careers_home_url", ""),
            country=entry.get("country", ""),
        ))
        result.created += 1
    return result
