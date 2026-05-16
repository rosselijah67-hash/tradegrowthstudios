"""CRM task schema and persistence helpers."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import date, datetime, time as datetime_time, timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Any, Mapping

from . import db, territories
from .config import load_yaml_config

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ provides zoneinfo.
    ZoneInfo = None  # type: ignore[assignment]


TASK_TYPES = {
    "follow_up",
    "needs_quote",
    "call_scheduled",
    "proposal_follow_up",
    "collect_assets",
    "client_access_needed",
    "public_packet_needed",
    "draft_review",
    "send_outreach",
    "contract_deposit",
    "project_handoff",
    "launch_qa",
    "custom",
}
TASK_STATUSES = {"open", "in_progress", "waiting", "done", "cancelled"}
TASK_STATUS_ALIASES = {
    "complete": "done",
    "completed": "done",
    "closed": "done",
    "canceled": "cancelled",
}
TASK_PRIORITIES = {"low", "normal", "high", "urgent"}

TASK_TYPE_OPTIONS = tuple((value, value.replace("_", " ").title()) for value in sorted(TASK_TYPES))
TASK_STATUS_OPTIONS = (
    ("open", "Open"),
    ("in_progress", "In Progress"),
    ("waiting", "Waiting"),
    ("done", "Done"),
    ("cancelled", "Cancelled"),
)
TASK_PRIORITY_OPTIONS = (
    ("low", "Low"),
    ("normal", "Normal"),
    ("high", "High"),
    ("urgent", "Urgent"),
)
TASK_TYPE_LABELS = dict(TASK_TYPE_OPTIONS)
TASK_STATUS_LABELS = dict(TASK_STATUS_OPTIONS)
TASK_PRIORITY_LABELS = dict(TASK_PRIORITY_OPTIONS)
OPEN_STATUSES = {"open", "in_progress", "waiting"}
CLOSED_STATUSES = {"done", "cancelled"}
SCHEMA_SQL = db.CRM_TASK_SCHEMA_SQL

_UNSET = object()


def ensure_schema_for_connection(conn: sqlite3.Connection) -> None:
    db.ensure_task_schema(conn)


def ensure_schema(db_path: str | Path | None = None) -> None:
    conn = db.connect(db_path)
    try:
        ensure_schema_for_connection(conn)
        conn.commit()
    finally:
        conn.close()


def generate_task_key() -> str:
    return f"task-{secrets.token_hex(8)}"


def load_task_templates() -> dict[str, Any]:
    try:
        templates = load_yaml_config("task_templates.yaml")
    except FileNotFoundError:
        return {}
    if not isinstance(templates, dict):
        raise ValueError("config/task_templates.yaml must contain a top-level mapping.")
    return templates


def normalize_task_type(value: Any) -> str:
    token = _token(value) or "custom"
    if token not in TASK_TYPES:
        raise ValueError(f"Unsupported CRM task type: {value!r}")
    return token


def normalize_task_status(value: Any) -> str:
    token = _token(value) or "open"
    token = TASK_STATUS_ALIASES.get(token, token)
    if token not in TASK_STATUSES:
        raise ValueError(f"Unsupported CRM task status: {value!r}")
    return token


def normalize_priority(value: Any) -> str:
    token = _token(value) or "normal"
    if token not in TASK_PRIORITIES:
        raise ValueError(f"Unsupported CRM task priority: {value!r}")
    return token


def normalize_status(value: Any) -> str:
    return normalize_task_status(value)


def task_type_label(task_type: Any) -> str:
    return TASK_TYPE_LABELS.get(normalize_task_type(task_type), "Custom")


def build_due_at(due_date: Any, due_time: Any, timezone: str | None = None) -> str | None:
    clean_date = _normalize_due_date(due_date)
    clean_time = _normalize_due_time(due_time)
    if clean_date is None:
        return None
    if clean_time is None:
        return clean_date

    combined = datetime.combine(
        date.fromisoformat(clean_date),
        datetime_time.fromisoformat(clean_time),
    )
    timezone_name = _clean_text(timezone)
    if timezone_name:
        if ZoneInfo is None:
            raise ValueError("Timezone support is unavailable in this Python runtime.")
        try:
            local_zone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"Unsupported timezone: {timezone_name}") from exc
        combined = combined.replace(tzinfo=local_zone).astimezone(datetime_timezone.utc)
    return combined.replace(microsecond=0).isoformat()


def parse_due_parts(
    due_date: Any,
    due_time: Any,
    timezone: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    clean_date = _normalize_due_date(due_date)
    clean_time = _normalize_due_time(due_time)
    return clean_date, clean_time, build_due_at(clean_date, clean_time, timezone)


def snooze_target(
    value: Any,
    *,
    today: date | None = None,
    timezone: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    clean = _clean_text(value)
    if not clean:
        raise ValueError("Choose a snooze date.")
    base_date = today or datetime.now().date()
    quick_options = {
        "tomorrow": base_date + timedelta(days=1),
        "plus_2_days": base_date + timedelta(days=2),
        "next_week": base_date + timedelta(days=7),
    }
    if clean in quick_options:
        return parse_due_parts(quick_options[clean].isoformat(), None, timezone)
    if "T" in clean:
        due_date, due_time = clean.split("T", 1)
        return parse_due_parts(due_date, due_time[:8], timezone)
    return parse_due_parts(clean, None, timezone)


def create_task(
    conn: sqlite3.Connection,
    prospect_id: int,
    task_type: Any,
    title: Any,
    *,
    quote_id: int | None = None,
    contact_id: int | None = None,
    assigned_to: Any = None,
    created_by: Any = None,
    created_by_user: Any = None,
    owner_username: Any = None,
    market_state: Any = None,
    status: Any = "open",
    priority: Any = "normal",
    due_date: Any = None,
    due_time: Any = None,
    due_at: Any = None,
    timezone: str | None = None,
    contact_name: Any = None,
    contact_email: Any = None,
    contact_phone: Any = None,
    notes: Any = None,
    outcome_notes: Any = None,
    snooze_until: Any = None,
    completed_at: Any = None,
    cancelled_at: Any = None,
    auto_task_key: Any = None,
    metadata: Mapping[str, Any] | None = None,
    task_key: str | None = None,
) -> int:
    db.ensure_task_schema(conn)
    prospect = _require_prospect(conn, prospect_id)
    clean_type = normalize_task_type(task_type)
    clean_title = _clean_text(title)
    if not clean_title:
        raise ValueError("Task title is required.")

    clean_status = normalize_task_status(status)
    clean_priority = normalize_priority(priority)
    clean_due_date, clean_due_time, computed_due_at = parse_due_parts(
        due_date,
        due_time,
        timezone,
    )
    clean_due_at = _clean_text(due_at) or computed_due_at
    clean_created_by = _clean_text(created_by) or _clean_text(created_by_user)
    clean_owner = _clean_text(owner_username) or _clean_text(_row_value(prospect, "owner_username"))
    clean_market_state = _normalize_market_state(market_state) or _derive_market_state(prospect)
    contact = _load_contact(conn, contact_id, prospect_id) if contact_id is not None else None
    if quote_id is not None:
        _require_quote(conn, quote_id, prospect_id)

    now = db.utc_now()
    clean_completed_at = _clean_text(completed_at)
    clean_cancelled_at = _clean_text(cancelled_at)
    if clean_status == "done" and not clean_completed_at:
        clean_completed_at = now
    if clean_status == "cancelled" and not clean_cancelled_at:
        clean_cancelled_at = now

    clean_metadata = dict(metadata or {})
    clean_auto_task_key = _clean_text(auto_task_key) or _clean_text(clean_metadata.get("auto_task_key")) or None
    if clean_auto_task_key:
        clean_metadata["auto_task_key"] = clean_auto_task_key
        existing_auto_task = find_existing_auto_task_id(conn, clean_auto_task_key)
        if existing_auto_task is not None:
            return existing_auto_task

    task_id, created = _insert_task_with_unique_key(
        conn,
        task_key=_clean_text(task_key),
        prospect_id=int(prospect_id),
        quote_id=quote_id,
        contact_id=contact_id,
        assigned_to=_clean_text(assigned_to),
        created_by=clean_created_by,
        owner_username=clean_owner,
        market_state=clean_market_state,
        task_type=clean_type,
        title=clean_title,
        status=clean_status,
        priority=clean_priority,
        due_date=clean_due_date,
        due_time=clean_due_time,
        due_at=clean_due_at,
        timezone=_clean_text(timezone),
        contact_name=_clean_text(contact_name) or _clean_text(_row_value(contact, "name")),
        contact_email=_clean_text(contact_email) or _clean_text(_row_value(contact, "email")),
        contact_phone=_clean_text(contact_phone) or _clean_text(_row_value(contact, "phone")),
        notes=_clean_text(notes),
        outcome_notes=_clean_text(outcome_notes),
        snooze_until=_clean_text(snooze_until),
        completed_at=clean_completed_at,
        cancelled_at=clean_cancelled_at,
        auto_task_key=clean_auto_task_key,
        metadata=clean_metadata,
        now=now,
    )
    if created:
        _safe_log_task_event(
            conn,
            task_id,
            int(prospect_id),
            "task_created",
            note=clean_title,
            metadata={"task_type": clean_type, "priority": clean_priority},
        )
    return task_id


def update_task(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    task_type: Any = _UNSET,
    title: Any = _UNSET,
    status: Any = _UNSET,
    priority: Any = _UNSET,
    quote_id: Any = _UNSET,
    contact_id: Any = _UNSET,
    assigned_to: Any = _UNSET,
    created_by: Any = _UNSET,
    owner_username: Any = _UNSET,
    market_state: Any = _UNSET,
    due_date: Any = _UNSET,
    due_time: Any = _UNSET,
    due_at: Any = _UNSET,
    timezone: Any = _UNSET,
    contact_name: Any = _UNSET,
    contact_email: Any = _UNSET,
    contact_phone: Any = _UNSET,
    notes: Any = _UNSET,
    outcome_notes: Any = _UNSET,
    snooze_until: Any = _UNSET,
    completed_at: Any = _UNSET,
    cancelled_at: Any = _UNSET,
    metadata: Mapping[str, Any] | None | object = _UNSET,
) -> dict[str, Any]:
    db.ensure_task_schema(conn)
    current = _require_task(conn, task_id)
    prospect_id = int(current["prospect_id"])
    now = db.utc_now()
    updates: dict[str, Any] = {}

    if task_type is not _UNSET:
        updates["task_type"] = normalize_task_type(task_type)
    if title is not _UNSET:
        clean_title = _clean_text(title)
        if not clean_title:
            raise ValueError("Task title is required.")
        updates["title"] = clean_title
    if status is not _UNSET:
        clean_status = normalize_task_status(status)
        updates["status"] = clean_status
        if clean_status == "done":
            updates["completed_at"] = current.get("completed_at") or now
            updates["cancelled_at"] = None
        elif clean_status == "cancelled":
            updates["cancelled_at"] = current.get("cancelled_at") or now
        else:
            updates["completed_at"] = None
            updates["cancelled_at"] = None
    if priority is not _UNSET:
        updates["priority"] = normalize_priority(priority)
    if quote_id is not _UNSET:
        if quote_id is not None:
            _require_quote(conn, int(quote_id), prospect_id)
        updates["quote_id"] = quote_id
    if contact_id is not _UNSET:
        if contact_id is not None:
            _load_contact(conn, int(contact_id), prospect_id, required=True)
        updates["contact_id"] = contact_id
    for column, value in (
        ("assigned_to", assigned_to),
        ("created_by", created_by),
        ("owner_username", owner_username),
        ("contact_name", contact_name),
        ("contact_email", contact_email),
        ("contact_phone", contact_phone),
        ("notes", notes),
        ("outcome_notes", outcome_notes),
        ("snooze_until", snooze_until),
    ):
        if value is not _UNSET:
            updates[column] = _clean_text(value)
    if market_state is not _UNSET:
        updates["market_state"] = _normalize_market_state(market_state)
    if completed_at is not _UNSET:
        updates["completed_at"] = _clean_text(completed_at)
    if cancelled_at is not _UNSET:
        updates["cancelled_at"] = _clean_text(cancelled_at)
    if timezone is not _UNSET:
        updates["timezone"] = _clean_text(timezone)
    if due_date is not _UNSET:
        updates["due_date"] = _normalize_due_date(due_date)
    if due_time is not _UNSET:
        updates["due_time"] = _normalize_due_time(due_time)
    if due_at is not _UNSET:
        updates["due_at"] = _clean_text(due_at)
    elif due_date is not _UNSET or due_time is not _UNSET or timezone is not _UNSET:
        effective_due_date = updates.get("due_date", current.get("due_date"))
        effective_due_time = updates.get("due_time", current.get("due_time"))
        effective_timezone = updates.get("timezone", current.get("timezone"))
        updates["due_at"] = build_due_at(effective_due_date, effective_due_time, effective_timezone)
    if metadata is not _UNSET:
        merged_metadata = {}
        if isinstance(current.get("metadata"), dict):
            merged_metadata.update(current["metadata"])
        if metadata:
            merged_metadata.update(dict(metadata))
        updates["metadata_json"] = db.json_dumps(merged_metadata)

    if updates:
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE crm_tasks SET {assignments}, updated_at = ? WHERE id = ?",
            [*updates.values(), now, task_id],
        )
        _safe_log_task_event(
            conn,
            task_id,
            prospect_id,
            "task_updated",
            metadata={"updated_fields": sorted(updates)},
        )
    return get_task(conn, task_id) or {}


def complete_task(
    conn: sqlite3.Connection,
    task_id: int,
    outcome_notes: Any = None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {"status": "done"}
    if outcome_notes is not None:
        updates["outcome_notes"] = outcome_notes
    task = update_task(conn, task_id, **updates)
    _safe_log_task_event(
        conn,
        task_id,
        int(task["prospect_id"]),
        "task_completed",
        note=_clean_text(outcome_notes),
    )
    return task


def cancel_task(
    conn: sqlite3.Connection,
    task_id: int,
    outcome_notes: Any = None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {"status": "cancelled"}
    if outcome_notes is not None:
        updates["outcome_notes"] = outcome_notes
    task = update_task(conn, task_id, **updates)
    _safe_log_task_event(
        conn,
        task_id,
        int(task["prospect_id"]),
        "task_cancelled",
        note=_clean_text(outcome_notes),
    )
    return task


def snooze_task(
    conn: sqlite3.Connection,
    task_id: int,
    snooze_until: Any,
) -> dict[str, Any]:
    current = _require_task(conn, task_id)
    try:
        _, _, clean_snooze = snooze_target(snooze_until, timezone=current.get("timezone"))
    except ValueError:
        clean_snooze = _clean_text(snooze_until)
    task = update_task(
        conn,
        task_id,
        status="waiting" if current.get("status") in OPEN_STATUSES else _UNSET,
        snooze_until=clean_snooze,
    )
    _safe_log_task_event(
        conn,
        task_id,
        int(task["prospect_id"]),
        "task_snoozed",
        metadata={"snooze_until": clean_snooze},
    )
    return task


def get_task(conn: sqlite3.Connection, task_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM crm_tasks WHERE id = ?", (task_id,)).fetchone()
    return _task_from_row(row) if row else None


def list_tasks_for_prospect(
    conn: sqlite3.Connection,
    prospect_id: int,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    filters = {"prospect_id": prospect_id, "include_done": include_done}
    return list_tasks(conn, filters)


def list_tasks(
    conn: sqlite3.Connection,
    filters: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    clauses, params = _task_filter_clauses(filters)
    sql = "SELECT * FROM crm_tasks"
    if clauses:
        sql += f" WHERE {' AND '.join(clauses)}"
    sql += """
        ORDER BY
            due_at IS NULL,
            due_at ASC,
            CASE priority
                WHEN 'urgent' THEN 0
                WHEN 'high' THEN 1
                WHEN 'normal' THEN 2
                WHEN 'low' THEN 3
                ELSE 4
            END,
            updated_at DESC,
            id DESC
    """
    limit = _coerce_limit((filters or {}).get("limit"), default=200)
    sql += " LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_task_from_row(row) for row in rows]


def count_tasks_by_bucket(
    conn: sqlite3.Connection,
    filters: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    filter_values = dict(filters or {})
    filter_values.pop("limit", None)
    clauses, params = _task_filter_clauses(filter_values)
    sql = "SELECT status, due_date, due_at, snooze_until FROM crm_tasks"
    if clauses:
        sql += f" WHERE {' AND '.join(clauses)}"
    rows = conn.execute(sql, params).fetchall()
    now = db.utc_now()
    today = now[:10]
    buckets = {
        "total": 0,
        "active": 0,
        "open": 0,
        "in_progress": 0,
        "waiting": 0,
        "done": 0,
        "cancelled": 0,
        "overdue": 0,
        "due_today": 0,
        "upcoming": 0,
        "snoozed": 0,
        "unscheduled": 0,
    }
    for row in rows:
        status = normalize_task_status(_row_value(row, "status"))
        buckets["total"] += 1
        buckets[status] += 1
        if status in CLOSED_STATUSES:
            continue
        buckets["active"] += 1
        snooze_until = _clean_text(_row_value(row, "snooze_until"))
        if snooze_until and snooze_until > now:
            buckets["snoozed"] += 1
            continue
        due_at = _clean_text(_row_value(row, "due_at"))
        due_day = (due_at or _clean_text(_row_value(row, "due_date")))[:10]
        if not due_day:
            buckets["unscheduled"] += 1
        elif due_day < today:
            buckets["overdue"] += 1
        elif due_day == today:
            buckets["due_today"] += 1
        else:
            buckets["upcoming"] += 1
    return buckets


def create_task_from_template(
    conn: sqlite3.Connection,
    prospect_id: int,
    template_key: str,
    *,
    quote_id: int | None = None,
    contact_id: int | None = None,
    assigned_to: Any = None,
    created_by: Any = None,
    owner_username: Any = None,
    market_state: Any = None,
    due_date: Any = None,
    due_time: Any = None,
    timezone: str | None = None,
    title: Any = None,
    task_type: Any = None,
    priority: Any = None,
    notes: Any = None,
    metadata: Mapping[str, Any] | None = None,
    contact_name: Any = None,
    contact_email: Any = None,
    contact_phone: Any = None,
    format_values: Mapping[str, Any] | None = None,
    **extra_format_values: Any,
) -> int:
    templates = load_task_templates()
    template = templates.get(template_key)
    if not isinstance(template, Mapping):
        raise ValueError(f"Unknown CRM task template: {template_key}")

    prospect = _require_prospect(conn, prospect_id)
    context = _prospect_template_context(prospect)
    context.update(dict(format_values or {}))
    context.update(extra_format_values)
    template_title = _clean_text(title) or _format_template(
        _clean_text(template.get("title")) or "Task for {business_name}",
        context,
    )
    template_notes = _clean_text(notes)
    if not template_notes:
        template_notes = _format_template(_clean_text(template.get("notes")), context)
    if due_date is None and template.get("default_due_days") is not None:
        due_date = (datetime.now().date() + timedelta(days=int(template["default_due_days"]))).isoformat()

    merged_metadata: dict[str, Any] = {"template_key": template_key}
    template_metadata = template.get("metadata")
    if isinstance(template_metadata, Mapping):
        merged_metadata.update(dict(template_metadata))
    if metadata:
        merged_metadata.update(dict(metadata))

    return create_task(
        conn,
        prospect_id,
        task_type or template.get("task_type") or template_key,
        template_title,
        quote_id=quote_id,
        contact_id=contact_id,
        assigned_to=assigned_to,
        created_by=created_by,
        owner_username=owner_username,
        market_state=market_state,
        priority=priority or template.get("priority") or "normal",
        due_date=due_date,
        due_time=due_time,
        timezone=timezone,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        notes=template_notes,
        metadata=merged_metadata,
    )


def log_task_event(
    conn: sqlite3.Connection,
    task_id: int,
    prospect_id: int,
    event_type: str,
    note: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> int:
    event_metadata = {"task_id": task_id}
    if metadata:
        event_metadata.update(dict(metadata))
    return db.upsert_outreach_event(
        conn,
        event_key=f"task-event-{task_id}-{secrets.token_hex(6)}",
        prospect_id=prospect_id,
        campaign_key="crm_tasks",
        channel="crm",
        event_type=event_type,
        status="recorded",
        subject=note,
        metadata=event_metadata,
    )


def _insert_task_with_unique_key(conn: sqlite3.Connection, **values: Any) -> tuple[int, bool]:
    preferred_key = values.pop("task_key") or None
    for _attempt in range(8):
        task_key = preferred_key or generate_task_key()
        try:
            cursor = conn.execute(
                """
                INSERT INTO crm_tasks (
                    task_key, prospect_id, quote_id, contact_id, assigned_to, created_by,
                    owner_username, market_state, task_type, title, status, priority,
                    due_date, due_time, due_at, timezone, contact_name, contact_email,
                    contact_phone, notes, outcome_notes, snooze_until, completed_at,
                    cancelled_at, auto_task_key, metadata_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    task_key,
                    values["prospect_id"],
                    values["quote_id"],
                    values["contact_id"],
                    values["assigned_to"],
                    values["created_by"],
                    values["owner_username"],
                    values["market_state"],
                    values["task_type"],
                    values["title"],
                    values["status"],
                    values["priority"],
                    values["due_date"],
                    values["due_time"],
                    values["due_at"],
                    values["timezone"],
                    values["contact_name"],
                    values["contact_email"],
                    values["contact_phone"],
                    values["notes"],
                    values["outcome_notes"],
                    values["snooze_until"],
                    values["completed_at"],
                    values["cancelled_at"],
                    values["auto_task_key"],
                    db.json_dumps(values["metadata"]),
                    values["now"],
                    values["now"],
                ),
            )
            return int(cursor.lastrowid), True
        except sqlite3.IntegrityError:
            auto_task_key = _clean_text(values.get("auto_task_key"))
            if auto_task_key:
                existing_id = find_existing_auto_task_id(conn, auto_task_key)
                if existing_id is not None:
                    return existing_id, False
            if preferred_key:
                raise
    raise RuntimeError("Could not generate a unique task key.")


def find_existing_auto_task_id(conn: sqlite3.Connection, auto_task_key: Any) -> int | None:
    clean_key = _clean_text(auto_task_key)
    if not clean_key:
        return None
    row = conn.execute(
        """
        SELECT id
        FROM crm_tasks
        WHERE auto_task_key = ?
          AND status IN ('open', 'in_progress', 'waiting')
        ORDER BY id DESC
        LIMIT 1
        """,
        (clean_key,),
    ).fetchone()
    if row is not None:
        return int(_row_value(row, "id"))

    rows = conn.execute(
        """
        SELECT id, metadata_json
        FROM crm_tasks
        WHERE status IN ('open', 'in_progress', 'waiting')
          AND metadata_json LIKE '%auto_task_key%'
        ORDER BY id DESC
        """
    ).fetchall()
    for row in rows:
        metadata = _json_loads(_row_value(row, "metadata_json"), {})
        if isinstance(metadata, dict) and _clean_text(metadata.get("auto_task_key")) == clean_key:
            return int(_row_value(row, "id"))
    return None


def _task_filter_clauses(filters: Mapping[str, Any] | None) -> tuple[list[str], list[Any]]:
    values = filters or {}
    clauses: list[str] = []
    params: list[Any] = []
    explicit_status = values.get("status") is not None or values.get("statuses") is not None
    if not explicit_status and not _truthy(values.get("include_done")):
        clauses.append("status NOT IN ('done', 'cancelled')")
    if values.get("status") is not None:
        clauses.append("status = ?")
        params.append(normalize_task_status(values["status"]))
    if values.get("statuses") is not None:
        statuses = [normalize_task_status(status) for status in _iter_filter_values(values["statuses"])]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

    simple_filters = (
        ("prospect_id", "prospect_id", int),
        ("quote_id", "quote_id", int),
        ("auto_task_key", "auto_task_key", _clean_text),
        ("assigned_to", "assigned_to", _clean_text),
        ("owner_username", "owner_username", _clean_text),
        ("task_type", "task_type", normalize_task_type),
        ("priority", "priority", normalize_priority),
    )
    for filter_key, column, normalizer in simple_filters:
        if values.get(filter_key) is None:
            continue
        clauses.append(f"{column} = ?")
        params.append(normalizer(values[filter_key]))
    if values.get("market_state") is not None:
        clauses.append("market_state = ?")
        params.append(_normalize_market_state(values["market_state"]))
    if values.get("due_before") is not None:
        clauses.append("due_at <= ?")
        params.append(_clean_text(values["due_before"]))
    if values.get("due_after") is not None:
        clauses.append("due_at >= ?")
        params.append(_clean_text(values["due_after"]))
    if values.get("due_date") is not None:
        clauses.append("due_date = ?")
        params.append(_normalize_due_date(values["due_date"]))
    return clauses, params


def _require_task(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} does not exist.")
    return task


def _require_prospect(conn: sqlite3.Connection, prospect_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if row is None:
        raise ValueError(f"Prospect {prospect_id} does not exist.")
    return row


def _require_quote(conn: sqlite3.Connection, quote_id: int, prospect_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        raise ValueError(f"Quote {quote_id} does not exist.")
    if int(_row_value(row, "prospect_id") or 0) != int(prospect_id):
        raise ValueError(f"Quote {quote_id} does not belong to prospect {prospect_id}.")
    return row


def _load_contact(
    conn: sqlite3.Connection,
    contact_id: int,
    prospect_id: int,
    *,
    required: bool = False,
) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row is None:
        if required:
            raise ValueError(f"Contact {contact_id} does not exist.")
        return None
    if int(_row_value(row, "prospect_id") or 0) != int(prospect_id):
        raise ValueError(f"Contact {contact_id} does not belong to prospect {prospect_id}.")
    return row


def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
    task = db.row_to_dict(row)
    task["metadata"] = _json_loads(task.get("metadata_json"), {})
    if not task.get("auto_task_key"):
        task["auto_task_key"] = _clean_text(task["metadata"].get("auto_task_key"))
    if not task.get("snooze_until") and task.get("snoozed_until"):
        task["snooze_until"] = task.get("snoozed_until")
    if not task.get("created_by") and task.get("created_by_user"):
        task["created_by"] = task.get("created_by_user")
    return task


def _prospect_template_context(prospect: sqlite3.Row) -> dict[str, Any]:
    return {
        "business_name": _row_value(prospect, "business_name") or "this business",
        "market": _row_value(prospect, "market") or "",
        "niche": _row_value(prospect, "niche") or "",
        "city": _row_value(prospect, "city") or _row_value(prospect, "city_guess") or "",
        "state": _derive_market_state(prospect) or "",
        "phone": _row_value(prospect, "phone") or "",
        "website_url": _row_value(prospect, "website_url") or "",
    }


def _derive_market_state(prospect: sqlite3.Row | None) -> str | None:
    for key in ("market_state", "state", "state_guess"):
        state = _normalize_market_state(_row_value(prospect, key))
        if state:
            return state
    return None


def _normalize_market_state(value: Any) -> str | None:
    clean = _clean_text(value)
    if not clean:
        return None
    return territories.normalize_state(clean) or clean.upper()


def _normalize_due_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    clean = _clean_text(value)
    if not clean:
        return None
    try:
        return date.fromisoformat(clean[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("Due date must use YYYY-MM-DD.") from exc


def _normalize_due_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, datetime_time):
        timespec = "seconds" if value.second else "minutes"
        return value.replace(microsecond=0).isoformat(timespec=timespec)
    clean = _clean_text(value)
    if not clean:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(clean[:8], fmt).time()
        except ValueError:
            continue
        timespec = "seconds" if parsed.second else "minutes"
        return parsed.isoformat(timespec=timespec)
    raise ValueError("Due time must use HH:MM or HH:MM:SS.")


def _safe_log_task_event(
    conn: sqlite3.Connection,
    task_id: int,
    prospect_id: int,
    event_type: str,
    note: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> int | None:
    try:
        return log_task_event(conn, task_id, prospect_id, event_type, note, metadata)
    except sqlite3.OperationalError as exc:
        if "outreach_events" in str(exc):
            return None
        raise


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _format_template(template: str, values: Mapping[str, Any]) -> str:
    if not template:
        return ""
    return template.format_map(_SafeFormatDict(values))


def _iter_filter_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _coerce_limit(value: Any, *, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 1000))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _row_value(row: sqlite3.Row | Mapping[str, Any] | None, key: str) -> Any:
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else row
    return row[key] if key in keys else None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
