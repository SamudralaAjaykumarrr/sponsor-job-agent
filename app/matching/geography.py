import re

# Deliberately conservative: only rejects on an EXPLICIT non-US signal.
# Ambiguous/missing location text is allowed through -- work-arrangement
# classification and human review are the safety net downstream, and the
# discovery cycle should not silently drop legitimate US remote postings that
# simply lack a fully spelled-out location string.
NON_US_COUNTRY_TOKENS = [
    "united kingdom", "canada", "india", "germany", "france", "australia",
    "singapore", "ireland", "netherlands", "spain", "italy", "poland", "brazil",
    "mexico", "japan", "china", "philippines", "pakistan", "bangladesh",
    "ukraine", "romania", "portugal", "sweden", "switzerland", "israel",
    "united arab emirates", "south africa", "argentina", "colombia", "vietnam",
    "indonesia", "malaysia", "thailand", "egypt", "nigeria", "kenya",
]

US_SIGNAL_TOKENS = [
    "united states", "usa", "u.s.", "remote - us", "remote (us)", "remote, us",
    "remote-us", "remote us",
]

US_STATE_ABBRS = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC".split()
)


def is_us_location(location: str) -> bool:
    if not location:
        return True  # ambiguous -- do not reject solely for missing location data

    loc_lower = location.lower()

    if any(tok in loc_lower for tok in NON_US_COUNTRY_TOKENS):
        return False

    if any(tok in loc_lower for tok in US_SIGNAL_TOKENS):
        return True

    if any(t in US_STATE_ABBRS for t in re.findall(r"\b[A-Z]{2}\b", location)):
        return True

    return True  # ambiguous (e.g. bare city name) -- allow through
