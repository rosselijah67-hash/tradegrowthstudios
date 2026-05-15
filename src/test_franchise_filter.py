"""Local assertions for the franchise exclusion matcher.

Run manually with:
    python -m src.test_franchise_filter
"""

from __future__ import annotations

from .franchise_filter import check_franchise_exclusion


HARD_EXCLUDE_EXAMPLES = [
    "Mr. Rooter Plumbing of McKinney",
    "Mister Sparky Electric",
    "One Hour Heating & Air Conditioning",
    "Precision Garage Door Service",
    "Mighty Dog Roofing",
    "SERVPRO of Frisco",
    "Paul Davis Restoration",
    "Mosquito Joe",
    "The Grounds Guys",
    "CertaPro Painters",
    "Aire Serv",
]

ALLOW_EXAMPLES = [
    "Rooter Brothers Plumbing",
    "One Stop Roofing",
    "Mr. Smith Roofing",
    "Precision Roofing Solutions",
    "Elite Roofing LLC",
    "Performance Roofing",
]


def test_hard_exclusions() -> None:
    for name in HARD_EXCLUDE_EXAMPLES:
        result = check_franchise_exclusion({"business_name": name})
        assert result["is_hard_exclude"], f"Expected hard exclude for {name}: {result}"
        assert result["recommended_action"] == "DISQUALIFY", result


def test_allowed_examples() -> None:
    for name in ALLOW_EXAMPLES:
        result = check_franchise_exclusion({"business_name": name})
        assert not result["is_hard_exclude"], f"Unexpected hard exclude for {name}: {result}"


def test_domain_exclusion() -> None:
    result = check_franchise_exclusion({"website_url": "https://cleveland.servpro.com/"})
    assert result["is_hard_exclude"], result
    assert result["matched_domain"] == "servpro.com", result


if __name__ == "__main__":
    test_hard_exclusions()
    test_allowed_examples()
    test_domain_exclusion()
    print("franchise_filter tests passed")
