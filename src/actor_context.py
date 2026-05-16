"""Dashboard actor context for territory-aware CLI execution."""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import territories
from .config import load_yaml_config


ACTOR_ENV_KEYS = (
    "APP_ACTOR_USERNAME",
    "APP_ACTOR_ROLE",
    "APP_ACTOR_ALLOWED_STATES",
    "APP_ACTOR_MARKET",
    "APP_ACTOR_MARKET_STATE",
)

logger = logging.getLogger(__name__)
_NO_ACTOR_NOTICE_EMITTED = False


@dataclass(frozen=True)
class ActorContext:
    username: str
    role: str
    allowed_states: tuple[str, ...]
    market: str | None = None
    market_state: str | None = None


class ActorAccessError(RuntimeError):
    """Raised when a dashboard actor attempts to cross territory boundaries."""


def actor_from_env() -> ActorContext | None:
    if not any(str(os.environ.get(key) or "").strip() for key in ACTOR_ENV_KEYS):
        return None

    username = str(os.environ.get("APP_ACTOR_USERNAME") or "").strip().upper()
    role = str(os.environ.get("APP_ACTOR_ROLE") or "user").strip().lower()
    if role not in {"admin", "user"}:
        role = "user"

    raw_states = str(os.environ.get("APP_ACTOR_ALLOWED_STATES") or "").strip()
    if raw_states == "*":
        allowed_states = ("*",)
    else:
        allowed_states = tuple(territories.normalize_state_list(raw_states))

    return ActorContext(
        username=username,
        role=role,
        allowed_states=allowed_states,
        market=str(os.environ.get("APP_ACTOR_MARKET") or "").strip() or None,
        market_state=territories.normalize_state(os.environ.get("APP_ACTOR_MARKET_STATE")),
    )


def actor_is_present() -> bool:
    return actor_from_env() is not None


def actor_is_admin() -> bool:
    actor = actor_from_env()
    if actor is None:
        return True
    return actor.role == "admin" or "*" in actor.allowed_states


def actor_allowed_states() -> tuple[str, ...]:
    actor = actor_from_env()
    if actor is None:
        return ()
    if actor_is_admin():
        return ("*",)
    return tuple(state for state in actor.allowed_states if state != "*")


def actor_display_fields() -> dict[str, Any]:
    actor = actor_from_env()
    if actor is None:
        return {
            "actor_scope_active": False,
            "requested_by_user": None,
            "actor_role": None,
            "allowed_states": None,
            "actor_market": None,
            "actor_market_state": None,
        }
    return {
        "actor_scope_active": True,
        "requested_by_user": actor.username,
        "actor_role": actor.role,
        "allowed_states": "*" if actor_is_admin() else ",".join(actor_allowed_states()),
        "actor_market": actor.market,
        "actor_market_state": actor.market_state,
    }


def actor_summary_fields(market_filter: str | None = None) -> dict[str, Any]:
    actor = actor_from_env()
    if actor is None:
        return {
            "actor_username": None,
            "actor_role": "admin/local",
            "actor_allowed_states": "ALL",
            "actor_market_filter": market_filter,
            "actor_scope_active": False,
            "actor_scope_applied": False,
        }

    is_admin = actor_is_admin()
    return {
        "actor_username": actor.username,
        "actor_role": actor.role,
        "actor_allowed_states": "ALL" if is_admin else ",".join(actor_allowed_states()),
        "actor_market_filter": market_filter or actor.market,
        "actor_scope_active": True,
        "actor_scope_applied": not is_admin,
    }


def log_actor_scope(log: logging.Logger | None = None) -> None:
    actor = actor_from_env()
    if actor is None:
        _emit_no_actor_notice(log)
        return
    target_logger = log or logger
    target_logger.info(
        "actor_scope_active",
        extra={"event": "actor_scope_active", **actor_display_fields()},
    )


def _emit_no_actor_notice(log: logging.Logger | None = None) -> None:
    global _NO_ACTOR_NOTICE_EMITTED
    if _NO_ACTOR_NOTICE_EMITTED:
        return
    message = "No dashboard actor environment is present; no actor territory scope is active."
    if log is not None:
        log.info(
            "actor_scope_inactive",
            extra={"event": "actor_scope_inactive", "actor_scope_active": False},
        )
    else:
        print(message, file=sys.stderr)
    _NO_ACTOR_NOTICE_EMITTED = True


def configured_market_state(market_key: str | None) -> str | None:
    if not market_key:
        return None
    return territories.get_market_state(market_key, _markets_config())


def owner_username_for_state(state_code: Any) -> str | None:
    actor = actor_from_env()
    state = territories.normalize_state(state_code)
    if not state:
        return actor.username if actor else None
    if actor is not None and not actor_is_admin():
        return actor.username
    try:
        return territories.state_owner_username(state, load_yaml_config("users.yaml"))
    except (FileNotFoundError, ValueError):
        return actor.username if actor else None


def validate_actor_market_access(
    market_key: str | None,
    *,
    allow_global_scope: bool = False,
) -> str | None:
    actor = actor_from_env()
    if actor is None:
        _emit_no_actor_notice()
        return configured_market_state(market_key)
    if actor_is_admin():
        return configured_market_state(market_key) or actor.market_state

    market = str(market_key or "").strip()
    if not market:
        if allow_global_scope:
            return None
        raise ActorAccessError(territories.territory_error_message())
    if actor.market and market != actor.market:
        raise ActorAccessError(territories.territory_error_message())

    state = configured_market_state(market)
    if state not in actor_allowed_states():
        raise ActorAccessError(territories.territory_error_message())
    return state


def actor_can_access_prospect(
    prospect_or_connection: Any,
    prospect_id: int | None = None,
) -> bool:
    actor = actor_from_env()
    if actor is None or actor_is_admin():
        return True
    prospect = _resolve_prospect_record(prospect_or_connection, prospect_id)
    if prospect is None:
        return False
    state = prospect_state(prospect)
    return bool(state and state in actor_allowed_states())


def validate_actor_prospect_access(
    prospect_or_connection: Any,
    prospect_id: int | None = None,
) -> None:
    actor = actor_from_env()
    if actor is None or actor_is_admin():
        return
    prospect = _resolve_prospect_record(prospect_or_connection, prospect_id)
    if prospect is None:
        return
    if not actor_can_access_prospect(prospect):
        raise ActorAccessError(territories.territory_error_message())


def prospect_state(prospect: dict[str, Any]) -> str | None:
    for key in ("market_state", "canonical_state"):
        state = territories.normalize_state(prospect.get(key))
        if state:
            return state
    state = configured_market_state(str(prospect.get("market") or "").strip())
    if state:
        return state
    for key in ("state", "state_guess"):
        state = territories.normalize_state(prospect.get(key))
        if state:
            return state
    return None


def actor_sql_scope_clause(table_alias: str | None = "prospects") -> tuple[str, list[Any]]:
    actor = actor_from_env()
    if actor is None:
        _emit_no_actor_notice()
        return "1 = 1", []
    if actor_is_admin():
        return "1 = 1", []

    allowed_states = [state for state in actor_allowed_states() if state != "*"]
    if not allowed_states:
        return "1 = 0", []

    state_col = _qualified_column(table_alias, "market_state")
    market_col = _qualified_column(table_alias, "market")
    raw_state_col = _qualified_column(table_alias, "state")
    state_guess_col = _qualified_column(table_alias, "state_guess")

    state_placeholders = ", ".join("?" for _ in allowed_states)
    clauses = [f"UPPER(COALESCE({state_col}, '')) IN ({state_placeholders})"]
    params: list[Any] = list(allowed_states)

    accessible_market_keys = _accessible_market_keys(allowed_states)
    if accessible_market_keys:
        market_placeholders = ", ".join("?" for _ in accessible_market_keys)
        clauses.append(
            "("
            f"TRIM(COALESCE({state_col}, '')) = '' "
            f"AND {market_col} IN ({market_placeholders})"
            ")"
        )
        params.extend(accessible_market_keys)

    configured_keys = list(_markets_mapping(_markets_config()).keys())
    if configured_keys:
        configured_placeholders = ", ".join("?" for _ in configured_keys)
        unconfigured_market_sql = (
            f"({market_col} IS NULL OR TRIM({market_col}) = '' "
            f"OR {market_col} NOT IN ({configured_placeholders}))"
        )
    else:
        unconfigured_market_sql = "1 = 1"

    clauses.append(
        "("
        f"TRIM(COALESCE({state_col}, '')) = '' "
        f"AND {unconfigured_market_sql} "
        f"AND UPPER(COALESCE({raw_state_col}, '')) IN ({state_placeholders})"
        ")"
    )
    if configured_keys:
        params.extend(configured_keys)
    params.extend(allowed_states)

    clauses.append(
        "("
        f"TRIM(COALESCE({state_col}, '')) = '' "
        f"AND {unconfigured_market_sql} "
        f"AND UPPER(COALESCE({state_guess_col}, '')) IN ({state_placeholders})"
        ")"
    )
    if configured_keys:
        params.extend(configured_keys)
    params.extend(allowed_states)

    return f"({' OR '.join(clauses)})", params


def actor_scope_clause_for_prospects(alias: str | None = "prospects") -> tuple[str, list[Any]]:
    return actor_sql_scope_clause(alias)


def append_actor_scope(
    clauses: list[str],
    params: list[Any],
    table_alias: str | None = "prospects",
) -> None:
    clause, clause_params = actor_sql_scope_clause(table_alias)
    if clause != "1 = 1":
        clauses.append(clause)
        params.extend(clause_params)


def _markets_config() -> dict[str, Any]:
    try:
        return load_yaml_config("markets.yaml")
    except (FileNotFoundError, ValueError):
        return {"markets": {}}


def _markets_mapping(markets_config: dict[str, Any]) -> dict[str, Any]:
    markets = markets_config.get("markets")
    return markets if isinstance(markets, dict) else markets_config


def _accessible_market_keys(allowed_states: list[str]) -> list[str]:
    markets_config = _markets_config()
    markets = _markets_mapping(markets_config)
    return [
        str(key)
        for key in markets
        if territories.get_market_state(key, markets_config) in allowed_states
    ]


def _resolve_prospect_record(
    prospect_or_connection: Any,
    prospect_id: int | None = None,
) -> dict[str, Any] | None:
    if prospect_id is None:
        return _record_to_dict(prospect_or_connection)

    row = prospect_or_connection.execute(
        """
        SELECT id, market, market_state, state, state_guess
        FROM prospects
        WHERE id = ?
        LIMIT 1
        """,
        (prospect_id,),
    ).fetchone()
    return _record_to_dict(row)


def _record_to_dict(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    if isinstance(record, dict):
        return record
    if isinstance(record, Mapping):
        return dict(record)
    keys = getattr(record, "keys", None)
    if callable(keys):
        return {key: record[key] for key in keys()}
    try:
        return dict(record)
    except (TypeError, ValueError):
        return None


def _qualified_column(table_alias: str | None, column_name: str) -> str:
    if not table_alias:
        return column_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_alias):
        raise ValueError("table_alias must be a simple SQL identifier.")
    return f"{table_alias}.{column_name}"
