"""Local contract rendering and export helpers.

This module only generates local files from already-stored contract data. It
does not call DocuSign, send email, convert PDFs, or reach external services.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import contracts as contract_service
from . import db
from .config import PROJECT_ROOT


DOCX_TEMPLATE_MISSING_WARNING = "DOCX contract template missing."
PRIMARY_DOCX_TEMPLATE = PROJECT_ROOT / "contract_templates" / "service_contract_template.docx"
FALLBACK_DOCX_TEMPLATE = PROJECT_ROOT / "contract_templates" / "service_contract.docx"
HTML_TEMPLATE_NAME = "contracts/service_contract.html.j2"
PROVIDER_SIGNATURE_ANCHOR = "/tgs_provider_sign/"
PROVIDER_DATE_ANCHOR = "/tgs_provider_date/"
CLIENT_SIGNATURE_ANCHORS = (
    "/tgs_client1_sign/",
    "/tgs_client2_sign/",
    "/tgs_client3_sign/",
)
CLIENT_DATE_ANCHORS = (
    "/tgs_client1_date/",
    "/tgs_client2_date/",
    "/tgs_client3_date/",
)
FINAL_OR_SENT_STATUSES = {"sent", "completed"}
REDACTED_KEYS = {
    "audit",
    "email",
    "fallback_price",
    "floor_price",
    "internal_notes",
    "phone",
    "private_notes",
    "raw_score",
    "salesman_notes",
    "score",
    "client_email",
    "client_phone",
    "signer_email",
    "signer_phone",
}


def get_contract_output_dir(contract_key: str) -> Path:
    """Return the local output directory for a contract key."""

    safe_key = _safe_path_segment(contract_key or "contract")
    output_dir = PROJECT_ROOT / "runs" / "latest" / "contracts" / safe_key
    output_dir.resolve(strict=False).relative_to(PROJECT_ROOT.resolve(strict=False))
    return output_dir


def build_contract_render_context(
    conn: sqlite3.Connection,
    contract_id: int,
) -> dict[str, Any]:
    """Build the sanitized render context shared by HTML and DOCX exports."""

    contract = contract_service.load_contract(conn, contract_id)
    if contract is None:
        raise ValueError(f"Contract {contract_id} does not exist.")

    variables = contract_service.build_contract_variables(conn, contract_id)
    business = _mapping(variables.get("business"))
    signer_primary = _mapping(variables.get("signer_primary"))
    quote = _mapping(variables.get("quote"))
    scope = _mapping(variables.get("scope"))
    contract_vars = _mapping(variables.get("contract"))
    signers = _build_client_signers(signer_primary, variables.get("signers"))
    business.setdefault("billing_address", business.get("address_line") or "")
    scope.setdefault("add_ons", scope.get("optional_items") or [])
    quote.setdefault(
        "package_description",
        scope.get("client_visible_notes") or quote.get("title") or quote.get("package_name") or "",
    )
    if not contract_vars.get("duration"):
        term_months = _int_value(quote.get("term_months"))
        contract_vars["duration"] = f"{term_months} months" if term_months else ""
    anchor_map = build_anchor_map(signers)
    validation = validate_required_contract_variables(
        {
            "business": business,
            "signer_primary": signer_primary,
            "quote": quote,
            "scope": scope,
            "contract": contract_vars,
            "signers": signers,
        }
    )
    warnings = list(validation.get("warnings", []))
    docx_template, template_warning = _resolve_docx_template()
    if template_warning:
        warnings.append(template_warning)

    signature_blocks = _signature_blocks(signers, anchor_map)
    hidden_anchor_strings = _hidden_anchor_strings(anchor_map, signature_blocks)
    context = {
        "contract_record": contract,
        "contract_id": contract_id,
        "contract_key": contract.get("contract_key"),
        "business": business,
        "signer_primary": signer_primary,
        "signer_2": _signer_alias(signers, 1),
        "signer_3": _signer_alias(signers, 2),
        "signers": signers,
        "quote": quote,
        "scope": scope,
        "contract": contract_vars,
        "anchor_map": anchor_map,
        "signature_blocks": signature_blocks,
        "hidden_anchor_strings": hidden_anchor_strings,
        "validation": validation,
        "warnings": _dedupe(warnings),
        "docx_template_path": str(docx_template) if docx_template else "",
        "docx_template_warning": template_warning,
        "provider": {
            "name": "Trade Growth Studio",
            "signature_anchor": PROVIDER_SIGNATURE_ANCHOR,
            "date_anchor": PROVIDER_DATE_ANCHOR,
        },
    }
    return context


def render_contract_html(conn: sqlite3.Connection, contract_id: int) -> str:
    """Render a standalone, client-facing HTML contract preview."""

    context = build_contract_render_context(conn, contract_id)
    return _render_html_from_context(context)


def render_contract_docx(
    conn: sqlite3.Connection,
    contract_id: int,
) -> dict[str, Any]:
    """Render a DOCX contract if a DOCX template is available."""

    context = build_contract_render_context(conn, contract_id)
    output_dir = get_contract_output_dir(str(context["contract_key"] or contract_id))
    return _render_docx_from_context(context, output_dir, conn=conn)


def generate_contract_artifacts(
    conn: sqlite3.Connection,
    contract_id: int,
) -> dict[str, Any]:
    """Generate local HTML/JSON artifacts and DOCX when a template exists."""

    context = build_contract_render_context(conn, contract_id)
    contract_key = str(context["contract_key"] or f"contract-{contract_id}")
    output_dir = get_contract_output_dir(contract_key)
    output_dir.mkdir(parents=True, exist_ok=True)

    html = _render_html_from_context(context)
    html_file = _write_text_artifact(
        conn,
        context,
        output_dir / "contract.html",
        html,
        artifact_type="contract_html_preview",
        artifact_suffix="html",
        content_type="text/html",
    )
    variables_file = write_redacted_contract_variables(context, output_dir)
    _upsert_contract_artifact(
        conn,
        context,
        variables_file["relative_path"],
        artifact_type="contract_variables_redacted",
        artifact_suffix="variables",
        content_type=variables_file["content_type"],
        content_hash=variables_file["content_hash"],
    )
    anchor_file = _write_anchor_map(context, output_dir)
    _upsert_contract_artifact(
        conn,
        context,
        anchor_file["relative_path"],
        artifact_type="contract_docusign_anchor_map",
        artifact_suffix="anchors",
        content_type=anchor_file["content_type"],
        content_hash=anchor_file["content_hash"],
    )
    docx_file = _render_docx_from_context(context, output_dir, conn=conn)

    docx_path = docx_file.get("relative_path") if docx_file.get("generated") else None
    contract_service.update_contract_generated_paths(
        conn,
        contract_id,
        docx_path=docx_path,
        html_path=html_file["relative_path"],
    )
    status = str(context["contract_record"].get("status") or "draft").lower()
    if status not in FINAL_OR_SENT_STATUSES:
        contract_service.update_contract_status(
            conn,
            contract_id,
            "generated",
            note="Generated local contract artifacts.",
            metadata={
                "html_path": html_file["relative_path"],
                "docx_path": docx_path,
                "warnings": context["warnings"],
            },
        )
    contract_service.log_contract_event(
        conn,
        contract_id,
        event_type="contract_generated",
        status="generated",
        note="Generated local contract artifacts.",
        metadata={
            "html_path": html_file["relative_path"],
            "docx_path": docx_path,
            "variables_path": variables_file["relative_path"],
            "anchor_map_path": anchor_file["relative_path"],
            "warnings": context["warnings"],
        },
    )

    return {
        "contract_id": contract_id,
        "contract_key": contract_key,
        "output_dir": str(output_dir),
        "relative_output_dir": _relative_path(output_dir),
        "html": html_file,
        "docx": docx_file,
        "variables": variables_file,
        "anchor_map": anchor_file,
        "warnings": context["warnings"],
        "validation": context["validation"],
    }


def write_redacted_contract_variables(
    context: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Write a redacted render-context JSON file for local debugging/review."""

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = _redact_context(context)
    path = output_dir / "contract_variables_redacted.json"
    content = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(content + "\n", encoding="utf-8")
    return {
        "absolute_path": str(path),
        "relative_path": _relative_path(path),
        "content_type": "application/json",
        "content_hash": _stable_hash(content),
    }


def build_anchor_map(signers: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Build canonical DocuSign anchor metadata without creating an envelope."""

    configured_signers = [dict(signer) for signer in (signers or []) if isinstance(signer, Mapping)]
    clients = []
    for index in range(3):
        signer = configured_signers[index] if index < len(configured_signers) else {}
        clients.append(
            {
                "index": index + 1,
                "role": signer.get("role") or f"client_{index + 1}",
                "name": signer.get("name") or "",
                "title": signer.get("title") or "",
                "configured": bool(signer.get("name") or signer.get("email")),
                "required": bool(signer.get("required", True)) if signer else False,
                "routing_order": int(signer.get("routing_order") or index + 1) if signer else index + 1,
                "signature_anchor": CLIENT_SIGNATURE_ANCHORS[index],
                "date_anchor": CLIENT_DATE_ANCHORS[index],
            }
        )
    all_anchors = [
        PROVIDER_SIGNATURE_ANCHOR,
        PROVIDER_DATE_ANCHOR,
        *CLIENT_SIGNATURE_ANCHORS,
        *CLIENT_DATE_ANCHORS,
    ]
    return {
        "provider": {
            "role": "provider",
            "name": "Trade Growth Studio",
            "configured": True,
            "required": True,
            "routing_order": 99,
            "signature_anchor": PROVIDER_SIGNATURE_ANCHOR,
            "date_anchor": PROVIDER_DATE_ANCHOR,
        },
        "clients": clients,
        "overflow_signer_count": max(0, len(configured_signers) - 3),
        "all_anchors": all_anchors,
        "note": (
            "Only create DocuSign tabs for configured signers. "
            "Unused anchors may remain in templates until hidden or removed."
        ),
    }


def validate_required_contract_variables(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return non-blocking validation results for manually confirmed fields."""

    required_paths = (
        ("business.legal_name", ("business", "legal_name")),
        ("business.entity_type", ("business", "entity_type")),
        ("signer_primary.name", ("signer_primary", "name")),
        ("signer_primary.title", ("signer_primary", "title")),
        ("signer_primary.email", ("signer_primary", "email")),
        ("contract.effective_date", ("contract", "effective_date")),
        ("contract.start_date", ("contract", "start_date")),
    )
    missing = [
        label
        for label, path in required_paths
        if not _clean(_get_nested(context, path))
    ]
    warnings = []
    if missing:
        warnings.append(
            "Manual confirmation needed before sending: " + ", ".join(missing)
        )
    signers = context.get("signers")
    if isinstance(signers, Sequence) and not isinstance(signers, (str, bytes)) and len(signers) > 3:
        warnings.append("More than 3 client signers configured; only first 3 anchor pairs are mapped.")
    return {"ok": not missing, "missing": missing, "warnings": warnings}


def _render_html_from_context(context: Mapping[str, Any]) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(PROJECT_ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template(HTML_TEMPLATE_NAME)
    return template.render(**context).strip() + "\n"


def _render_docx_from_context(
    context: Mapping[str, Any],
    output_dir: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    template_path, warning = _resolve_docx_template()
    if template_path is None:
        return {
            "generated": False,
            "warning": warning or DOCX_TEMPLATE_MISSING_WARNING,
            "template_path": "",
        }

    try:
        from docxtpl import DocxTemplate
    except ImportError:
        return {
            "generated": False,
            "warning": "DOCX templating dependency missing: docxtpl is not installed.",
            "template_path": _relative_path(template_path),
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "contract.docx"
    document = DocxTemplate(str(template_path))
    document.render(_docx_context(context))
    document.save(str(output_path))
    content = output_path.read_bytes()
    relative_path = _relative_path(output_path)
    _upsert_contract_artifact(
        conn,
        context,
        relative_path,
        artifact_type="contract_docx",
        artifact_suffix="docx",
        content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        content_hash=_stable_hash_bytes(content),
    )
    return {
        "generated": True,
        "absolute_path": str(output_path),
        "relative_path": relative_path,
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "content_hash": _stable_hash_bytes(content),
        "template_path": _relative_path(template_path),
        "warning": warning,
    }


def _docx_context(context: Mapping[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "business",
        "signer_primary",
        "signer_2",
        "signer_3",
        "signers",
        "quote",
        "scope",
        "contract",
        "anchor_map",
        "signature_blocks",
        "provider",
        "warnings",
        "validation",
    }
    output = {key: deepcopy(context.get(key)) for key in allowed_keys}
    scope = _mapping(output.get("scope"))
    for key in ("included_items", "add_ons", "optional_items", "recurring_items", "assumptions"):
        if key in scope:
            scope[key] = _docx_list_text(scope.get(key))
    output["scope"] = scope
    contract = _mapping(output.get("contract"))
    if "additional_sections" in contract:
        contract["additional_sections"] = _docx_list_text(contract.get("additional_sections"))
    output["contract"] = contract
    return output


def _signer_alias(signers: Sequence[Mapping[str, Any]], index: int) -> dict[str, Any]:
    if index < len(signers) and isinstance(signers[index], Mapping):
        signer = dict(signers[index])
    else:
        signer = {}
    return {
        "name": _clean(signer.get("name")),
        "title": _clean(signer.get("title")),
        "email": _clean(signer.get("email")),
        "phone": _clean(signer.get("phone")),
    }


def _docx_list_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return _clean(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return _clean(value)
    lines = []
    for item in value:
        if isinstance(item, Mapping):
            title = _clean(item.get("title") or item.get("name") or item.get("category"))
            description = _clean(item.get("body") or item.get("description"))
            amount = _clean(item.get("line_total") or item.get("unit_price"))
            parts = [part for part in (title, description) if part]
            text = " - ".join(parts)
            if amount:
                text = f"{text} ({amount})" if text else amount
        else:
            text = _clean(item)
        if text:
            lines.append(text)
    return "; ".join(lines)


def _write_text_artifact(
    conn: sqlite3.Connection,
    context: Mapping[str, Any],
    path: Path,
    content: str,
    *,
    artifact_type: str,
    artifact_suffix: str,
    content_type: str,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    content_hash = _stable_hash(content)
    relative_path = _relative_path(path)
    _upsert_contract_artifact(
        conn,
        context,
        relative_path,
        artifact_type=artifact_type,
        artifact_suffix=artifact_suffix,
        content_type=content_type,
        content_hash=content_hash,
    )
    return {
        "absolute_path": str(path),
        "relative_path": relative_path,
        "content_type": content_type,
        "content_hash": content_hash,
    }


def _write_anchor_map(
    context: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    path = output_dir / "docusign_anchor_map.json"
    content = json.dumps(context.get("anchor_map") or {}, indent=2, sort_keys=True)
    path.write_text(content + "\n", encoding="utf-8")
    return {
        "absolute_path": str(path),
        "relative_path": _relative_path(path),
        "content_type": "application/json",
        "content_hash": _stable_hash(content),
    }


def _upsert_contract_artifact(
    conn: sqlite3.Connection | None,
    context: Mapping[str, Any],
    relative_path: str,
    *,
    artifact_type: str,
    artifact_suffix: str,
    content_type: str,
    content_hash: str,
) -> None:
    if conn is None:
        return
    contract = _mapping(context.get("contract_record"))
    contract_key = str(context.get("contract_key") or contract.get("contract_key") or "contract")
    db.upsert_artifact(
        conn,
        artifact_key=f"{contract_key}:{artifact_suffix}",
        artifact_type=artifact_type,
        prospect_id=int(contract.get("prospect_id")) if contract.get("prospect_id") is not None else None,
        path=relative_path,
        content_hash=content_hash,
        status="ready",
        metadata={
            "contract_id": context.get("contract_id"),
            "contract_key": contract_key,
            "contract_status": contract.get("status"),
            "quote_id": contract.get("quote_id"),
            "content_type": content_type,
        },
    )


def _resolve_docx_template() -> tuple[Path | None, str | None]:
    if PRIMARY_DOCX_TEMPLATE.exists():
        return PRIMARY_DOCX_TEMPLATE, None
    if FALLBACK_DOCX_TEMPLATE.exists():
        return FALLBACK_DOCX_TEMPLATE, (
            "Using fallback DOCX source contract_templates/service_contract.docx."
        )
    return None, DOCX_TEMPLATE_MISSING_WARNING


def _build_client_signers(
    signer_primary: Mapping[str, Any],
    raw_signers: Any,
) -> list[dict[str, Any]]:
    signers = []
    if isinstance(raw_signers, Sequence) and not isinstance(raw_signers, (str, bytes)):
        for signer in raw_signers:
            if not isinstance(signer, Mapping):
                continue
            clean = {
                "role": _clean(signer.get("role")) or "client",
                "name": _clean(signer.get("name")),
                "title": _clean(signer.get("title")),
                "email": _clean(signer.get("email")),
                "phone": _clean(signer.get("phone")),
                "required": bool(signer.get("required", True)),
                "routing_order": _int_value(signer.get("routing_order"), len(signers) + 1),
            }
            if any(clean.get(key) for key in ("name", "email", "title", "phone")):
                signers.append(clean)
    if signers:
        return signers

    primary = {
        "role": "client",
        "name": _clean(signer_primary.get("name")),
        "title": _clean(signer_primary.get("title")),
        "email": _clean(signer_primary.get("email")),
        "phone": _clean(signer_primary.get("phone")),
        "required": True,
        "routing_order": 1,
    }
    if any(primary.get(key) for key in ("name", "email", "title", "phone")):
        return [primary]
    return []


def _signature_blocks(
    signers: Sequence[Mapping[str, Any]],
    anchor_map: Mapping[str, Any],
) -> list[dict[str, Any]]:
    clients = anchor_map.get("clients") if isinstance(anchor_map.get("clients"), list) else []
    blocks = []
    for index, signer in enumerate(signers[:3]):
        anchor = clients[index] if index < len(clients) and isinstance(clients[index], Mapping) else {}
        blocks.append(
            {
                "role": signer.get("role") or f"client_{index + 1}",
                "name": signer.get("name") or "Authorized signer",
                "title": signer.get("title") or "",
                "signature_anchor": anchor.get("signature_anchor") or CLIENT_SIGNATURE_ANCHORS[index],
                "date_anchor": anchor.get("date_anchor") or CLIENT_DATE_ANCHORS[index],
            }
        )
    if not blocks:
        blocks.append(
            {
                "role": "client",
                "name": "Authorized signer to be confirmed",
                "title": "",
                "signature_anchor": CLIENT_SIGNATURE_ANCHORS[0],
                "date_anchor": CLIENT_DATE_ANCHORS[0],
            }
        )
    return blocks


def _hidden_anchor_strings(
    anchor_map: Mapping[str, Any],
    visible_blocks: Sequence[Mapping[str, Any]],
) -> list[str]:
    visible = {
        str(block.get("signature_anchor") or "")
        for block in visible_blocks
    } | {
        str(block.get("date_anchor") or "")
        for block in visible_blocks
    } | {PROVIDER_SIGNATURE_ANCHOR, PROVIDER_DATE_ANCHOR}
    anchors = anchor_map.get("all_anchors") if isinstance(anchor_map.get("all_anchors"), list) else []
    return [anchor for anchor in anchors if anchor and anchor not in visible]


def _redact_context(context: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "contract_key": context.get("contract_key"),
        "business": context.get("business"),
        "signer_primary": context.get("signer_primary"),
        "signers": context.get("signers"),
        "quote": context.get("quote"),
        "scope": context.get("scope"),
        "contract": context.get("contract"),
        "anchor_map": context.get("anchor_map"),
        "validation": context.get("validation"),
        "warnings": context.get("warnings"),
    }
    return _redact_value(payload)


def _redact_value(value: Any, key: str = "") -> Any:
    lowered_key = key.lower()
    if any(token in lowered_key for token in REDACTED_KEYS):
        return "[redacted]" if value else ""
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, key) for item in value]
    return value


def _relative_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    root = PROJECT_ROOT.resolve(strict=False)
    resolved.relative_to(root)
    return resolved.relative_to(root).as_posix()


def _safe_path_segment(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return text or "contract"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _get_nested(mapping: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _clean(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        clean = _clean(item)
        key = clean.lower()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result
