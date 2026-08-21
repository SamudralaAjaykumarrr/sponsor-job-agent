from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class RawJobPosting:
    """Normalized shape every provider must produce, before dedup/analysis."""

    provider: str
    external_job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    employment_type_raw: str = ""
    published_at: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None


class JobProvider(ABC):
    """Discovery connector for a public, unauthenticated job-board API.
    Implementations MUST isolate per-board/per-company fetch errors internally
    (log + skip) so one bad source never aborts a whole discovery cycle."""

    name: str = "base"

    @abstractmethod
    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        raise NotImplementedError
