"""Selects the right ApplicationProvider for a job's ATS. Mirrors
app.providers.registry's shape but is a wholly separate registry -- see
app.applications.provider's module docstring for why the two interfaces must
never be merged."""

from app.applications.mock_ats import MockATSProvider
from app.applications.provider import ApplicationProvider
from app.applications.providers_generic import GenericAssistOnlyProvider
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.applications.providers_lever import LeverApplicationProvider
from app.models import Job

_PROVIDERS: dict[str, ApplicationProvider] = {
    "mock_ats": MockATSProvider(),
    "greenhouse": GreenhouseApplicationProvider(),
    "lever": LeverApplicationProvider(),
}

_GENERIC = GenericAssistOnlyProvider()


def get_application_provider(job: Job) -> ApplicationProvider:
    provider = _PROVIDERS.get((job.provider or "").lower())
    if provider is not None and provider.detect_application(job):
        return provider
    return _GENERIC


def all_application_capabilities() -> list[dict]:
    caps = [p.get_capabilities().as_dict() for p in _PROVIDERS.values()]
    caps.append(_GENERIC.get_capabilities().as_dict())
    return caps
