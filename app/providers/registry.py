from app import config
from app.providers.base import JobProvider
from app.providers.greenhouse import GreenhouseProvider
from app.providers.lever import LeverProvider

# Add new providers here -- each one just needs a JobProvider subclass with a
# fetch_jobs(max_jobs) method that isolates its own errors internally.
_PROVIDER_FACTORIES = {
    "greenhouse": lambda: GreenhouseProvider(config.GREENHOUSE_BOARD_TOKENS),
    "lever": lambda: LeverProvider(config.LEVER_COMPANY_SLUGS),
}


def get_enabled_providers() -> list[JobProvider]:
    providers = []
    for name in config.ENABLED_PROVIDERS:
        factory = _PROVIDER_FACTORIES.get(name.strip().lower())
        if factory:
            providers.append(factory())
    return providers
