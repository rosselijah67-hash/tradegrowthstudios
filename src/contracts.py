"""CRM contract persistence helpers.

Contracts reuse quote/prospect data for convenience, but legal identity and
signer authority fields are intentionally left for manual confirmation.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any, Mapping

from . import db, quotes as quote_service


VALID_CONTRACT_STATUSES = {
    "draft",
    "generated",
    "sent",
    "delivered",
    "completed",
    "declined",
    "voided",
    "error",
    "superseded",
}

MANUAL_CONFIRMATION_REQUIRED = (
    "legal_business_name",
    "business_entity_type",
    "billing_address",
    "signer_name",
    "signer_title",
    "signer_email",
    "additional_signers",
    "additional_sections",
)

HEADER_COLUMNS = {
    "owner_username",
    "market_state",
    "title",
    "template_key",
    "legal_business_name",
    "business_entity_type",
    "billing_address",
    "signer_name",
    "signer_title",
    "signer_email",
    "signer_phone",
    "client_business_name",
    "client_contact_name",
    "client_email",
    "client_phone",
    "website_url",
    "effective_date",
    "start_date",
    "term_months",
    "one_time_total_cents",
    "recurring_monthly_total_cents",
    "deposit_due_cents",
    "balance_due_cents",
    "template_key",
}

INTEGER_COLUMNS = {
    "version",
    "term_months",
    "one_time_total_cents",
    "recurring_monthly_total_cents",
    "deposit_due_cents",
    "balance_due_cents",
}


def generate_contract_key() -> str:
    return f"contract-{secrets.token_hex(8)}"


def load_contract(conn: sqlite3.Connection, contract_id: int) -> dict[str, Any] | None:
    db.ensure_contract_schema(conn)
    row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
    if row is None:
        return None
    contract = _contract_from_row(row)
    contract["events"] = _load_contract_events(conn, contract_id)
    return contract


def get_contract_by_key(conn: sqlite3.Connection, contract_key: str) -> dict[str, Any] | None:
    db.ensure_contract_schema(conn)
    row = conn.execute("SELECT id FROM contracts WHERE contract_key = ?", (contract_key,)).fetchone()
    if row is None:
        return None
    return load_contract(conn, int(row["id"]))


def list_contracts(
    conn: sqlite3.Connection,
    filters: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    db.ensure_contract_schema(conn)
    values = dict(filters or {})
    clauses = ["1 = 1"]
    params: list[Any] = []

    simple_filters = (
        ("prospect_id", "c.prospect_id", int),
        ("quote_id", "c.quote_id", int),
        ("status", "c.status", _normalize_status),
        ("owner_username", "c.owner_username", _clean_text),
        ("market_state", "c.market_state", _clean_text),
        ("contract_key", "c.contract_key", _clean_text),
        ("docusign_envelope_id", "c.docusign_envelope_id", _clean_text),
    )
    for filter_key, column, normalizer in simple_filters:
        if values.get(filter_key) is None:
            continue
        clauses.append(f"{column} = ?")
        params.append(normalizer(values[filter_key]))

    statuses = _iter_filter_values(values.get("statuses"))
    if statuses:
        normalized_statuses = [_normalize_status(status) for status in statuses]
        clauses.append(f"c.status IN ({', '.join('?' for _ in normalized_statuses)})")
        params.extend(normalized_statuses)

    limit = _coerce_limit(values.get("limit"), default=200)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT c.*,
               p.business_name AS prospect_business_name,
               p.market AS prospect_market,
               p.niche AS prospect_niche,
               q.quote_key AS quote_key,
               q.status AS quote_status
        FROM contracts c
        LEFT JOIN prospects p ON p.id = c.prospect_id
        LEFT JOIN quotes q ON q.id = c.quote_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_contract_from_row(row) for row in rows]


def list_contracts_for_prospect(
    conn: sqlite3.Connection,
    prospect_id: int,
) -> list[dict[str, Any]]:
    return list_contracts(conn, {"prospect_id": prospect_id, "limit": 1000})


def list_contracts_for_quote(
    conn: sqlite3.Connection,
    quote_id: int,
) -> list[dict[str, Any]]:
    return list_contracts(conn, {"quote_id": quote_id, "limit": 1000})


def create_contract_from_quote(
    conn: sqlite3.Connection,
    quote_id: int,
    created_by: str | None = None,
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    quote = quote_service.get_quote(conn, quote_id)
    if quote is None:
        raise ValueError(f"Quote {quote_id} does not exist.")

    prospect = _load_prospect(conn, int(quote["prospect_id"]))
    if prospect is None:
        raise ValueError(f"Prospect {quote['prospect_id']} does not exist.")
    primary_contact = _load_primary_contact(conn, int(prospect["id"]))

    business_name = (
        _clean_text(quote.get("client_business_name"))
        or _clean_text(prospect.get("business_name"))
        or "this business"
    )
    client_contact_name = (
        _clean_text(quote.get("client_contact_name"))
        or _clean_text((primary_contact or {}).get("name"))
    )
    client_email = (
        _clean_text(quote.get("client_email"))
        or _clean_text((primary_contact or {}).get("email"))
    )
    client_phone = (
        _clean_text(quote.get("client_phone"))
        or _clean_text((primary_contact or {}).get("phone"))
        or _clean_text(prospect.get("phone"))
    )
    owner_username = (
        _clean_text(quote.get("owner_username"))
        or _clean_text(prospect.get("owner_username"))
        or _clean_text(created_by)
    )
    market_state = _clean_text(quote.get("market_state")) or _prospect_state(prospect)
    now = db.utc_now()

    values = {
        "prospect_id": int(prospect["id"]),
        "quote_id": int(quote["id"]),
        "owner_username": owner_username,
        "market_state": market_state,
        "status": "draft",
        "title": f"Service Agreement for {business_name}",
        "template_key": "service_contract",
        "version": 1,
        "legal_business_name": None,
        "business_entity_type": None,
        "billing_address": None,
        "signer_name": None,
        "signer_title": None,
        "signer_email": None,
        "signer_phone": None,
        "client_business_name": business_name,
        "client_contact_name": client_contact_name,
        "client_email": client_email,
        "client_phone": client_phone,
        "website_url": _clean_text(quote.get("website_url")) or _clean_text(prospect.get("website_url")),
        "effective_date": None,
        "start_date": None,
        "term_months": _int_value(quote.get("term_months")),
        "one_time_total_cents": _int_value(quote.get("one_time_total_cents")),
        "recurring_monthly_total_cents": _int_value(quote.get("recurring_monthly_total_cents")),
        "deposit_due_cents": _int_value(quote.get("deposit_due_cents")),
        "balance_due_cents": _int_value(quote.get("balance_due_cents")),
        "sections": [],
        "signers": [],
        "metadata": {
            "source": "crm_contract_module",
            "created_by": _clean_text(created_by),
            "source_quote_key": quote.get("quote_key"),
            "manual_confirmation_required": list(MANUAL_CONFIRMATION_REQUIRED),
        },
        "created_at": now,
        "updated_at": now,
    }

    for _attempt in range(8):
        contract_key = generate_contract_key()
        values["contract_key"] = contract_key
        values["variables"] = _build_contract_variables_snapshot(
            values,
            prospect=prospect,
            quote=quote,
        )
        try:
            cursor = conn.execute(
                """
                INSERT INTO contracts (
                    contract_key, prospect_id, quote_id, owner_username, market_state,
                    status, title, template_key, version, legal_business_name,
                    business_entity_type, billing_address, signer_name, signer_title,
                    signer_email, signer_phone, client_business_name, client_contact_name,
                    client_email, client_phone, website_url, effective_date, start_date,
                    term_months, one_time_total_cents, recurring_monthly_total_cents,
                    deposit_due_cents, balance_due_cents, variables_json, sections_json,
                    signers_json, metadata_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    contract_key,
                    values["prospect_id"],
                    values["quote_id"],
                    values["owner_username"],
                    values["market_state"],
                    values["status"],
                    values["title"],
                    values["template_key"],
                    values["version"],
                    values["legal_business_name"],
                    values["business_entity_type"],
                    values["billing_address"],
                    values["signer_name"],
                    values["signer_title"],
                    values["signer_email"],
                    values["signer_phone"],
                    values["client_business_name"],
                    values["client_contact_name"],
                    values["client_email"],
                    values["client_phone"],
                    values["website_url"],
                    values["effective_date"],
                    values["start_date"],
                    values["term_months"],
                    values["one_time_total_cents"],
                    values["recurring_monthly_total_cents"],
                    values["deposit_due_cents"],
                    values["balance_due_cents"],
                    _json_dumps(values["variables"], {}),
                    _json_dumps(values["sections"], []),
                    _json_dumps(values["signers"], []),
                    _json_dumps(values["metadata"], {}),
                    now,
                    now,
                ),
            )
            contract_id = int(cursor.lastrowid)
            break
        except sqlite3.IntegrityError:
            continue
    else:
        raise RuntimeError("Could not generate a unique contract key.")

    log_contract_event(
        conn,
        contract_id,
        prospect_id=int(prospect["id"]),
        quote_id=int(quote["id"]),
        event_type="contract_created",
        metadata={"created_by": _clean_text(created_by), "quote_key": quote.get("quote_key")},
    )
    return load_contract(conn, contract_id) or {}


def update_contract_header(
    conn: sqlite3.Connection,
    contract_id: int,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    current = _require_contract(conn, contract_id)
    updates: dict[str, Any] = {}

    for column in HEADER_COLUMNS:
        if column not in values:
            continue
        value = values[column]
        if column in INTEGER_COLUMNS:
            updates[column] = _int_value(value)
        else:
            updates[column] = _clean_text(value)

    metadata_updates = _mapping_or_none(values.get("metadata"))
    if metadata_updates is not None:
        metadata = dict(current.get("metadata") or {})
        metadata.update(metadata_updates)
        updates["metadata_json"] = _json_dumps(metadata, {})

    variables_updates = _mapping_or_none(values.get("variables"))
    if variables_updates is not None:
        variables = dict(current.get("variables") or {})
        variables.update(variables_updates)
        updates["variables_json"] = _json_dumps(variables, {})

    if updates:
        updates["updated_at"] = db.utc_now()
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE contracts SET {assignments} WHERE id = ?",
            [*updates.values(), contract_id],
        )
        log_contract_event(
            conn,
            contract_id,
            event_type="contract_updated",
            metadata={"updated_fields": sorted(updates)},
        )
    return load_contract(conn, contract_id) or {}


def update_contract_signers(
    conn: sqlite3.Connection,
    contract_id: int,
    signers: list[Mapping[str, Any]],
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    _require_contract(conn, contract_id)
    normalized = _normalize_signers(signers)
    primary = normalized[0] if normalized else {}
    now = db.utc_now()
    conn.execute(
        """
        UPDATE contracts
        SET signers_json = ?,
            signer_name = ?,
            signer_title = ?,
            signer_email = ?,
            signer_phone = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            _json_dumps(normalized, []),
            primary.get("name"),
            primary.get("title"),
            primary.get("email"),
            primary.get("phone"),
            now,
            contract_id,
        ),
    )
    log_contract_event(
        conn,
        contract_id,
        event_type="contract_signers_updated",
        metadata={"signer_count": len(normalized)},
    )
    return load_contract(conn, contract_id) or {}


def update_contract_sections(
    conn: sqlite3.Connection,
    contract_id: int,
    sections: list[Mapping[str, Any]],
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    _require_contract(conn, contract_id)
    normalized = _normalize_sections(sections)
    conn.execute(
        """
        UPDATE contracts
        SET sections_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (_json_dumps(normalized, []), db.utc_now(), contract_id),
    )
    log_contract_event(
        conn,
        contract_id,
        event_type="contract_sections_updated",
        metadata={"section_count": len(normalized)},
    )
    return load_contract(conn, contract_id) or {}


def update_contract_generated_paths(
    conn: sqlite3.Connection,
    contract_id: int,
    docx_path: str | None = None,
    html_path: str | None = None,
    pdf_path: str | None = None,
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    _require_contract(conn, contract_id)
    updates = {}
    if docx_path is not None:
        updates["generated_docx_path"] = _clean_text(docx_path)
    if html_path is not None:
        updates["generated_html_path"] = _clean_text(html_path)
    if pdf_path is not None:
        updates["generated_pdf_path"] = _clean_text(pdf_path)
    if updates:
        updates["updated_at"] = db.utc_now()
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE contracts SET {assignments} WHERE id = ?",
            [*updates.values(), contract_id],
        )
        log_contract_event(
            conn,
            contract_id,
            event_type="contract_generated_paths_updated",
            metadata={key: value for key, value in updates.items() if key != "updated_at"},
        )
    return load_contract(conn, contract_id) or {}


def update_contract_status(
    conn: sqlite3.Connection,
    contract_id: int,
    status: str,
    note: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    current = _require_contract(conn, contract_id)
    normalized = _normalize_status(status)
    now = db.utc_now()
    updates: dict[str, Any] = {"status": normalized, "updated_at": now}
    timestamp_column = {
        "sent": "sent_at",
        "completed": "completed_at",
        "declined": "declined_at",
        "voided": "voided_at",
    }.get(normalized)
    if timestamp_column:
        updates[timestamp_column] = current.get(timestamp_column) or now
    if metadata:
        merged_metadata = dict(current.get("metadata") or {})
        merged_metadata.update(dict(metadata))
        updates["metadata_json"] = _json_dumps(merged_metadata, {})

    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE contracts SET {assignments} WHERE id = ?",
        [*updates.values(), contract_id],
    )
    log_contract_event(
        conn,
        contract_id,
        event_type=f"contract_status_{normalized}",
        status="recorded",
        note=note,
        metadata={
            "old_status": current.get("status"),
            "new_status": normalized,
            **dict(metadata or {}),
        },
    )
    return load_contract(conn, contract_id) or {}


def update_docusign_status(
    conn: sqlite3.Connection,
    contract_id: int,
    envelope_id: str | None = None,
    status: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    current = _require_contract(conn, contract_id)
    updates: dict[str, Any] = {}
    if envelope_id is not None:
        updates["docusign_envelope_id"] = _clean_text(envelope_id)
    if status is not None:
        updates["docusign_status"] = _clean_text(status)
    if metadata:
        merged_metadata = dict(current.get("metadata") or {})
        merged_metadata["docusign_last_update"] = dict(metadata)
        updates["metadata_json"] = _json_dumps(merged_metadata, {})
    if updates:
        updates["docusign_status_updated_at"] = db.utc_now()
        updates["updated_at"] = updates["docusign_status_updated_at"]
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE contracts SET {assignments} WHERE id = ?",
            [*updates.values(), contract_id],
        )
        log_contract_event(
            conn,
            contract_id,
            event_type="docusign_status_updated",
            metadata={
                "envelope_id": updates.get("docusign_envelope_id", current.get("docusign_envelope_id")),
                "docusign_status": updates.get("docusign_status", current.get("docusign_status")),
                **dict(metadata or {}),
            },
        )
    return load_contract(conn, contract_id) or {}


def create_contract_revision(
    conn: sqlite3.Connection,
    contract_id: int,
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    source = _require_contract(conn, contract_id)
    now = db.utc_now()
    title = _clean_text(source.get("title")) or "Service Agreement"
    if "revision" not in title.lower():
        title = f"{title} Revision"
    version = _int_value(source.get("version"), default=1) + 1

    for _attempt in range(8):
        contract_key = generate_contract_key()
        try:
            cursor = conn.execute(
                """
                INSERT INTO contracts (
                    contract_key, prospect_id, quote_id, owner_username, market_state,
                    status, title, template_key, version, legal_business_name,
                    business_entity_type, billing_address, signer_name, signer_title,
                    signer_email, signer_phone, client_business_name, client_contact_name,
                    client_email, client_phone, website_url, effective_date, start_date,
                    term_months, one_time_total_cents, recurring_monthly_total_cents,
                    deposit_due_cents, balance_due_cents, variables_json, sections_json,
                    signers_json, supersedes_contract_id, metadata_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    contract_key,
                    source["prospect_id"],
                    source.get("quote_id"),
                    source.get("owner_username"),
                    source.get("market_state"),
                    title,
                    source.get("template_key"),
                    version,
                    source.get("legal_business_name"),
                    source.get("business_entity_type"),
                    source.get("billing_address"),
                    source.get("signer_name"),
                    source.get("signer_title"),
                    source.get("signer_email"),
                    source.get("signer_phone"),
                    source.get("client_business_name"),
                    source.get("client_contact_name"),
                    source.get("client_email"),
                    source.get("client_phone"),
                    source.get("website_url"),
                    source.get("effective_date"),
                    source.get("start_date"),
                    _int_value(source.get("term_months")),
                    _int_value(source.get("one_time_total_cents")),
                    _int_value(source.get("recurring_monthly_total_cents")),
                    _int_value(source.get("deposit_due_cents")),
                    _int_value(source.get("balance_due_cents")),
                    _json_dumps(source.get("variables") or {}, {}),
                    _json_dumps(source.get("sections") or [], []),
                    _json_dumps(source.get("signers") or [], []),
                    source["id"],
                    _json_dumps(
                        {
                            **dict(source.get("metadata") or {}),
                            "revision_of": source.get("contract_key"),
                            "source_contract_id": source.get("id"),
                        },
                        {},
                    ),
                    now,
                    now,
                ),
            )
            new_contract_id = int(cursor.lastrowid)
            break
        except sqlite3.IntegrityError:
            continue
    else:
        raise RuntimeError("Could not generate a unique contract key.")

    update_contract_status(
        conn,
        int(source["id"]),
        "superseded",
        note=f"Superseded by contract {contract_key}.",
        metadata={"new_contract_id": new_contract_id, "new_contract_key": contract_key},
    )
    log_contract_event(
        conn,
        new_contract_id,
        event_type="contract_revision_created",
        metadata={"supersedes_contract_id": source.get("id"), "version": version},
    )
    return load_contract(conn, new_contract_id) or {}


def log_contract_event(
    conn: sqlite3.Connection,
    contract_id: int | None,
    prospect_id: int | None = None,
    quote_id: int | None = None,
    event_type: str | None = None,
    status: str = "recorded",
    note: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> int:
    db.ensure_contract_schema(conn)
    if contract_id is not None and (prospect_id is None or quote_id is None):
        row = conn.execute(
            "SELECT prospect_id, quote_id FROM contracts WHERE id = ?",
            (contract_id,),
        ).fetchone()
        if row is not None:
            if prospect_id is None:
                prospect_id = row["prospect_id"]
            if quote_id is None:
                quote_id = row["quote_id"]
    cursor = conn.execute(
        """
        INSERT INTO contract_events (
            contract_id, prospect_id, quote_id, event_type, status, note,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract_id,
            prospect_id,
            quote_id,
            event_type or "contract_event",
            status,
            note,
            _json_dumps(dict(metadata or {}), {}),
            db.utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def build_contract_variables(
    conn: sqlite3.Connection,
    contract_id: int,
) -> dict[str, Any]:
    db.ensure_contract_schema(conn)
    contract = _require_contract(conn, contract_id)
    prospect = _load_prospect(conn, int(contract["prospect_id"]))
    quote = None
    if contract.get("quote_id") is not None:
        quote = quote_service.get_quote(conn, int(contract["quote_id"]))
    return _build_contract_variables_snapshot(
        contract,
        prospect=prospect,
        quote=quote,
    )


def _require_contract(conn: sqlite3.Connection, contract_id: int) -> dict[str, Any]:
    contract = load_contract(conn, contract_id)
    if contract is None:
        raise ValueError(f"Contract {contract_id} does not exist.")
    return contract


def _load_contract_events(conn: sqlite3.Connection, contract_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM contract_events
        WHERE contract_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (contract_id,),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def _contract_from_row(row: sqlite3.Row) -> dict[str, Any]:
    contract = db.row_to_dict(row)
    contract["variables"] = _json_loads(contract.get("variables_json"), {})
    contract["sections"] = _json_loads(contract.get("sections_json"), [])
    contract["signers"] = _json_loads(contract.get("signers_json"), [])
    contract["metadata"] = _json_loads(contract.get("metadata_json"), {})
    return contract


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    event = db.row_to_dict(row)
    event["metadata"] = _json_loads(event.get("metadata_json"), {})
    return event


def _load_prospect(conn: sqlite3.Connection, prospect_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    return db.row_to_dict(row) if row is not None else None


def _load_primary_contact(conn: sqlite3.Connection, prospect_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM contacts
        WHERE prospect_id = ?
        ORDER BY
            CASE WHEN email IS NULL OR email = '' THEN 1 ELSE 0 END,
            COALESCE(confidence, 0) DESC,
            id ASC
        LIMIT 1
        """,
        (prospect_id,),
    ).fetchone()
    return db.row_to_dict(row) if row is not None else None


def _build_contract_variables_snapshot(
    contract: Mapping[str, Any],
    *,
    prospect: Mapping[str, Any] | None,
    quote: Mapping[str, Any] | None,
) -> dict[str, Any]:
    sections = _normalize_sections(_list_value(contract.get("sections")))
    signers = _normalize_signers(_list_value(contract.get("signers")))
    line_items = _line_item_views(_list_value((quote or {}).get("line_items")))
    display_name = (
        _clean_text(contract.get("client_business_name"))
        or _clean_text((quote or {}).get("client_business_name"))
        or _clean_text((prospect or {}).get("business_name"))
        or ""
    )
    website_url = (
        _clean_text(contract.get("website_url"))
        or _clean_text((quote or {}).get("website_url"))
        or _clean_text((prospect or {}).get("website_url"))
        or ""
    )
    phone = (
        _clean_text(contract.get("client_phone"))
        or _clean_text((quote or {}).get("client_phone"))
        or _clean_text((prospect or {}).get("phone"))
        or ""
    )
    assumptions = []
    quote_assumptions = (quote or {}).get("assumptions")
    if isinstance(quote_assumptions, Mapping):
        assumptions = [
            str(item).strip()
            for item in quote_assumptions.get("items", [])
            if str(item).strip()
        ]

    return {
        "business": {
            "display_name": display_name,
            "legal_name": _clean_text(contract.get("legal_business_name")) or "",
            "entity_type": _clean_text(contract.get("business_entity_type")) or "",
            "website_url": website_url,
            "phone": phone,
            "address_line": (
                _clean_text(contract.get("billing_address"))
                or _clean_text((prospect or {}).get("formatted_address"))
                or _clean_text((prospect or {}).get("address"))
                or ""
            ),
            "city": _clean_text((prospect or {}).get("city")) or _clean_text((prospect or {}).get("city_guess")) or "",
            "state": (
                _clean_text((prospect or {}).get("state"))
                or _clean_text((prospect or {}).get("state_guess"))
                or _clean_text(contract.get("market_state"))
                or ""
            ),
            "postal_code": _clean_text((prospect or {}).get("postal_code")) or "",
        },
        "signer_primary": {
            "name": _clean_text(contract.get("signer_name")) or "",
            "title": _clean_text(contract.get("signer_title")) or "",
            "email": _clean_text(contract.get("signer_email")) or "",
            "phone": _clean_text(contract.get("signer_phone")) or "",
        },
        "signers": signers,
        "quote": {
            "quote_key": _clean_text((quote or {}).get("quote_key")) or "",
            "package_key": _clean_text((quote or {}).get("package_key")) or "",
            "package_name": _clean_text((quote or {}).get("package_name")) or "",
            "title": _clean_text((quote or {}).get("title")) or "",
            "one_time_subtotal": quote_service.format_money((quote or {}).get("one_time_subtotal_cents")),
            "one_time_subtotal_cents": _int_value((quote or {}).get("one_time_subtotal_cents")),
            "one_time_discount": quote_service.format_money((quote or {}).get("one_time_discount_cents")),
            "one_time_discount_cents": _int_value((quote or {}).get("one_time_discount_cents")),
            "one_time_total": quote_service.format_money(contract.get("one_time_total_cents")),
            "one_time_total_cents": _int_value(contract.get("one_time_total_cents")),
            "recurring_monthly_total": quote_service.format_money(contract.get("recurring_monthly_total_cents")),
            "recurring_monthly_total_cents": _int_value(contract.get("recurring_monthly_total_cents")),
            "deposit_percent": _int_value((quote or {}).get("deposit_percent")),
            "deposit_due": quote_service.format_money(contract.get("deposit_due_cents")),
            "deposit_due_cents": _int_value(contract.get("deposit_due_cents")),
            "balance_due": quote_service.format_money(contract.get("balance_due_cents")),
            "balance_due_cents": _int_value(contract.get("balance_due_cents")),
            "term_months": _int_value(contract.get("term_months")),
            "valid_until": _clean_text((quote or {}).get("valid_until")) or "",
        },
        "scope": {
            "included_items": [
                item
                for item in line_items
                if not item["is_optional"] and not item["is_recurring"] and not item["is_discount"]
            ],
            "optional_items": [item for item in line_items if item["is_optional"]],
            "recurring_items": [item for item in line_items if item["is_recurring"]],
            "assumptions": assumptions,
            "client_visible_notes": _clean_text((quote or {}).get("client_visible_notes")) or "",
        },
        "contract": {
            "contract_key": _clean_text(contract.get("contract_key")) or "",
            "title": _clean_text(contract.get("title")) or "",
            "template_key": _clean_text(contract.get("template_key")) or "",
            "version": _int_value(contract.get("version"), default=1),
            "status": _clean_text(contract.get("status")) or "draft",
            "effective_date": _clean_text(contract.get("effective_date")) or "",
            "start_date": _clean_text(contract.get("start_date")) or "",
            "additional_sections": sections,
            "generated_docx_path": _clean_text(contract.get("generated_docx_path")) or "",
            "generated_pdf_path": _clean_text(contract.get("generated_pdf_path")) or "",
            "generated_html_path": _clean_text(contract.get("generated_html_path")) or "",
        },
        "manual_confirmation_required": list(MANUAL_CONFIRMATION_REQUIRED),
    }


def _line_item_views(items: list[Any]) -> list[dict[str, Any]]:
    output = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        if metadata.get("client_visible") is False:
            continue
        recurring_interval = _clean_text(item.get("recurring_interval"))
        item_type = _clean_text(item.get("item_type")) or "custom"
        output.append(
            {
                "item_key": _clean_text(item.get("item_key")) or "",
                "item_type": item_type,
                "category": _clean_text(item.get("category")) or "",
                "name": _clean_text(item.get("name")) or "Line item",
                "description": _clean_text(item.get("description")) or "",
                "quantity": item.get("quantity") if item.get("quantity") is not None else 1,
                "unit_label": _clean_text(metadata.get("unit_label")) or "each",
                "unit_price": quote_service.format_money(item.get("unit_price_cents")),
                "unit_price_cents": _int_value(item.get("unit_price_cents")),
                "line_total": quote_service.format_money(item.get("line_total_cents")),
                "line_total_cents": _int_value(item.get("line_total_cents")),
                "recurring_interval": recurring_interval or "",
                "is_optional": bool(item.get("is_optional")),
                "is_included": bool(item.get("is_included")),
                "is_recurring": item_type == "recurring" or recurring_interval == "monthly",
                "is_discount": item_type == "discount",
                "salesman_notes": _clean_text(metadata.get("salesman_notes")) or "",
            }
        )
    return output


def _normalize_signers(signers: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for index, signer in enumerate(signers, start=1):
        if not isinstance(signer, Mapping):
            continue
        clean = {
            "role": _clean_text(signer.get("role")) or "client",
            "name": _clean_text(signer.get("name")) or "",
            "title": _clean_text(signer.get("title")) or "",
            "email": _clean_text(signer.get("email")) or "",
            "phone": _clean_text(signer.get("phone")) or "",
            "required": _truthy(signer.get("required"), default=True),
            "routing_order": _int_value(signer.get("routing_order"), default=index),
            "signature_anchor": (
                _clean_text(signer.get("signature_anchor")) or f"/signer{index}_sign/"
            ),
            "date_anchor": _clean_text(signer.get("date_anchor")) or f"/signer{index}_date/",
        }
        metadata = signer.get("metadata")
        if isinstance(metadata, Mapping):
            clean["metadata"] = dict(metadata)
        normalized.append(clean)
    return normalized


def _normalize_sections(sections: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, Mapping):
            continue
        clean = {
            "section_key": _clean_text(section.get("section_key")) or f"section_{index}",
            "title": _clean_text(section.get("title")) or "",
            "body": _clean_text(section.get("body")) or "",
            "requires_signature": _truthy(section.get("requires_signature"), default=False),
            "signer_index": _int_value(section.get("signer_index"), default=0),
        }
        metadata = section.get("metadata")
        if isinstance(metadata, Mapping):
            clean["metadata"] = dict(metadata)
        normalized.append(clean)
    return normalized


def _normalize_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VALID_CONTRACT_STATUSES:
        raise ValueError(f"Unsupported contract status: {value!r}")
    return normalized


def _prospect_state(prospect: Mapping[str, Any]) -> str | None:
    for key in ("market_state", "state", "state_guess"):
        value = _clean_text(prospect.get(key))
        if value:
            return value.upper() if len(value) == 2 else value
    return None


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _iter_filter_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return fallback
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback
    if isinstance(fallback, list) and not isinstance(loaded, list):
        return fallback
    if isinstance(fallback, dict) and not isinstance(loaded, dict):
        return fallback
    return loaded


def _json_dumps(value: Any, fallback: Any) -> str:
    return json.dumps(fallback if value is None else value, sort_keys=True)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "required"}


def _coerce_limit(value: Any, *, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 1000))

