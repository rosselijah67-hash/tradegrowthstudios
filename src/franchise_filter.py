"""Config-backed franchise and national-chain exclusion checks."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from .config import load_yaml_config


CONFIG_FILE = "franchise_exclusions.yaml"
LEGAL_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "pllc",
}


def normalize_company_name(value: str) -> str:
    """Normalize company/brand text for conservative phrase matching."""

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower()).strip()
    tokens = [token for token in text.split() if token not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def normalize_domain(value: str) -> str:
    """Normalize a URL or hostname into a lower-case hostname without www."""

    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"//{raw}"
    parsed = urlparse(candidate)
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    host = host.split("@")[-1].split(":", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


@lru_cache(maxsize=1)
def load_franchise_exclusions() -> dict[str, Any]:
    return load_yaml_config(CONFIG_FILE)


def check_franchise_exclusion(prospect_or_fields: dict[str, Any]) -> dict[str, Any]:
    config = load_franchise_exclusions()
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    soft_penalty = int(settings.get("soft_exclude_penalty") or 40)
    hard_action = str(settings.get("default_hard_exclude_action") or "DISQUALIFY").upper()
    soft_action = str(settings.get("default_soft_exclude_action") or "PENALIZE").upper()

    fields = prospect_or_fields or {}
    names = _candidate_names(fields)
    domains = _candidate_domains(fields)
    text = " ".join([*names, *domains, _types_text(fields.get("types_json"))]).strip()
    normalized_text = normalize_company_name(text)

    lists = config.get("lists") if isinstance(config.get("lists"), dict) else {}
    categories = config.get("categories") if isinstance(config.get("categories"), dict) else {}

    match = _match_domains(domains, _list_values(lists, "hard_exclude_domains"))
    if match:
        return _result(
            hard=True,
            action=hard_action,
            reason="Matched hard-exclude franchise/national-chain domain.",
            matched_domain=match,
            confidence=1.0,
            penalty=0,
        )

    match = _match_domains(domains, _list_values(lists, "soft_exclude_domains"))
    if match:
        return _result(
            soft=True,
            action=soft_action,
            reason="Matched soft-exclude franchise/corporate domain.",
            matched_domain=match,
            confidence=0.82,
            penalty=soft_penalty,
        )

    parent_match = _match_name_list(normalized_text, _list_values(lists, "parent_platforms"))
    if parent_match:
        return _result(
            hard=True,
            action=hard_action,
            reason="Matched parent franchise/corporate platform.",
            matched_name=parent_match,
            confidence=0.9,
            penalty=0,
        )

    match = _match_name_list(normalized_text, _list_values(lists, "hard_exclude_names"))
    if match:
        return _result(
            hard=True,
            action=hard_action,
            reason="Matched hard-exclude franchise/national-chain name.",
            matched_name=match,
            confidence=0.95,
            penalty=0,
        )

    category_match = _match_categories(normalized_text, categories, "hard_exclude")
    if category_match:
        category, name = category_match
        return _result(
            hard=True,
            action=hard_action,
            reason="Matched category hard-exclude franchise/national-chain name.",
            matched_name=name,
            matched_category=category,
            confidence=0.95,
            penalty=0,
        )

    regex_match = _match_regex(text, _list_values(lists, "regex_hard_exclude"))
    if regex_match:
        return _result(
            hard=True,
            action=hard_action,
            reason="Matched hard-exclude franchise/national-chain regex.",
            matched_regex=regex_match,
            confidence=0.9,
            penalty=0,
        )

    match = _match_name_list(normalized_text, _list_values(lists, "soft_exclude_names"))
    if match:
        return _result(
            soft=True,
            action=soft_action,
            reason="Matched soft-exclude corporate/franchise-like name.",
            matched_name=match,
            confidence=0.72,
            penalty=soft_penalty,
        )

    category_match = _match_categories(normalized_text, categories, "soft_exclude")
    if category_match:
        category, name = category_match
        return _result(
            soft=True,
            action=soft_action,
            reason="Matched category soft-exclude corporate/franchise-like name.",
            matched_name=name,
            matched_category=category,
            confidence=0.72,
            penalty=soft_penalty,
        )

    regex_match = _match_regex(text, _list_values(lists, "regex_soft_exclude"))
    if regex_match:
        return _result(
            soft=True,
            action=soft_action,
            reason="Matched soft-exclude corporate/franchise-like regex.",
            matched_regex=regex_match,
            confidence=0.68,
            penalty=soft_penalty,
        )

    return _result(action="ALLOW", reason="No franchise/national-chain exclusion matched.")


def _result(
    *,
    action: str,
    reason: str,
    hard: bool = False,
    soft: bool = False,
    matched_name: str = "",
    matched_domain: str = "",
    matched_regex: str = "",
    matched_category: str = "",
    confidence: float = 0.0,
    penalty: int = 0,
) -> dict[str, Any]:
    return {
        "is_excluded": bool(hard or soft),
        "is_hard_exclude": bool(hard),
        "is_soft_exclude": bool(soft),
        "matched_name": matched_name,
        "matched_domain": matched_domain,
        "matched_regex": matched_regex,
        "matched_category": matched_category,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "recommended_action": action,
        "reason": reason,
        "penalty": int(penalty),
    }


def _candidate_names(fields: dict[str, Any]) -> list[str]:
    values = [
        fields.get("business_name"),
        fields.get("place_name"),
        fields.get("display_name"),
        fields.get("name"),
    ]
    return _unique_strings(values)


def _candidate_domains(fields: dict[str, Any]) -> list[str]:
    values = [
        fields.get("domain"),
        fields.get("website_url"),
        fields.get("websiteUri"),
        fields.get("website"),
    ]
    domains = [normalize_domain(str(value)) for value in values if value]
    return _unique_strings(domains)


def _types_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value)
    if isinstance(value, dict):
        return " ".join(str(item) for item in value.values())
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)
    return _types_text(parsed)


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
    return output


def _list_values(owner: dict[str, Any], key: str) -> list[str]:
    raw = owner.get(key) if isinstance(owner, dict) else []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _match_domains(domains: list[str], candidates: list[str]) -> str:
    normalized = [normalize_domain(candidate) for candidate in candidates]
    for domain in domains:
        for candidate in normalized:
            if candidate and (domain == candidate or domain.endswith(f".{candidate}")):
                return candidate
    return ""


def _match_name_list(normalized_text: str, candidates: list[str]) -> str:
    padded_text = f" {normalized_text} "
    for candidate in candidates:
        normalized = normalize_company_name(candidate)
        if not normalized:
            continue
        tokens = normalized.split()
        if normalized_text == normalized:
            return candidate
        if len(tokens) >= 2 and f" {normalized} " in padded_text:
            return candidate
    return ""


def _match_categories(
    normalized_text: str,
    categories: dict[str, Any],
    list_name: str,
) -> tuple[str, str] | None:
    for category, values in categories.items():
        if not isinstance(values, dict):
            continue
        match = _match_name_list(normalized_text, _list_values(values, list_name))
        if match:
            return str(category), match
    return None


def _match_regex(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return ""
