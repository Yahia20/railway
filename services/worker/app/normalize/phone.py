"""Phone normalisation to E.164.

One phone format is what makes identity resolution possible at all. Every match
in the RESOLVE step is `customers.primary_phone_e164 = ?`, so a number stored as
`0500000000` in one table and `+966500000000` in another is two customers.

The default region matters and must be configured, not guessed: `0500000000` is
a valid mobile in Saudi Arabia (+966) and the same digits mean nothing in Egypt.
The sample call is a Saudi number while the sample Bitrix deal is an Egyptian
portal, so this system genuinely handles both and the region cannot be hardcoded.
"""
from __future__ import annotations

import re

# National trunk prefix -> country code, for the countries actually in scope.
KNOWN_REGIONS = {
    "SA": {"cc": "966", "mobile_len": 9, "mobile_prefix": "5"},
    "EG": {"cc": "20", "mobile_len": 10, "mobile_prefix": "1"},
    "AE": {"cc": "971", "mobile_len": 9, "mobile_prefix": "5"},
    "KW": {"cc": "965", "mobile_len": 8, "mobile_prefix": "5"},
    "QA": {"cc": "974", "mobile_len": 8, "mobile_prefix": "3"},
}

_NON_DIGIT = re.compile(r"[^\d+]")


class PhoneError(ValueError):
    pass


def normalize_phone(raw: str | None, default_region: str = "SA") -> str | None:
    """Return an E.164 string, or None if the input cannot be a phone number.

    Raises PhoneError only for input that looks like a number but cannot be
    resolved without guessing — silently picking a country would merge two
    different people onto one customer record.
    """
    if not raw:
        return None

    cleaned = _NON_DIGIT.sub("", str(raw).strip())
    if not cleaned:
        return None

    # 00 international prefix
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if not 7 <= len(digits) <= 15 or digits.startswith("0"):
            raise PhoneError(f"not a valid E.164 number: {raw!r}")
        return "+" + digits

    region = KNOWN_REGIONS.get(default_region.upper())
    if region is None:
        raise PhoneError(f"unknown default_region {default_region!r}")

    # Already carries its own country code, e.g. 966500000000
    if cleaned.startswith(region["cc"]):
        rest = cleaned[len(region["cc"]):]
        if len(rest) == region["mobile_len"]:
            return "+" + cleaned

    # National format with a leading trunk 0, e.g. 0500000000
    if cleaned.startswith("0"):
        national = cleaned[1:]
        if len(national) == region["mobile_len"]:
            return "+" + region["cc"] + national

    # Bare national number, e.g. 500000000
    if len(cleaned) == region["mobile_len"] and cleaned.startswith(region["mobile_prefix"]):
        return "+" + region["cc"] + cleaned

    raise PhoneError(
        f"cannot normalise {raw!r} for region {default_region}: "
        f"got {len(cleaned)} digits, expected {region['mobile_len']} national"
    )


def try_normalize(raw: str | None, default_region: str = "SA") -> tuple[str | None, str | None]:
    """Non-raising variant for batch ingest: returns (e164, error)."""
    try:
        return normalize_phone(raw, default_region), None
    except PhoneError as exc:
        return None, str(exc)
