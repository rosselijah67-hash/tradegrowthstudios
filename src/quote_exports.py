"""Client-facing quote export helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import PROJECT_ROOT
from .quotes import format_money


DEFAULT_SENDER_NAME = "Trade Growth Studio"
DEFAULT_ASSUMPTIONS = (
    "Client verifies all business claims before launch, including licensing, insurance, warranties, financing, emergency availability, and service-area details.",
    "Client provides photos/video or approves licensed media as an add-on.",
    "Third-party costs such as hosting, call tracking, scheduling tools, ad platforms, and licensed media are separate unless listed.",
    "Quote does not include paid ad management unless listed as a line item.",
    "SEO setup improves site structure and readiness but does not guarantee rankings, traffic, or revenue.",
)


def build_export_context(
    quote: dict[str, Any],
    prospect: dict[str, Any] | None = None,
    *,
    sender_name: str = DEFAULT_SENDER_NAME,
) -> dict[str, Any]:
    """Build a sanitized, client-facing export context."""

    business_name = _clean(
        quote.get("client_business_name")
        or (prospect or {}).get("business_name")
        or "your business"
    )
    contact_name = _clean(quote.get("client_contact_name") or business_name)
    quote_date = _date_part(quote.get("created_at")) or datetime.now(timezone.utc).date().isoformat()
    valid_until = _clean(quote.get("valid_until") or "")
    visible_items = [item for item in quote.get("line_items", []) if _is_client_visible(item)]
    one_time_items = [
        _line_item_view(item)
        for item in visible_items
        if item.get("item_type") != "discount"
        and item.get("item_type") != "recurring"
        and item.get("recurring_interval") != "monthly"
    ]
    recurring_items = [
        _line_item_view(item)
        for item in visible_items
        if item.get("item_type") == "recurring" or item.get("recurring_interval") == "monthly"
    ]
    discount_items = [
        _line_item_view(item)
        for item in visible_items
        if item.get("item_type") == "discount"
    ]
    custom_assumptions = _quote_assumptions(quote)
    assumptions = _dedupe([*custom_assumptions, *DEFAULT_ASSUMPTIONS])
    if valid_until:
        assumptions.append(f"Quote is valid until {valid_until}.")
    else:
        assumptions.append("Quote validity date to be confirmed before approval.")

    return {
        "quote": quote,
        "quote_key": _clean(quote.get("quote_key") or f"quote-{quote.get('id') or ''}"),
        "quote_date": quote_date,
        "valid_until": valid_until,
        "status": _status_label(quote.get("status")),
        "business_name": business_name,
        "contact_name": contact_name,
        "client_email": _clean(quote.get("client_email") or ""),
        "client_phone": _clean(quote.get("client_phone") or ""),
        "website_url": _clean(quote.get("website_url") or (prospect or {}).get("website_url") or ""),
        "package_name": _clean(quote.get("package_name") or "Custom scope"),
        "scope_summary": _clean(quote.get("title") or quote.get("package_name") or "Website replacement scope"),
        "one_time_items": one_time_items,
        "recurring_items": recurring_items,
        "discount_items": discount_items,
        "one_time_total": format_money(quote.get("one_time_total_cents")),
        "one_time_subtotal": format_money(quote.get("one_time_subtotal_cents")),
        "discount_total": format_money(quote.get("one_time_discount_cents")),
        "recurring_total": format_money(quote.get("recurring_monthly_total_cents")),
        "deposit_due": format_money(quote.get("deposit_due_cents")),
        "balance_due": format_money(quote.get("balance_due_cents")),
        "deposit_percent": int(quote.get("deposit_percent") or 0),
        "client_visible_notes": _clean(quote.get("client_visible_notes") or ""),
        "assumptions": assumptions,
        "sender_name": _clean(sender_name or DEFAULT_SENDER_NAME),
    }


def render_email_text(
    quote: dict[str, Any],
    prospect: dict[str, Any] | None = None,
    *,
    sender_name: str = DEFAULT_SENDER_NAME,
) -> str:
    context = build_export_context(quote, prospect, sender_name=sender_name)
    one_time_lines = _format_items_for_text(context["one_time_items"], include_optional=True)
    discount_lines = _format_items_for_text(context["discount_items"], include_optional=True)
    recurring_lines = _format_items_for_text(
        context["recurring_items"],
        include_optional=True,
        monthly=True,
    )
    if discount_lines:
        one_time_lines = [*one_time_lines, *discount_lines]
    if not one_time_lines:
        one_time_lines = ["Scope to be confirmed."]
    if not recurring_lines:
        recurring_lines = ["None selected"]

    notes = context["client_visible_notes"] or "No additional client-facing notes."
    lines = [
        f"Subject: Website replacement quote for {context['business_name']}",
        "",
        f"{context['contact_name']},",
        "",
        "Here is the proposed scope and pricing for the website replacement work we discussed.",
        "Optional add-ons are marked clearly and are not included in the project total unless you approve them.",
        "",
        "Package:",
        context["package_name"],
        "",
        "One-time website work:",
        *one_time_lines,
        "",
        "Project total:",
        context["one_time_total"],
        "",
        "Deposit due to start work:",
        context["deposit_due"],
        "",
        "Balance due before launch:",
        context["balance_due"],
        "",
        "Monthly ongoing work:",
        *recurring_lines,
        "",
        "Notes:",
        notes,
        "",
        "Assumptions:",
        *[f"- {item}" for item in context["assumptions"]],
        "",
        "If this scope looks right, the next step is approval and deposit instructions.",
        "",
        context["sender_name"],
    ]
    return "\n".join(lines).strip() + "\n"


def write_text_export(
    connection: Any,
    quote: dict[str, Any],
    text: str,
) -> dict[str, str]:
    relative_path = _quote_export_relative_path(quote, "quote.txt")
    return _write_export(
        connection,
        quote,
        relative_path=relative_path,
        content=text,
        artifact_type="quote_email_text",
        artifact_suffix="text",
        content_type="text/plain",
    )


def write_html_export(
    connection: Any,
    quote: dict[str, Any],
    html: str,
) -> dict[str, str]:
    relative_path = _quote_export_relative_path(quote, "quote.html")
    return _write_export(
        connection,
        quote,
        relative_path=relative_path,
        content=html,
        artifact_type="quote_print_html",
        artifact_suffix="html",
        content_type="text/html",
    )


def log_export_event(
    connection: Any,
    quote: dict[str, Any],
    *,
    event_type: str,
    export_file: dict[str, str],
) -> int:
    metadata = {
        "quote_key": quote.get("quote_key"),
        "path": export_file.get("relative_path"),
        "content_type": export_file.get("content_type"),
    }
    db.ensure_quote_schema(connection)
    return _insert_quote_event(
        connection,
        quote,
        event_type=event_type,
        metadata=metadata,
    )


def _insert_quote_event(
    connection: Any,
    quote: dict[str, Any],
    *,
    event_type: str,
    metadata: dict[str, Any],
) -> int:
    from . import quotes

    return quotes.log_quote_event(
        connection,
        int(quote["id"]),
        int(quote["prospect_id"]),
        event_type,
        note=f"Generated {metadata.get('content_type')} quote export.",
        metadata=metadata,
    )


def _format_items_for_text(
    items: list[dict[str, Any]],
    *,
    include_optional: bool,
    monthly: bool = False,
) -> list[str]:
    lines = []
    for item in items:
        if not include_optional and item.get("is_optional"):
            continue
        amount = item["line_total"]
        suffix = "/mo" if monthly else ""
        quantity = item.get("quantity") or "1"
        unit_label = item.get("unit_label") or "each"
        description = f" - {item['description']}" if item.get("description") else ""
        if item.get("is_optional"):
            lines.append(
                f"- OPTIONAL ADD-ON - NOT INCLUDED IN PROJECT TOTAL UNLESS APPROVED: "
                f"{item['name']}{description} ({quantity} {unit_label}): {amount}{suffix}"
            )
        else:
            lines.append(f"- {item['name']}{description} ({quantity} {unit_label}): {amount}{suffix}")
        if item.get("salesman_notes"):
            lines.append(f"  Note: {item['salesman_notes']}")
    return lines


def _line_item_view(item: dict[str, Any]) -> dict[str, Any]:
    quantity = item.get("quantity")
    quantity_text = _quantity_label(quantity)
    name = _clean(item.get("name") or "Line item")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    total_cents = int(item.get("line_total_cents") or 0)
    if item.get("item_type") == "discount":
        total_cents = -abs(total_cents)
    return {
        "name": name,
        "description": _clean(item.get("description") or ""),
        "quantity": quantity_text,
        "unit_label": _clean(metadata.get("unit_label") or "each"),
        "unit_price": format_money(item.get("unit_price_cents")),
        "line_total": format_money(total_cents),
        "salesman_notes": _clean(metadata.get("salesman_notes") or ""),
        "is_optional": bool(item.get("is_optional")),
        "is_discount": item.get("item_type") == "discount",
        "is_recurring": item.get("item_type") == "recurring" or item.get("recurring_interval") == "monthly",
    }


def _is_client_visible(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return metadata.get("client_visible") is not False


def _quote_assumptions(quote: dict[str, Any]) -> list[str]:
    assumptions = quote.get("assumptions") if isinstance(quote.get("assumptions"), dict) else {}
    items = assumptions.get("items") if isinstance(assumptions.get("items"), list) else []
    return [_clean(item) for item in items if _clean(item)]


def _quote_export_relative_path(quote: dict[str, Any], filename: str) -> str:
    quote_key = _safe_path_segment(quote.get("quote_key") or f"quote-{quote.get('id') or 'unknown'}")
    return f"runs/latest/quotes/{quote_key}/{filename}"


def _write_export(
    connection: Any,
    quote: dict[str, Any],
    *,
    relative_path: str,
    content: str,
    artifact_type: str,
    artifact_suffix: str,
    content_type: str,
) -> dict[str, str]:
    absolute_path = (PROJECT_ROOT / relative_path).resolve(strict=False)
    absolute_path.relative_to(PROJECT_ROOT.resolve(strict=False))
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(content, encoding="utf-8")
    db.upsert_artifact(
        connection,
        artifact_key=f"{quote.get('quote_key')}:{artifact_suffix}",
        artifact_type=artifact_type,
        prospect_id=int(quote["prospect_id"]),
        path=relative_path,
        content_hash=_stable_hash(content),
        status="ready",
        metadata={
            "quote_id": quote.get("id"),
            "quote_key": quote.get("quote_key"),
            "quote_status": quote.get("status"),
            "content_type": content_type,
        },
    )
    return {
        "relative_path": relative_path,
        "absolute_path": str(absolute_path),
        "content_type": content_type,
    }


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _quantity_label(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "1"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _date_part(value: Any) -> str:
    text = _clean(value)
    return text[:10] if len(text) >= 10 else ""


def _status_label(value: Any) -> str:
    text = _clean(value or "draft").replace("_", " ")
    return text.title()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        clean = _clean(item)
        key = clean.lower()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result


def _safe_path_segment(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return text or "quote"


def _clean(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
