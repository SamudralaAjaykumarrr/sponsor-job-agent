"""Selects the right ApplicationProvider for a job's ATS. Mirrors
app.providers.registry's shape but is a wholly separate registry -- see
app.applications.provider's module docstring for why the two interfaces must
never be merged."""

from app.applications.mock_ats import MockATSProvider
from app.applications.provider import ApplicationProvider
from app.applications.providers_ashby import AshbyApplicationProvider
from app.applications.providers_generic import GenericAssistOnlyProvider
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.applications.providers_lever import LeverApplicationProvider
from app.applications.providers_smartrecruiters import SmartRecruitersApplicationProvider
from app.applications.providers_workable import WorkableApplicationProvider
from app.applications.providers_workday import WorkdayApplicationProvider
from app.models import Job

_PROVIDERS: dict[str, ApplicationProvider] = {
    "mock_ats": MockATSProvider(),
    "greenhouse": GreenhouseApplicationProvider(),
    "lever": LeverApplicationProvider(),
    "ashby": AshbyApplicationProvider(),
    "workday": WorkdayApplicationProvider(),
    "smartrecruiters": SmartRecruitersApplicationProvider(),
    "workable": WorkableApplicationProvider(),
}

_GENERIC = GenericAssistOnlyProvider()


def get_application_provider(job: Job) -> ApplicationProvider:
    provider = _PROVIDERS.get((job.provider or "").lower())
    if provider is not None and provider.detect_application(job):
        return provider
    return _GENERIC


def generic_provider_names() -> list[str]:
    """Provider Post-Approval Execution V1: every discovery-side provider
    name (app.providers.registry.all_provider_names(), e.g. ashby/workday/
    smartrecruiters/workable/bamboohr/breezy/recruitee/comeet) that does NOT
    have a dedicated app-layer ApplicationProvider and therefore falls
    through to GenericAssistOnlyProvider in get_application_provider() above
    -- purely derived from the two existing registries (no new data), so the
    application-capability-matrix UI can honestly say WHICH real providers
    the single 'generic' row actually covers instead of leaving a reader to
    guess. mock_ats is not a discovery provider, so it's naturally excluded."""
    from app.providers.registry import all_provider_names

    return sorted(name for name in all_provider_names() if name not in _PROVIDERS)


def all_application_capabilities() -> list[dict]:
    caps = [p.get_capabilities().as_dict() for p in _PROVIDERS.values()]
    generic = _GENERIC.get_capabilities().as_dict()
    generic["covers_provider_names"] = generic_provider_names()
    caps.append(generic)
    return caps
