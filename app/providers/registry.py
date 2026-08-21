from app import config
from app.providers.ashby import AshbyProvider
from app.providers.bamboohr import BambooHRProvider
from app.providers.base import JobProvider
from app.providers.breezy import BreezyProvider
from app.providers.capabilities import ProviderCapabilities
from app.providers.comeet import CometProvider
from app.providers.greenhouse import GreenhouseProvider
from app.providers.lever import LeverProvider
from app.providers.recruitee import RecruiteeProvider
from app.providers.smartrecruiters import SmartRecruitersProvider
from app.providers.unsupported import (
    ICIMSProvider,
    JazzHRProvider,
    JobviteProvider,
    OracleRecruitingProvider,
    PinpointProvider,
    TeamtailorProvider,
)
from app.providers.workable import WorkableProvider
from app.providers.workday import WorkdayProvider

# Add new providers here -- each one just needs a JobProvider subclass with a
# fetch_jobs(max_jobs) method that isolates its own errors internally.
_PROVIDER_FACTORIES = {
    "greenhouse": lambda: GreenhouseProvider(config.GREENHOUSE_BOARD_TOKENS),
    "lever": lambda: LeverProvider(config.LEVER_COMPANY_SLUGS),
    "ashby": lambda: AshbyProvider(config.ASHBY_JOB_BOARD_NAMES),
    "workable": lambda: WorkableProvider(config.WORKABLE_ACCOUNT_SUBDOMAINS),
    "smartrecruiters": lambda: SmartRecruitersProvider(config.SMARTRECRUITERS_COMPANY_IDS),
    "bamboohr": lambda: BambooHRProvider(config.BAMBOOHR_SUBDOMAINS),
    "recruitee": lambda: RecruiteeProvider(config.RECRUITEE_SUBDOMAINS),
    "breezy": lambda: BreezyProvider(config.BREEZY_SUBDOMAINS),
    "comeet": lambda: CometProvider(config.COMEET_COMPANY_TOKENS),
    "workday": lambda: WorkdayProvider(config.WORKDAY_TENANT_BASE_URLS),
    "teamtailor": lambda: TeamtailorProvider(config.TEAMTAILOR_SUBDOMAINS),
    "jobvite": lambda: JobviteProvider([]),
    "pinpoint": lambda: PinpointProvider([]),
    "jazzhr": lambda: JazzHRProvider([]),
    "icims": lambda: ICIMSProvider([]),
    "oracle": lambda: OracleRecruitingProvider([]),
}

# Provider CLASSES (not factories) for every known provider -- used to expose
# capabilities programmatically regardless of whether the provider is
# currently enabled/configured with any tenants.
_PROVIDER_CLASSES: dict[str, type[JobProvider]] = {
    "greenhouse": GreenhouseProvider,
    "lever": LeverProvider,
    "ashby": AshbyProvider,
    "workable": WorkableProvider,
    "smartrecruiters": SmartRecruitersProvider,
    "bamboohr": BambooHRProvider,
    "recruitee": RecruiteeProvider,
    "breezy": BreezyProvider,
    "comeet": CometProvider,
    "workday": WorkdayProvider,
    "teamtailor": TeamtailorProvider,
    "jobvite": JobviteProvider,
    "pinpoint": PinpointProvider,
    "jazzhr": JazzHRProvider,
    "icims": ICIMSProvider,
    "oracle": OracleRecruitingProvider,
}


def get_enabled_providers() -> list[JobProvider]:
    providers = []
    for name in config.ENABLED_PROVIDERS:
        factory = _PROVIDER_FACTORIES.get(name.strip().lower())
        if factory:
            providers.append(factory())
    return providers


def build_provider_for_tenant(provider_name: str, tenant_identifier: str) -> JobProvider | None:
    """Builds a single-tenant provider instance for a company_registry row,
    for the adaptive per-tenant discovery path."""
    factory = _PROVIDER_FACTORIES.get(provider_name.strip().lower())
    cls = _PROVIDER_CLASSES.get(provider_name.strip().lower())
    if cls is None:
        return None
    if provider_name.lower() == "workday":
        return WorkdayProvider([tenant_identifier])
    if provider_name.lower() == "comeet":
        return CometProvider([tenant_identifier])
    try:
        return cls([tenant_identifier])
    except Exception:
        return None


def all_provider_names() -> list[str]:
    return sorted(_PROVIDER_CLASSES.keys())


def get_capabilities(provider_name: str) -> ProviderCapabilities | None:
    cls = _PROVIDER_CLASSES.get(provider_name.strip().lower())
    return cls.get_capabilities() if cls else None


def all_capabilities() -> list[ProviderCapabilities]:
    return [cls.get_capabilities() for cls in _PROVIDER_CLASSES.values()]
