"""Given a public careers/application URL, detect the likely ATS provider and
(when deterministically extractable) its tenant identifier. Pattern-matching
only -- never fetches the URL, never guesses a confident answer from a weak
signal. Confidence is always reported alongside the guess so callers can
decide whether to trust it automatically or require human review."""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class DetectionResult:
    provider: Optional[str]
    confidence: float  # 0.0 (no match) to 1.0 (certain, tenant extracted)
    tenant_identifier: Optional[str]
    evidence: str


def _no_match(url: str) -> DetectionResult:
    return DetectionResult(provider=None, confidence=0.0, tenant_identifier=None,
                            evidence=f"no known ATS URL pattern matched '{url}'")


def _first_path_segment(path: str) -> Optional[str]:
    segments = [s for s in path.split("/") if s]
    return segments[0] if segments else None


# Each rule: (provider_name, host_regex, tenant_extractor(host, path) -> Optional[str], evidence_template)
def _rule_greenhouse(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)greenhouse\.io$", host) or re.search(r"(^|\.)job-boards\.greenhouse\.io$", host):
        tenant = _first_path_segment(path)
        conf = 0.95 if tenant else 0.6
        return DetectionResult("greenhouse", conf, tenant, f"host '{host}' matches Greenhouse job boards")
    return None


def _rule_lever(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)jobs\.lever\.co$", host):
        tenant = _first_path_segment(path)
        conf = 0.95 if tenant else 0.6
        return DetectionResult("lever", conf, tenant, f"host '{host}' matches Lever's public postings domain")
    return None


def _rule_ashby(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)jobs\.ashbyhq\.com$", host):
        tenant = _first_path_segment(path)
        conf = 0.95 if tenant else 0.6
        return DetectionResult("ashby", conf, tenant, f"host '{host}' matches Ashby's public job board domain")
    return None


def _rule_workable(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)apply\.workable\.com$", host):
        tenant = _first_path_segment(path)
        return DetectionResult("workable", 0.9 if tenant else 0.6, tenant, f"host '{host}' matches Workable's apply domain")
    m = re.match(r"^([a-z0-9-]+)\.workable\.com$", host)
    if m:
        return DetectionResult("workable", 0.85, m.group(1), f"host '{host}' matches a Workable company subdomain")
    return None


def _rule_smartrecruiters(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)(careers|jobs)\.smartrecruiters\.com$", host):
        tenant = _first_path_segment(path)
        return DetectionResult("smartrecruiters", 0.9 if tenant else 0.6, tenant,
                                f"host '{host}' matches SmartRecruiters' public careers domain")
    return None


def _rule_bamboohr(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.bamboohr\.com$", host)
    if m and ("careers" in path or "jobs" in path):
        return DetectionResult("bamboohr", 0.9, m.group(1), f"host '{host}' matches a BambooHR company subdomain")
    return None


def _rule_jobvite(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)jobs\.jobvite\.com$", host):
        tenant = _first_path_segment(path)
        return DetectionResult("jobvite", 0.85 if tenant else 0.5, tenant, f"host '{host}' matches Jobvite's public jobs domain")
    m = re.match(r"^([a-z0-9-]+)\.jobvite\.com$", host)
    if m:
        return DetectionResult("jobvite", 0.75, m.group(1), f"host '{host}' matches a Jobvite company subdomain")
    return None


def _rule_recruitee(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.recruitee\.com$", host)
    if m:
        return DetectionResult("recruitee", 0.9, m.group(1), f"host '{host}' matches a Recruitee company subdomain")
    return None


def _rule_teamtailor(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.teamtailor\.com$", host)
    if m:
        return DetectionResult("teamtailor", 0.85, m.group(1), f"host '{host}' matches a Teamtailor company subdomain")
    return None


def _rule_pinpoint(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.pinpointhq\.com$", host)
    if m:
        return DetectionResult("pinpoint", 0.85, m.group(1), f"host '{host}' matches a Pinpoint company subdomain")
    return None


def _rule_breezy(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.breezy\.hr$", host)
    if m:
        return DetectionResult("breezy", 0.9, m.group(1), f"host '{host}' matches a Breezy HR company subdomain")
    return None


def _rule_jazzhr(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.applytojob\.com$", host)
    if m:
        return DetectionResult("jazzhr", 0.85, m.group(1), f"host '{host}' matches a JazzHR applicant-facing subdomain")
    return None


def _rule_comeet(host: str, path: str) -> Optional[DetectionResult]:
    if re.search(r"(^|\.)comeet\.(com|co)$", host) and "/jobs/" in path:
        segments = [s for s in path.split("/") if s]
        tenant = segments[1] if len(segments) > 1 and segments[0] == "jobs" else None
        return DetectionResult("comeet", 0.7 if tenant else 0.4, tenant, f"host '{host}' matches Comeet's public careers path")
    return None


def _rule_workday(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        tenant, wd_host = m.group(1), m.group(2)
        site = _first_path_segment(path) or "en-US"
        tenant_identifier = f"{tenant}/{wd_host}/{site}"
        return DetectionResult("workday", 0.85, tenant_identifier,
                                f"host '{host}' matches Workday's myworkdayjobs.com pattern (tenant={tenant}, site={site})")
    return None


def _rule_icims(host: str, path: str) -> Optional[DetectionResult]:
    m = re.match(r"^([a-z0-9-]+)\.icims\.com$", host)
    if m:
        return DetectionResult("icims", 0.6, m.group(1),
                                f"host '{host}' matches an iCIMS company subdomain (LIMITED support -- no verified discovery API)")
    return None


def _rule_oracle(host: str, path: str) -> Optional[DetectionResult]:
    if "oraclecloud.com" in host and ("hcmui" in path.lower() or "candidateexperience" in path.lower()):
        return DetectionResult("oracle", 0.5, None,
                                f"host '{host}' matches Oracle Cloud Recruiting's URL pattern (UNSUPPORTED -- tenant params not reliably extractable)")
    return None


_RULES = [
    _rule_greenhouse, _rule_lever, _rule_ashby, _rule_workable, _rule_smartrecruiters,
    _rule_bamboohr, _rule_jobvite, _rule_recruitee, _rule_teamtailor, _rule_pinpoint,
    _rule_breezy, _rule_jazzhr, _rule_comeet, _rule_workday, _rule_icims, _rule_oracle,
]


def detect_provider(url: str) -> DetectionResult:
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return _no_match(url)
    if not parsed.netloc:
        return _no_match(url)

    host = parsed.netloc.lower()
    path = parsed.path or ""

    for rule in _RULES:
        result = rule(host, path)
        if result is not None:
            return result
    return _no_match(url)
