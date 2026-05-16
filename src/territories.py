"""Canonical state and territory helpers for dashboard data partitioning."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


STATE_NAME_TO_CODE: dict[str, str] = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}

STATE_CODE_TO_NAME: dict[str, str] = {code: name for name, code in STATE_NAME_TO_CODE.items()}
STATE_CODES: set[str] = set(STATE_CODE_TO_NAME)


def _alias_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").strip().upper())


STATE_ALIASES: dict[str, str] = {}
for _state_name, _state_code in STATE_NAME_TO_CODE.items():
    STATE_ALIASES[_alias_key(_state_name)] = _state_code
    STATE_ALIASES[_state_code] = _state_code
STATE_ALIASES.update(
    {
        "D C": "DC",
        "DC": "DC",
        "WASHINGTONDC": "DC",
        "WASHINGTOND C": "DC",
    }
)
STATE_ALIASES = {_alias_key(key): value for key, value in STATE_ALIASES.items()}


def normalize_state(value: Any) -> str | None:
    """Return a canonical two-letter state code, or None when not recognized."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    alias = STATE_ALIASES.get(_alias_key(text))
    if alias in STATE_CODES:
        return alias
    return None


def normalize_state_list(values: Any) -> list[str]:
    """Normalize an iterable or comma-delimited string of states, preserving order."""

    if values is None:
        return []
    if isinstance(values, str):
        text = values.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            raw_values = (
                parsed if isinstance(parsed, list) else [part.strip() for part in text.split(",")]
            )
        else:
            raw_values = [part.strip() for part in text.split(",")]
    elif isinstance(values, Iterable):
        raw_values = values
    else:
        raw_values = [values]

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if str(value or "").strip() == "*":
            code = "*"
        else:
            code = normalize_state(value)
        if code and code not in seen:
            normalized.append(code)
            seen.add(code)
    return normalized


def validate_state_code(value: Any) -> bool:
    """Return True when value can be normalized to a known state code."""

    return normalize_state(value) is not None


def _markets_mapping(markets_config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(markets_config, Mapping):
        return {}
    markets = markets_config.get("markets")
    if isinstance(markets, Mapping):
        return markets
    return markets_config


def infer_state_from_market_key_or_label(
    market_key: Any,
    market_data: Mapping[str, Any] | None,
) -> str | None:
    """Infer a market state only from obvious key suffixes or label state markers."""

    key_text = str(market_key or "").strip()
    raw = market_data if isinstance(market_data, Mapping) else {}
    label_text = str(raw.get("label") or "").strip()

    key_suffix = re.search(r"(?:^|[_\-\s])([A-Za-z]{2})$", key_text)
    if key_suffix:
        state = normalize_state(key_suffix.group(1))
        if state:
            return state

    label_candidates: list[str] = []
    for pattern in (
        r",\s*([A-Za-z]{2})(?:\b|$)",
        r"\(([A-Za-z]{2})\)\s*$",
        r"\b([A-Za-z]{2})\s*$",
    ):
        match = re.search(pattern, label_text)
        if match:
            state = normalize_state(match.group(1))
            if state:
                label_candidates.append(state)

    normalized_label = f" {_alias_key(label_text)} "
    for state_name, state_code in STATE_NAME_TO_CODE.items():
        compact_name = _alias_key(state_name)
        if compact_name and compact_name in normalized_label:
            label_candidates.append(state_code)

    unique = list(dict.fromkeys(label_candidates))
    return unique[0] if len(unique) == 1 else None


def get_market_state(
    market_key: Any,
    markets_config: Mapping[str, Any] | None,
) -> str | None:
    """Return the canonical state for a configured market key."""

    if market_key is None:
        return None
    key = str(market_key).strip()
    if not key:
        return None
    raw_market = _markets_mapping(markets_config).get(key)
    market_data = raw_market if isinstance(raw_market, Mapping) else {}
    configured_state = normalize_state(market_data.get("state"))
    if configured_state:
        return configured_state
    return infer_state_from_market_key_or_label(key, market_data)


def market_belongs_to_state(
    market_key: Any,
    state_code: Any,
    markets_config: Mapping[str, Any] | None,
) -> bool:
    """Return whether a market resolves to the provided state."""

    state = normalize_state(state_code)
    return bool(state and get_market_state(market_key, markets_config) == state)


def _users_mapping(users_config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(users_config, Mapping):
        return {}
    users = users_config.get("users")
    if isinstance(users, Mapping):
        return users
    return users_config


def _iter_users(users_config: Mapping[str, Any] | None) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for username, raw_user in _users_mapping(users_config).items():
        if isinstance(raw_user, Mapping):
            user = raw_user
        else:
            user = {}
        yield str(user.get("username") or username), user


def _lookup_user(
    user_or_username: Any,
    users_config: Mapping[str, Any] | None = None,
) -> tuple[str | None, Mapping[str, Any]]:
    if isinstance(user_or_username, Mapping):
        username = user_or_username.get("username") or user_or_username.get("user_id")
        return (str(username) if username else None), user_or_username

    username = str(user_or_username or "").strip()
    if not username:
        return None, {}
    username_key = username.lower()
    for configured_username, user in _iter_users(users_config):
        if configured_username.lower() == username_key:
            return configured_username, user
    return username, {}


def _user_role(user: Mapping[str, Any]) -> str:
    return str(user.get("role") or "").strip().lower()


def _user_state_values(user: Mapping[str, Any]) -> Any:
    for key in ("states", "state_codes", "allowed_states", "territories"):
        if key in user:
            return user.get(key)
    return []


def _user_is_admin(user: Mapping[str, Any]) -> bool:
    if _user_role(user) == "admin":
        return True
    return "*" in normalize_state_list(_user_state_values(user))


def user_state_codes(
    user_or_username: Any,
    users_config: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return normalized state codes from a session/user dict or users config."""

    _, user = _lookup_user(user_or_username, users_config)
    return [state for state in normalize_state_list(_user_state_values(user)) if state != "*"]


def state_owner_username(
    state_code: Any,
    users_config: Mapping[str, Any] | None,
) -> str | None:
    """Return the non-admin owner username for a state, if configured."""

    state = normalize_state(state_code)
    if not state:
        return None
    for username, user in _iter_users(users_config):
        if _user_is_admin(user):
            continue
        if state in normalize_state_list(_user_state_values(user)):
            return username
    return None


def validate_exclusive_territories(users_config: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return duplicate/invalid non-admin territory assignments; empty means valid."""

    owners_by_state: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for username, user in _iter_users(users_config):
        if _user_is_admin(user):
            continue
        raw_states = _user_state_values(user)
        if isinstance(raw_states, str):
            raw_state_values: Iterable[Any] = [part.strip() for part in raw_states.split(",")]
        elif isinstance(raw_states, Iterable):
            raw_state_values = raw_states
        else:
            raw_state_values = [raw_states]
        for raw_state in raw_state_values:
            if str(raw_state or "").strip() == "*":
                continue
            state = normalize_state(raw_state)
            if not state:
                conflicts.append(
                    {
                        "type": "invalid_state",
                        "state": raw_state,
                        "username": username,
                    }
                )
                continue
            existing_owner = owners_by_state.get(state)
            if existing_owner and existing_owner != username:
                conflicts.append(
                    {
                        "type": "duplicate_state",
                        "state": state,
                        "usernames": [existing_owner, username],
                    }
                )
            else:
                owners_by_state[state] = username
    return conflicts


def user_can_access_state(
    user_or_username: Any,
    state_code: Any,
    users_config: Mapping[str, Any] | None,
) -> bool:
    """Return whether a user/session/config row can access a state."""

    state = normalize_state(state_code)
    if not state:
        return False
    _, user = _lookup_user(user_or_username, users_config)
    if _user_is_admin(user):
        return True
    return state in normalize_state_list(_user_state_values(user))


def user_can_access_market(
    user_or_username: Any,
    market_key: Any,
    markets_config: Mapping[str, Any] | None,
    users_config: Mapping[str, Any] | None,
) -> bool:
    """Return whether a user/session/config row can access a market."""

    state = get_market_state(market_key, markets_config)
    return user_can_access_state(user_or_username, state, users_config)


def get_accessible_market_keys(
    user_or_username: Any,
    markets_config: Mapping[str, Any] | None,
    users_config: Mapping[str, Any] | None,
) -> list[str]:
    """Return configured market keys visible to the given user/session/config row."""

    markets = _markets_mapping(markets_config)
    _, user = _lookup_user(user_or_username, users_config)
    if _user_is_admin(user):
        return [str(key) for key in markets.keys()]

    allowed_states = set(normalize_state_list(_user_state_values(user)))
    return [
        str(key)
        for key in markets.keys()
        if (state := get_market_state(key, markets_config)) and state in allowed_states
    ]


def territory_error_message() -> str:
    return "you do not own this market"


def _qualified_column(table_alias: str | None, column_name: str) -> str:
    if not table_alias:
        return column_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_alias):
        raise ValueError("table_alias must be a simple SQL identifier.")
    return f"{table_alias}.{column_name}"


def _accessible_filter_sql(
    user_or_username: Any,
    *,
    table_alias: str | None,
    column_name: str,
    users_config: Mapping[str, Any] | None = None,
) -> tuple[str, list[str]]:
    _, user = _lookup_user(user_or_username, users_config)
    if _user_is_admin(user):
        return "1=1", []

    states = user_state_codes(user_or_username, users_config)
    if not states:
        return "1=0", []

    placeholders = ", ".join("?" for _ in states)
    column = _qualified_column(table_alias, column_name)
    return f"{column} IN ({placeholders})", states


def accessible_market_filter_sql(
    user_or_username: Any,
    table_alias: str | None = "prospects",
    users_config: Mapping[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Return a market-state SQL filter and params for a user; admin is unfiltered."""

    return _accessible_filter_sql(
        user_or_username,
        table_alias=table_alias,
        column_name="market_state",
        users_config=users_config,
    )


def accessible_state_filter_sql(
    user_or_username: Any,
    table_alias: str | None = "prospects",
    users_config: Mapping[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Return a state SQL filter and params for a user; admin is unfiltered."""

    return _accessible_filter_sql(
        user_or_username,
        table_alias=table_alias,
        column_name="market_state",
        users_config=users_config,
    )
