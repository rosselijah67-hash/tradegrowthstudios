"""Quote catalog and persistence helpers for the dashboard CRM."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from html import escape
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from . import db
from .config import load_yaml_config


VALID_QUOTE_STATUSES = {"draft", "sent", "accepted", "declined", "expired", "superseded"}
VALID_ITEM_TYPES = {"package", "addon", "recurring", "discount", "custom"}
VALID_RECURRING_INTERVALS = {None, "monthly"}


def load_quote_catalog() -> dict[str, Any]:
    """Load the human-editable quote catalog from config/quote_catalog.yaml."""

    catalog = load_yaml_config("quote_catalog.yaml")
    catalog.setdefault("base_packages", {})
    catalog.setdefault("recurring_retainers", {})
    catalog.setdefault("addons", {})
    catalog.setdefault("positioning", {})
    return catalog


def format_money(cents: Any) -> str:
    amount = int(cents or 0)
    sign = "-" if amount < 0 else ""
    dollars, remainder = divmod(abs(amount), 100)
    if remainder:
        return f"{sign}${dollars:,}.{remainder:02d}"
    return f"{sign}${dollars:,}"


def parse_money_to_cents(value: Any) -> int:
    """Parse a display money value into cents.

    Integer input is treated as already being cents. String and float input are
    treated as dollar values, so "$1,250.50" becomes 125050.
    """

    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("Boolean values cannot be parsed as money.")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, float):
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        if not text:
            return 0
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        text = re.sub(r"[$,\s]", "", text)
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid money value: {value!r}") from exc
        if negative:
            amount = -amount
    cents = (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def create_quote_for_prospect(
    conn: sqlite3.Connection,
    prospect_id: int,
    package_key: str | None = None,
) -> dict[str, Any]:
    db.ensure_quote_schema(conn)
    prospect = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if prospect is None:
        raise ValueError(f"Prospect {prospect_id} does not exist.")

    catalog = load_quote_catalog()
    package = _catalog_item(catalog.get("base_packages"), package_key) if package_key else None
    package_name = str(package.get("name") or "") if package else None
    business_name = str(prospect["business_name"] or "").strip()
    title = f"{package_name} for {business_name}" if package_name else f"Quote for {business_name}"
    now = db.utc_now()
    metadata = {"source": "crm_quote_module"}
    if package and package.get("requires_custom_quote"):
        metadata["requires_custom_quote"] = True
    if package and package.get("requires_discovery"):
        metadata["requires_discovery"] = True

    quote_id = _insert_quote_with_unique_key(
        conn,
        prospect_id=prospect_id,
        owner_username=_row_value(prospect, "owner_username"),
        market_state=_row_value(prospect, "market_state"),
        package_key=package_key,
        package_name=package_name,
        title=title,
        client_business_name=business_name,
        client_phone=prospect["phone"],
        website_url=prospect["website_url"],
        metadata=metadata,
        now=now,
    )

    if package:
        add_or_update_line_item(
            conn,
            quote_id,
            item_key=package_key,
            item_type="package",
            category="base_package",
            name=package_name or package_key or "Base Package",
            description=package.get("description"),
            unit_price_cents=int(package.get("default_price_cents") or 0),
            sort_order=0,
            metadata={
                "display_price": package.get("display_price"),
                "requires_custom_quote": bool(package.get("requires_custom_quote")),
                "requires_discovery": bool(package.get("requires_discovery")),
                "unit_label": "each",
                "salesman_notes": "",
            },
        )
    else:
        recalculate_quote_totals(conn, quote_id)

    log_quote_event(
        conn,
        quote_id,
        prospect_id,
        "quote_created",
        metadata={"package_key": package_key},
    )
    return get_quote(conn, quote_id) or {}


def get_quote(conn: sqlite3.Connection, quote_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        return None
    quote = _quote_from_row(row)
    quote["line_items"] = _load_line_items(conn, quote_id)
    quote["events"] = _load_quote_events(conn, quote_id)
    return quote


def get_quote_by_key(conn: sqlite3.Connection, quote_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id FROM quotes WHERE quote_key = ?", (quote_key,)).fetchone()
    if row is None:
        return None
    return get_quote(conn, int(row["id"]))


def list_quotes_for_prospect(conn: sqlite3.Connection, prospect_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM quotes
        WHERE prospect_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    return [_quote_from_row(row) for row in rows]


def list_quotes(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT q.*, p.business_name AS prospect_business_name,
               p.market AS prospect_market, p.niche AS prospect_niche
        FROM quotes q
        LEFT JOIN prospects p ON p.id = q.prospect_id
        ORDER BY q.updated_at DESC, q.id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 200), 1000)),),
    ).fetchall()
    quotes = []
    for row in rows:
        quote = _quote_from_row(row)
        quote["prospect_business_name"] = row["prospect_business_name"]
        quote["prospect_market"] = row["prospect_market"]
        quote["prospect_niche"] = row["prospect_niche"]
        quotes.append(quote)
    return quotes


def update_quote_header(
    conn: sqlite3.Connection,
    quote_id: int,
    *,
    package_key: str | None = None,
    package_name: str | None = None,
    title: str | None = None,
    client_business_name: str | None = None,
    client_contact_name: str | None = None,
    client_email: str | None = None,
    client_phone: str | None = None,
    website_url: str | None = None,
    term_months: int | None = None,
    deposit_percent: int | None = None,
    valid_until: str | None = None,
    client_visible_notes: str | None = None,
    assumptions: dict[str, Any] | None = None,
    internal_notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db.ensure_quote_schema(conn)
    quote = get_quote(conn, quote_id)
    if quote is None:
        raise ValueError(f"Quote {quote_id} does not exist.")
    now = db.utc_now()
    current_metadata = quote.get("metadata") if isinstance(quote.get("metadata"), dict) else {}
    if metadata:
        current_metadata.update(metadata)
    conn.execute(
        """
        UPDATE quotes
        SET package_key = ?,
            package_name = ?,
            title = ?,
            client_business_name = ?,
            client_contact_name = ?,
            client_email = ?,
            client_phone = ?,
            website_url = ?,
            term_months = ?,
            deposit_percent = ?,
            valid_until = ?,
            client_visible_notes = ?,
            assumptions_json = ?,
            internal_notes = ?,
            metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            package_key,
            package_name,
            title,
            client_business_name,
            client_contact_name,
            client_email,
            client_phone,
            website_url,
            int(term_months or 0),
            _bounded_percent(deposit_percent),
            valid_until,
            client_visible_notes,
            db.json_dumps(assumptions),
            internal_notes,
            json.dumps(current_metadata, sort_keys=True),
            now,
            quote_id,
        ),
    )
    return recalculate_quote_totals(conn, quote_id)


def replace_quote_line_items(
    conn: sqlite3.Connection,
    quote_id: int,
    line_items: list[dict[str, Any]],
) -> dict[str, Any]:
    db.ensure_quote_schema(conn)
    if conn.execute("SELECT id FROM quotes WHERE id = ?", (quote_id,)).fetchone() is None:
        raise ValueError(f"Quote {quote_id} does not exist.")
    conn.execute("DELETE FROM quote_line_items WHERE quote_id = ?", (quote_id,))
    for sort_order, item in enumerate(line_items):
        add_or_update_line_item(
            conn,
            quote_id,
            item_key=item.get("item_key"),
            item_type=item.get("item_type") or "custom",
            category=item.get("category"),
            name=str(item.get("name") or "").strip(),
            description=item.get("description"),
            quantity=item.get("quantity", 1),
            unit_price_cents=int(item.get("unit_price_cents") or 0),
            recurring_interval=item.get("recurring_interval"),
            is_optional=bool(item.get("is_optional")),
            is_included=bool(item.get("is_included")),
            sort_order=int(item.get("sort_order", sort_order)),
            metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        )
    return recalculate_quote_totals(conn, quote_id)


def update_quote_status(
    conn: sqlite3.Connection,
    quote_id: int,
    status: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    normalized = str(status or "").strip().lower()
    if normalized not in VALID_QUOTE_STATUSES:
        raise ValueError(f"Unsupported quote status: {status}")
    quote = get_quote(conn, quote_id)
    if quote is None:
        raise ValueError(f"Quote {quote_id} does not exist.")
    now = db.utc_now()
    sent_at = now if normalized == "sent" and not quote.get("sent_at") else quote.get("sent_at")
    accepted_at = now if normalized == "accepted" and not quote.get("accepted_at") else quote.get("accepted_at")
    declined_at = now if normalized == "declined" and not quote.get("declined_at") else quote.get("declined_at")
    conn.execute(
        """
        UPDATE quotes
        SET status = ?,
            sent_at = ?,
            accepted_at = ?,
            declined_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (normalized, sent_at, accepted_at, declined_at, now, quote_id),
    )
    event_type = {
        "sent": "quote_marked_sent",
        "accepted": "quote_accepted",
        "declined": "quote_declined",
    }.get(normalized, f"quote_status_{normalized}")
    log_quote_event(
        conn,
        quote_id,
        int(quote["prospect_id"]),
        event_type,
        status="recorded",
        note=note,
        metadata={"old_status": quote.get("status"), "new_status": normalized},
    )
    return get_quote(conn, quote_id) or {}


def create_quote_revision(conn: sqlite3.Connection, quote_id: int) -> dict[str, Any]:
    source = get_quote(conn, quote_id)
    if source is None:
        raise ValueError(f"Quote {quote_id} does not exist.")
    catalog = load_quote_catalog()
    new_quote = create_quote_for_prospect(
        conn,
        int(source["prospect_id"]),
        str(source.get("package_key") or "") or None,
    )
    version = int(source.get("version") or 1) + 1
    title = str(source.get("title") or "Quote").strip()
    if "revision" not in title.lower():
        title = f"{title} Revision"
    update_quote_header(
        conn,
        int(new_quote["id"]),
        package_key=source.get("package_key"),
        package_name=source.get("package_name"),
        title=title,
        client_business_name=source.get("client_business_name"),
        client_contact_name=source.get("client_contact_name"),
        client_email=source.get("client_email"),
        client_phone=source.get("client_phone"),
        website_url=source.get("website_url"),
        term_months=int(source.get("term_months") or 0),
        deposit_percent=int(source.get("deposit_percent") or 50),
        valid_until=source.get("valid_until"),
        client_visible_notes=source.get("client_visible_notes"),
        assumptions=source.get("assumptions") if isinstance(source.get("assumptions"), dict) else {},
        internal_notes=source.get("internal_notes"),
        metadata={
            "source_quote_id": source.get("id"),
            "source_quote_key": source.get("quote_key"),
            "revision_of": source.get("quote_key"),
            "package_requires_discovery": bool(
                (catalog.get("base_packages") or {})
                .get(str(source.get("package_key") or ""), {})
                .get("requires_discovery")
            ),
        },
    )
    conn.execute(
        """
        UPDATE quotes
        SET version = ?,
            supersedes_quote_id = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (version, source["id"], db.utc_now(), new_quote["id"]),
    )
    now = db.utc_now()
    conn.execute(
        """
        UPDATE quotes
        SET status = 'superseded',
            updated_at = ?
        WHERE id = ?
        """,
        (now, source["id"]),
    )
    replace_quote_line_items(
        conn,
        int(new_quote["id"]),
        [
            {
                "item_key": item.get("item_key"),
                "item_type": item.get("item_type"),
                "category": item.get("category"),
                "name": item.get("name"),
                "description": item.get("description"),
                "quantity": item.get("quantity"),
                "unit_price_cents": item.get("unit_price_cents"),
                "recurring_interval": item.get("recurring_interval"),
                "is_optional": item.get("is_optional"),
                "is_included": item.get("is_included"),
                "sort_order": item.get("sort_order"),
                "metadata": item.get("metadata"),
            }
            for item in source.get("line_items", [])
        ],
    )
    log_quote_event(
        conn,
        int(new_quote["id"]),
        int(source["prospect_id"]),
        "quote_revision_created",
        metadata={"supersedes_quote_id": source.get("id"), "version": version},
    )
    log_quote_event(
        conn,
        int(source["id"]),
        int(source["prospect_id"]),
        "quote_updated",
        note=f"Superseded by quote {new_quote.get('quote_key')}.",
        metadata={"new_quote_id": new_quote.get("id"), "new_quote_key": new_quote.get("quote_key")},
    )
    return get_quote(conn, int(new_quote["id"])) or {}


def delete_quote(
    conn: sqlite3.Connection,
    quote_id: int,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    db.ensure_quote_schema(conn)
    quote = get_quote(conn, quote_id)
    if quote is None:
        raise ValueError(f"Quote {quote_id} does not exist.")

    log_quote_event(
        conn,
        quote_id,
        int(quote["prospect_id"]),
        "quote_deleted",
        status="recorded",
        note=note or f"Deleted quote {quote.get('quote_key')}.",
        metadata={
            "quote_key": quote.get("quote_key"),
            "quote_status": quote.get("status"),
            "package_key": quote.get("package_key"),
            "package_name": quote.get("package_name"),
            "one_time_total_cents": quote.get("one_time_total_cents"),
            "recurring_monthly_total_cents": quote.get("recurring_monthly_total_cents"),
        },
    )
    conn.execute("DELETE FROM quote_line_items WHERE quote_id = ?", (quote_id,))
    conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
    return quote


def render_quote_text(quote: dict[str, Any], prospect: dict[str, Any] | None = None) -> str:
    business_name = quote.get("client_business_name") or (prospect or {}).get("business_name") or "your business"
    lines = [
        f"Quote: {quote.get('title') or 'Website Quote'}",
        f"For: {business_name}",
        "",
        "Recommended package:",
        f"{quote.get('package_name') or 'Custom scope'}",
        "",
        "Scope:",
    ]
    for item in quote.get("line_items", []):
        if item.get("is_optional") or item.get("is_included"):
            continue
        if item.get("item_type") == "discount":
            continue
        suffix = "/mo" if item.get("recurring_interval") == "monthly" else ""
        lines.append(f"- {item.get('name')}: {format_money(item.get('line_total_cents'))}{suffix}")
    if quote.get("one_time_discount_cents"):
        lines.extend(["", f"Discount: -{format_money(quote.get('one_time_discount_cents'))}"])
    lines.extend(
        [
            "",
            f"One-time total: {format_money(quote.get('one_time_total_cents'))}",
            f"Monthly recurring: {format_money(quote.get('recurring_monthly_total_cents'))}/mo",
            f"Deposit due: {format_money(quote.get('deposit_due_cents'))}",
            f"Balance due: {format_money(quote.get('balance_due_cents'))}",
        ]
    )
    if quote.get("client_visible_notes"):
        lines.extend(["", "Notes:", str(quote["client_visible_notes"]).strip()])
    assumptions = quote.get("assumptions") if isinstance(quote.get("assumptions"), dict) else {}
    assumption_lines = assumptions.get("items") if isinstance(assumptions.get("items"), list) else []
    if assumption_lines:
        lines.extend(["", "Assumptions:"])
        lines.extend(f"- {item}" for item in assumption_lines if str(item).strip())
    return "\n".join(lines).strip() + "\n"


def render_quote_print_html(quote: dict[str, Any], prospect: dict[str, Any] | None = None) -> str:
    body = render_quote_text(quote, prospect)
    escaped = escape(body)
    title = escape(str(quote.get("title") or "Printable Quote"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; line-height: 1.5; max-width: 760px; margin: 40px auto; color: #172033; }}
    pre {{ white-space: pre-wrap; font: inherit; }}
    @media print {{ body {{ margin: 0.5in; }} }}
  </style>
</head>
<body>
  <pre>{escaped}</pre>
</body>
</html>
"""


def add_or_update_line_item(
    conn: sqlite3.Connection,
    quote_id: int,
    *,
    line_item_id: int | None = None,
    item_key: str | None = None,
    item_type: str = "custom",
    category: str | None = None,
    name: str,
    description: str | None = None,
    quantity: Any = 1,
    unit_price_cents: int = 0,
    recurring_interval: str | None = None,
    is_optional: bool = False,
    is_included: bool = False,
    sort_order: int = 0,
    metadata: dict[str, Any] | None = None,
) -> int:
    db.ensure_quote_schema(conn)
    quote = conn.execute("SELECT id FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if quote is None:
        raise ValueError(f"Quote {quote_id} does not exist.")

    normalized_type = _validate_item_type(item_type)
    normalized_interval = _validate_recurring_interval(recurring_interval)
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Line item name is required.")

    clean_quantity = _decimal_quantity(quantity)
    clean_unit_price = int(unit_price_cents or 0)
    line_total = _calculate_line_total(
        item_type=normalized_type,
        quantity=clean_quantity,
        unit_price_cents=clean_unit_price,
        is_included=is_included,
    )
    now = db.utc_now()

    if line_item_id is not None:
        cursor = conn.execute(
            """
            UPDATE quote_line_items
            SET item_key = ?,
                item_type = ?,
                category = ?,
                name = ?,
                description = ?,
                quantity = ?,
                unit_price_cents = ?,
                line_total_cents = ?,
                recurring_interval = ?,
                is_optional = ?,
                is_included = ?,
                sort_order = ?,
                metadata_json = ?,
                updated_at = ?
            WHERE id = ?
              AND quote_id = ?
            """,
            (
                item_key,
                normalized_type,
                category,
                clean_name,
                description,
                float(clean_quantity),
                clean_unit_price,
                line_total,
                normalized_interval,
                int(bool(is_optional)),
                int(bool(is_included)),
                int(sort_order or 0),
                db.json_dumps(metadata),
                now,
                line_item_id,
                quote_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Line item {line_item_id} does not exist on quote {quote_id}.")
        item_id = int(line_item_id)
    else:
        cursor = conn.execute(
            """
            INSERT INTO quote_line_items (
                quote_id, item_key, item_type, category, name, description,
                quantity, unit_price_cents, line_total_cents, recurring_interval,
                is_optional, is_included, sort_order, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quote_id,
                item_key,
                normalized_type,
                category,
                clean_name,
                description,
                float(clean_quantity),
                clean_unit_price,
                line_total,
                normalized_interval,
                int(bool(is_optional)),
                int(bool(is_included)),
                int(sort_order or 0),
                db.json_dumps(metadata),
                now,
                now,
            ),
        )
        item_id = int(cursor.lastrowid)

    recalculate_quote_totals(conn, quote_id)
    return item_id


def recalculate_quote_totals(conn: sqlite3.Connection, quote_id: int) -> dict[str, Any]:
    db.ensure_quote_schema(conn)
    quote_row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if quote_row is None:
        raise ValueError(f"Quote {quote_id} does not exist.")

    rows = conn.execute(
        """
        SELECT *
        FROM quote_line_items
        WHERE quote_id = ?
        ORDER BY sort_order, id
        """,
        (quote_id,),
    ).fetchall()

    one_time_subtotal = 0
    one_time_discount = 0
    recurring_monthly_total = 0
    for row in rows:
        item = _line_item_from_row(row)
        if item["is_optional"] or item["is_included"]:
            continue
        total = int(item["line_total_cents"] or 0)
        if item["item_type"] == "discount":
            if item["recurring_interval"] == "monthly":
                recurring_monthly_total += total
            else:
                one_time_discount += abs(total)
            continue
        if item["recurring_interval"] == "monthly" or item["item_type"] == "recurring":
            recurring_monthly_total += total
        else:
            one_time_subtotal += total

    one_time_total = max(0, one_time_subtotal - one_time_discount)
    recurring_monthly_total = max(0, recurring_monthly_total)
    deposit_percent = _bounded_percent(quote_row["deposit_percent"])
    deposit_due = _percent_amount(one_time_total, deposit_percent)
    balance_due = one_time_total - deposit_due

    metadata = _json_loads(quote_row["metadata_json"], {})
    metadata = _apply_floor_warning(
        metadata,
        package_key=quote_row["package_key"],
        one_time_total=one_time_total,
        internal_notes=quote_row["internal_notes"],
    )
    now = db.utc_now()
    conn.execute(
        """
        UPDATE quotes
        SET one_time_subtotal_cents = ?,
            one_time_discount_cents = ?,
            one_time_total_cents = ?,
            recurring_monthly_total_cents = ?,
            deposit_percent = ?,
            deposit_due_cents = ?,
            balance_due_cents = ?,
            metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            one_time_subtotal,
            one_time_discount,
            one_time_total,
            recurring_monthly_total,
            deposit_percent,
            deposit_due,
            balance_due,
            json.dumps(metadata, sort_keys=True),
            now,
            quote_id,
        ),
    )
    return get_quote(conn, quote_id) or {}


def log_quote_event(
    conn: sqlite3.Connection,
    quote_id: int | None,
    prospect_id: int | None,
    event_type: str,
    status: str = "recorded",
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    db.ensure_quote_schema(conn)
    now = db.utc_now()
    cursor = conn.execute(
        """
        INSERT INTO quote_events (
            quote_id, prospect_id, event_type, status, note, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quote_id,
            prospect_id,
            event_type,
            status,
            note,
            db.json_dumps(metadata),
            now,
        ),
    )
    return int(cursor.lastrowid)


def _catalog_item(items: Any, key: str | None) -> dict[str, Any]:
    if not key:
        raise ValueError("Catalog key is required.")
    if not isinstance(items, dict) or key not in items:
        raise ValueError(f"Unknown quote catalog key: {key}")
    item = items[key]
    if not isinstance(item, dict):
        raise ValueError(f"Quote catalog item {key} must be a mapping.")
    return item


def _insert_quote_with_unique_key(
    conn: sqlite3.Connection,
    *,
    prospect_id: int,
    owner_username: str | None,
    market_state: str | None,
    package_key: str | None,
    package_name: str | None,
    title: str,
    client_business_name: str,
    client_phone: str | None,
    website_url: str | None,
    metadata: dict[str, Any],
    now: str,
) -> int:
    for _attempt in range(8):
        quote_key = f"quote-{prospect_id}-{secrets.token_hex(5)}"
        try:
            cursor = conn.execute(
                """
                INSERT INTO quotes (
                    quote_key, prospect_id, owner_username, market_state,
                    version, status, package_key, package_name,
                    title, client_business_name, client_phone, website_url,
                    assumptions_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'draft', ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
                """,
                (
                    quote_key,
                    prospect_id,
                    owner_username,
                    market_state,
                    package_key,
                    package_name,
                    title,
                    client_business_name,
                    client_phone,
                    website_url,
                    json.dumps(metadata, sort_keys=True),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique quote key.")


def _row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _load_line_items(conn: sqlite3.Connection, quote_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM quote_line_items
        WHERE quote_id = ?
        ORDER BY sort_order, id
        """,
        (quote_id,),
    ).fetchall()
    return [_line_item_from_row(row) for row in rows]


def _load_quote_events(conn: sqlite3.Connection, quote_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM quote_events
        WHERE quote_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (quote_id,),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def _quote_from_row(row: sqlite3.Row) -> dict[str, Any]:
    quote = db.row_to_dict(row)
    quote["assumptions"] = _json_loads(quote.get("assumptions_json"), {})
    quote["metadata"] = _json_loads(quote.get("metadata_json"), {})
    return quote


def _line_item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = db.row_to_dict(row)
    item["is_optional"] = bool(item.get("is_optional"))
    item["is_included"] = bool(item.get("is_included"))
    item["metadata"] = _json_loads(item.get("metadata_json"), {})
    return item


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    event = db.row_to_dict(row)
    event["metadata"] = _json_loads(event.get("metadata_json"), {})
    return event


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None or value == "":
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _validate_item_type(item_type: str | None) -> str:
    normalized = str(item_type or "custom").strip().lower()
    if normalized not in VALID_ITEM_TYPES:
        raise ValueError(f"Unsupported quote line item type: {item_type}")
    return normalized


def _validate_recurring_interval(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower() or None
    if normalized not in VALID_RECURRING_INTERVALS:
        raise ValueError(f"Unsupported recurring interval: {value}")
    return normalized


def _decimal_quantity(value: Any) -> Decimal:
    try:
        quantity = Decimal(str(value if value is not None else 1))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid quantity: {value!r}") from exc
    if quantity < 0:
        raise ValueError("Quantity cannot be negative.")
    return quantity


def _calculate_line_total(
    *,
    item_type: str,
    quantity: Decimal,
    unit_price_cents: int,
    is_included: bool,
) -> int:
    if is_included:
        return 0
    amount = (Decimal(unit_price_cents) * quantity).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    total = int(amount)
    if item_type == "discount":
        return -abs(total)
    return total


def _bounded_percent(value: Any) -> int:
    try:
        percent = int(value if value is not None else 50)
    except (TypeError, ValueError):
        percent = 50
    return min(100, max(0, percent))


def _percent_amount(amount_cents: int, percent: int) -> int:
    return int(
        (Decimal(amount_cents) * Decimal(percent) / Decimal(100)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _apply_floor_warning(
    metadata: dict[str, Any],
    *,
    package_key: Any,
    one_time_total: int,
    internal_notes: Any,
) -> dict[str, Any]:
    warnings = [
        str(warning)
        for warning in metadata.get("pricing_warnings", [])
        if "internal package floor" not in str(warning)
    ]
    catalog = load_quote_catalog()
    package = (catalog.get("base_packages") or {}).get(str(package_key or ""))
    floor = 0
    if isinstance(package, dict):
        floor = int(package.get("floor_price_cents") or 0)
    if floor and one_time_total < floor:
        if str(internal_notes or "").strip():
            metadata["pricing_override_acknowledged"] = True
        else:
            warnings.append(
                "Quote one-time total is below the internal package floor; add an internal override note before client use."
            )
            metadata["pricing_override_acknowledged"] = False
    if warnings:
        metadata["pricing_warnings"] = warnings
    else:
        metadata.pop("pricing_warnings", None)
    return metadata
