"""Backfill canonical market-state and owner fields in the single SQLite DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import db
from .config import get_database_path, load_env, load_yaml_config, project_path
from .territories import (
    get_market_state,
    normalize_state,
    state_owner_username,
    validate_exclusive_territories,
)


def _load_optional_yaml(filename: str) -> dict[str, Any]:
    try:
        return load_yaml_config(filename)
    except FileNotFoundError:
        return {}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Database does not exist: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _select_expr(
    columns: set[str],
    table_alias: str,
    column_name: str,
    alias: str | None = None,
) -> str:
    output_name = alias or column_name
    if column_name in columns:
        return f"{table_alias}.{column_name} AS {output_name}"
    return f"NULL AS {output_name}"


def _markets_mapping(markets_config: dict[str, Any]) -> dict[str, Any]:
    markets = markets_config.get("markets")
    return markets if isinstance(markets, dict) else {}


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _state_owner(
    state_code: str | None,
    users_config: dict[str, Any],
    states_with_no_owner: set[str],
) -> str | None:
    if not state_code:
        return None
    owner = state_owner_username(state_code, users_config)
    if owner is None:
        states_with_no_owner.add(state_code)
    return owner


def _derive_state_from_market(
    market: Any,
    markets_config: dict[str, Any],
    summary: dict[str, Any],
) -> str | None:
    market_text = _clean_text(market)
    if not market_text:
        return None
    if market_text not in _markets_mapping(markets_config):
        summary["records_with_unknown_market"] += 1
    return get_market_state(market_text, markets_config)


def _derive_prospect_state(
    row: sqlite3.Row,
    markets_config: dict[str, Any],
    summary: dict[str, Any],
) -> str | None:
    return (
        _derive_state_from_market(row["market"], markets_config, summary)
        or normalize_state(row["state"])
        or normalize_state(row["state_guess"])
    )


def _apply_update(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    row_id: int,
    changes: dict[str, Any],
    apply: bool,
) -> bool:
    if not changes:
        return False
    if apply:
        assignments = ", ".join(f"{column_name} = ?" for column_name in changes)
        params = [*changes.values(), row_id]
        connection.execute(
            f"UPDATE {table_name} SET {assignments} WHERE id = ?",
            params,
        )
    return True


def _field_changes(row: sqlite3.Row, targets: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for column_name, target_value in targets.items():
        current_value = row[column_name]
        if _clean_text(current_value) != _clean_text(target_value):
            changes[column_name] = target_value
    return changes


def _prospect_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = _table_columns(connection, "prospects")
    return connection.execute(
        f"""
        SELECT
            p.id,
            p.market,
            p.state,
            p.state_guess,
            {_select_expr(columns, "p", "market_state")},
            {_select_expr(columns, "p", "owner_username")}
        FROM prospects p
        ORDER BY p.id
        """
    ).fetchall()


def _dashboard_job_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    columns = _table_columns(connection, "dashboard_jobs")
    return connection.execute(
        f"""
        SELECT
            j.id,
            j.market,
            j.metadata_json,
            {_select_expr(columns, "j", "market_state")},
            {_select_expr(columns, "j", "requested_by_user")}
        FROM dashboard_jobs j
        ORDER BY j.id
        """
    ).fetchall()


def _joined_rows(connection: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
    table_columns = _table_columns(connection, table_name)
    prospect_columns = _table_columns(connection, "prospects")
    return connection.execute(
        f"""
        SELECT
            child.id,
            {_select_expr(table_columns, "child", "market_state")},
            {_select_expr(table_columns, "child", "owner_username")},
            p.market AS prospect_market,
            p.state AS prospect_state,
            p.state_guess AS prospect_state_guess,
            {_select_expr(prospect_columns, "p", "market_state", "prospect_market_state")},
            {_select_expr(prospect_columns, "p", "owner_username", "prospect_owner_username")}
        FROM {table_name} child
        LEFT JOIN prospects p ON p.id = child.prospect_id
        ORDER BY child.id
        """
    ).fetchall()


def _metadata_requested_by(row: sqlite3.Row) -> str | None:
    metadata_text = row["metadata_json"]
    if not metadata_text:
        return None
    try:
        metadata = json.loads(str(metadata_text))
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    for key in ("requested_by_user", "actor_username", "username", "user_id"):
        value = _clean_text(metadata.get(key))
        if value:
            return value
    return None


def _reconcile_prospects(
    connection: sqlite3.Connection,
    *,
    markets_config: dict[str, Any],
    users_config: dict[str, Any],
    summary: dict[str, Any],
    apply: bool,
) -> None:
    for row in _prospect_rows(connection):
        state = _derive_prospect_state(row, markets_config, summary)
        if not state:
            summary["records_with_unknown_state"] += 1
        owner = _state_owner(state, users_config, summary["states_with_no_owner"])
        changes = _field_changes(
            row,
            {
                "market_state": state,
                "owner_username": owner,
            },
        )
        if _apply_update(
            connection,
            table_name="prospects",
            row_id=int(row["id"]),
            changes=changes,
            apply=apply,
        ):
            summary["prospects_updated"] += 1


def _reconcile_dashboard_jobs(
    connection: sqlite3.Connection,
    *,
    markets_config: dict[str, Any],
    summary: dict[str, Any],
    apply: bool,
) -> None:
    for row in _dashboard_job_rows(connection):
        state = normalize_state(row["market_state"]) or _derive_state_from_market(
            row["market"],
            markets_config,
            summary,
        )
        if not state:
            summary["records_with_unknown_state"] += 1
        requested_by = _clean_text(row["requested_by_user"]) or _metadata_requested_by(row)
        changes = _field_changes(
            row,
            {
                "market_state": state,
                "requested_by_user": requested_by,
            },
        )
        if _apply_update(
            connection,
            table_name="dashboard_jobs",
            row_id=int(row["id"]),
            changes=changes,
            apply=apply,
        ):
            summary["dashboard_jobs_updated"] += 1


def _reconcile_joined_table(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    markets_config: dict[str, Any],
    users_config: dict[str, Any],
    summary: dict[str, Any],
    apply: bool,
) -> None:
    if not _table_exists(connection, table_name):
        return
    for row in _joined_rows(connection, table_name):
        prospect_state = (
            normalize_state(row["prospect_market_state"])
            or _derive_state_from_market(row["prospect_market"], markets_config, summary)
            or normalize_state(row["prospect_state"])
            or normalize_state(row["prospect_state_guess"])
        )
        state = normalize_state(row["market_state"]) or prospect_state
        if not state:
            summary["records_with_unknown_state"] += 1

        owner = (
            _state_owner(state, users_config, summary["states_with_no_owner"])
            or _clean_text(row["prospect_owner_username"])
        )
        changes = _field_changes(
            row,
            {
                "market_state": state,
                "owner_username": owner,
            },
        )
        if _apply_update(
            connection,
            table_name=table_name,
            row_id=int(row["id"]),
            changes=changes,
            apply=apply,
        ):
            summary[f"{table_name}_updated"] += 1


def reconcile(apply: bool) -> dict[str, Any]:
    load_env()
    db_path = get_database_path()
    markets_config = _load_optional_yaml("markets.yaml")
    users_config = _load_optional_yaml("users.yaml")
    conflicts = validate_exclusive_territories(users_config)
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "database": str(project_path(db_path)),
        "prospects_updated": 0,
        "dashboard_jobs_updated": 0,
        "outreach_queue_updated": 0,
        "quotes_updated": 0,
        "records_with_unknown_market": 0,
        "records_with_unknown_state": 0,
        "states_with_no_owner": set(),
        "territory_conflicts": conflicts,
    }

    connection = db.init_db(db_path) if apply else _connect_readonly(project_path(db_path))
    try:
        _reconcile_prospects(
            connection,
            markets_config=markets_config,
            users_config=users_config,
            summary=summary,
            apply=apply,
        )
        _reconcile_dashboard_jobs(
            connection,
            markets_config=markets_config,
            summary=summary,
            apply=apply,
        )
        _reconcile_joined_table(
            connection,
            table_name="outreach_queue",
            markets_config=markets_config,
            users_config=users_config,
            summary=summary,
            apply=apply,
        )
        _reconcile_joined_table(
            connection,
            table_name="quotes",
            markets_config=markets_config,
            users_config=users_config,
            summary=summary,
            apply=apply,
        )
        if apply:
            connection.commit()
    finally:
        connection.close()

    summary["states_with_no_owner"] = sorted(summary["states_with_no_owner"])
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    print(f"mode: {summary['mode']}")
    print(f"database: {summary['database']}")
    print(f"prospects updated: {summary['prospects_updated']}")
    print(f"dashboard_jobs updated: {summary['dashboard_jobs_updated']}")
    print(f"outreach_queue updated: {summary['outreach_queue_updated']}")
    print(f"quotes updated: {summary['quotes_updated']}")
    print(f"records with unknown market: {summary['records_with_unknown_market']}")
    print(f"records with unknown state: {summary['records_with_unknown_state']}")
    print(f"states with no owner: {', '.join(summary['states_with_no_owner']) or 'none'}")
    print(f"territory conflicts: {len(summary['territory_conflicts'])}")
    for conflict in summary["territory_conflicts"]:
        print(f"  - {json.dumps(conflict, sort_keys=True)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill territory ownership fields.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    mode.add_argument("--apply", action="store_true", help="Write backfilled values.")
    args = parser.parse_args()

    summary = reconcile(apply=bool(args.apply))
    print_summary(summary)
    return 1 if summary["territory_conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
