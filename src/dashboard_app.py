"""Local Flask dashboard for reviewing and triaging SQLite lead data."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import smtplib
import subprocess
import sqlite3
import ssl
import sys
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from flask import Flask, abort, current_app, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash

from . import dashboard_jobs, state
from .config import (
    PROJECT_ROOT,
    _load_simple_yaml,
    get_database_path,
    load_env,
    load_yaml_config,
    project_path,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
PIPELINE_DEFAULT_LIMIT = 20
PIPELINE_EXTERNAL_LIMIT = 100
AUDIT_JOB_LIMIT = 50
PLACES_JOB_LIMIT = 100
PIPELINE_LOG_PATH = "runs/latest/dashboard_job_log.txt"
PIPELINE_JOB_TIMEOUT_SECONDS = 600
OUTREACH_DRAFT_TIMEOUT_SECONDS = 120
MARKETS_CONFIG_PATH = "config/markets.yaml"
UNKNOWN_MARKET_VALUE = "__unknown__"
MARKET_KEY_PATTERN = re.compile(r"^[a-z0-9_]+$")
OUTBOUND_DEFAULT_CAMPAIGN = "intro_email"
OUTBOUND_DEFAULT_STEP = 1
OUTBOUND_DEFAULT_QUEUE_LIMIT = 10
OUTBOUND_MAX_QUEUE_LIMIT = 100
OUTREACH_DRAFT_STEPS = (1, 2, 3, 4)
OUTREACH_COPY_STYLES = {"owner_friendly", "direct", "technical"}
OUTREACH_REGENERATE_STEPS = {"1", "all"}
SEND_DEFAULT_LIMIT = 5
SEND_MAX_LIMIT = 10
SEND_DEFAULT_DAILY_CAP = 10
SEND_TEST_LOG_JSON_PATH = "runs/latest/dashboard_send_test.json"
SEND_TEST_LOG_TEXT_PATH = "runs/latest/dashboard_send_test.txt"
SEND_MAX_ATTACHMENT_BYTES = 1_500_000
INBOX_SYNC_JSON_PATH = "runs/latest/inbox_sync.json"
OUTBOUND_APPROVED_SQL = (
    "UPPER(COALESCE(human_review_decision, '')) = 'APPROVED' "
    "AND UPPER(COALESCE(status, '')) IN ('APPROVED_FOR_OUTREACH', 'OUTREACH_DRAFTED')"
)
OUTREACH_BANNED_COPY_PHRASES = (
    "[your name]",
    "case file",
    "audit notes",
    "audit recorded",
    "our system detected",
    "guaranteed",
    "rank you",
    "10x",
    "ai website",
)
OUTREACH_INTERNAL_JARGON_PHRASES = (
    "lead-score",
    "metadata",
    "artifact",
    "our system",
    "detected",
    "conversion issue",
)
PUBLIC_PACKET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
PUBLIC_AUTH_ENDPOINTS = {
    "login",
    "health",
    "static",
    "public_packet_page",
    "public_packet_asset",
}

PIPELINE_JOBS = {
    "eligibility": {
        "label": "Run pre-audit eligibility",
        "module": "src.eligibility",
        "external": False,
        "description": "Scores discovered prospects for audit eligibility using local data.",
    },
    "audit": {
        "label": "Run website audit for qualified prospects",
        "module": "src.audit_site",
        "external": True,
        "description": "Audits qualified websites and can capture screenshots/PageSpeed data.",
    },
    "score": {
        "label": "Run lead scoring",
        "module": "src.score_leads",
        "external": False,
        "description": "Scores audited leads from existing local audit data.",
    },
    "artifacts": {
        "label": "Generate artifacts",
        "module": "src.generate_artifacts",
        "external": False,
        "description": "Generates local review and sales artifacts from scored leads.",
    },
}

PIPELINE_COUNT_BUCKETS = [
    "DISCOVERED",
    "QUALIFIED",
    "DISQUALIFIED",
    "ELIGIBLE_FOR_AUDIT",
    "AUDITED",
    "READY",
    "PENDING_REVIEW",
    "APPROVED_FOR_OUTREACH",
]

PIPELINE_JOB_LOCK = threading.Lock()

PIPELINE_STAGE_BUCKETS = list(state.PIPELINE_STAGE_BUCKETS)

CRM_STAGES = list(state.CRM_STAGES)

CRM_STAGE_LABELS = dict(CRM_STAGES)

CRM_NEXT_ACTIONS = dict(state.CRM_NEXT_ACTIONS)

SALES_PACKET_STAGES = {"CONTACT_MADE", "CALL_BOOKED", "PROPOSAL_SENT", "CLOSED_WON"}

HIGH_TICKET_NICHE_KEYWORDS = {
    "roof",
    "hvac",
    "plumb",
    "electric",
    "foundation",
    "remodel",
    "restoration",
    "water damage",
    "concrete",
    "landscap",
    "tree",
    "pool",
    "fence",
    "garage door",
    "floor",
}

LEADS_COLUMNS = [
    "id",
    "business_name",
    "market",
    "niche",
    "website_url",
    "phone",
    "rating",
    "user_rating_count",
    "qualification_status",
    "audit_data_status",
    "human_review_status",
    "human_review_decision",
    "expected_close_score",
    "website_pain_score",
    "status",
    "next_action",
    "metadata_json",
]

LIFECYCLE_STAGE_ALIASES = {
    "ELIGIBLE_FOR_AUDIT": "ELIGIBLE_FOR_AUDIT",
    "INELIGIBLE": "INELIGIBLE",
    "AUDIT_READY": "AUDIT_READY",
    "PENDING_REVIEW": "PENDING_REVIEW",
    "APPROVED_FOR_OUTREACH": "APPROVED_FOR_OUTREACH",
    "REJECTED_REVIEW": "REJECTED_REVIEW",
    "OUTREACH_DRAFTED": "OUTREACH_DRAFTED",
    "DRAFT_OUTREACH": "OUTREACH_DRAFTED",
    "DRAFTED_OUTREACH": "OUTREACH_DRAFTED",
    "EMAIL_DRAFTED": "OUTREACH_DRAFTED",
    "SEND_OUTREACH": "OUTREACH_DRAFTED",
    "OUTREACH_SENT": "OUTREACH_SENT",
    "SENT_OUTREACH": "OUTREACH_SENT",
    "EMAIL_SENT": "OUTREACH_SENT",
    "CONTACT_MADE": "CONTACT_MADE",
    "CALL_BOOKED": "CALL_BOOKED",
    "BOOK_CALL": "CALL_BOOKED",
    "PROPOSAL_SENT": "PROPOSAL_SENT",
    "CLOSED_WON": "CLOSED_WON",
    "WON": "CLOSED_WON",
    "CLOSED_LOST": "CLOSED_LOST",
    "LOST": "CLOSED_LOST",
    "PROJECT_ACTIVE": "PROJECT_ACTIVE",
    "PROJECT_COMPLETE": "PROJECT_COMPLETE",
    "PROJECT_COMPLETED": "PROJECT_COMPLETE",
    "DISCARDED": "DISCARDED",
}

REVIEW_DECISION_STAGE_ALIASES = {
    "APPROVE": "APPROVED_FOR_OUTREACH",
    "APPROVED": "APPROVED_FOR_OUTREACH",
    "APPROVED_FOR_OUTREACH": "APPROVED_FOR_OUTREACH",
    "REJECT": "REJECTED_REVIEW",
    "REJECTED": "REJECTED_REVIEW",
    "REJECTED_REVIEW": "REJECTED_REVIEW",
    "DISCARD": "DISCARDED",
    "DISCARDED": "DISCARDED",
}

VISUAL_REVIEW_CATEGORIES = [
    ("mobile_layout", "Mobile Layout"),
    ("hero_section", "Hero Section"),
    ("call_to_action", "CTA Clarity"),
    ("header_navigation", "Header Navigation"),
    ("visual_clutter", "Visual Clutter"),
    ("readability", "Readability"),
    ("design_age", "Design Age"),
    ("form_or_booking_path", "Form Or Booking Path"),
    ("service_clarity", "Service Clarity"),
    ("trust_signals", "Trust Signals"),
    ("content_depth", "Content Depth"),
    ("seo_structure", "SEO Structure"),
    ("performance_perception", "Performance Perception"),
    ("layout_consistency", "Layout Consistency"),
    ("conversion_path", "Conversion Path"),
]

VISUAL_REVIEW_CLAIMS = {
    "mobile_layout": (
        "The mobile layout creates clear friction for visitors trying to evaluate or contact the business."
    ),
    "hero_section": (
        "The first screen does not establish the service, location, and next action as quickly as it could."
    ),
    "call_to_action": (
        "The call/request path is not prominent enough for a high-intent service visitor."
    ),
    "header_navigation": (
        "The header/navigation feels crowded and competes with the primary action."
    ),
    "visual_clutter": (
        "The page presents too many competing elements, making the next step less obvious."
    ),
    "readability": (
        "Several sections appear harder to scan than they should be for a service buyer."
    ),
    "design_age": (
        "The visual presentation feels dated relative to stronger local competitors."
    ),
    "form_or_booking_path": (
        "The request/booking path is not obvious enough from the main conversion areas."
    ),
    "service_clarity": "The site does not clarify the core services quickly enough.",
    "trust_signals": (
        "Trust signals are either weak, buried, or not organized around the conversion path."
    ),
    "content_depth": (
        "The content does not give enough structured service detail for a high-intent visitor."
    ),
    "seo_structure": (
        "The service/page structure appears thin for local search and service-specific discovery."
    ),
    "performance_perception": "The page presentation may feel heavy and harder to scan on mobile.",
    "layout_consistency": (
        "The layout lacks consistency between sections, which weakens perceived polish."
    ),
    "conversion_path": (
        "The route from landing on the site to calling or requesting service is not direct enough."
    ),
}


def resolve_project_path(path: str | Path | None) -> Path | None:
    """Resolve a local path relative to the project root."""

    if path is None:
        return None
    raw_path = Path(path)
    if raw_path.is_absolute():
        return raw_path.resolve(strict=False)
    return project_path(raw_path).resolve(strict=False)


MEDIA_ROOTS = tuple(
    (PROJECT_ROOT / directory).resolve(strict=False)
    for directory in ("screenshots", "artifacts", "runs")
)


def resolve_media_path(path: str | Path | None) -> Path | None:
    """Resolve a browser-served media path under approved project directories."""

    resolved = resolve_project_path(path)
    if resolved is None:
        return None
    for media_root in MEDIA_ROOTS:
        try:
            resolved.relative_to(media_root)
        except ValueError:
            continue
        return resolved
    return None


def parse_json_field(value: Any) -> Any:
    """Parse a JSON text column without raising template-breaking errors."""

    if value is None or value == "":
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}


def file_url_for(path: str | Path | None) -> str | None:
    """Return a local Flask URL for approved project media, or None."""

    resolved = resolve_media_path(path)
    if resolved is None:
        return None
    try:
        relative_path = resolved.relative_to(PROJECT_ROOT.resolve(strict=False))
    except ValueError:
        return None
    return url_for("project_media", relative_path=relative_path.as_posix())


def markets_config_path() -> Path:
    return project_path(MARKETS_CONFIG_PATH)


def load_yaml_text(text: str) -> Any:
    try:
        import yaml
    except ImportError:
        return _load_simple_yaml(text)
    return yaml.safe_load(text)


def dump_yaml_text(data: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError:
        return dump_simple_yaml(data)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)


def dump_simple_yaml(data: Any, indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(dump_simple_yaml(value, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}{key}: {simple_yaml_scalar(value)}")
    elif isinstance(data, list):
        for value in data:
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(dump_simple_yaml(value, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}- {simple_yaml_scalar(value)}")
    else:
        lines.append(f"{prefix}{simple_yaml_scalar(data)}")
    return "\n".join(line for line in lines if line != "") + "\n"


def simple_yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text:
        return '""'
    if re.search(r"(^\s|\s$|[,:#\[\]{}&*!|>'\"%@`])", text):
        return json.dumps(text)
    lowered = text.lower()
    if lowered in {"true", "false", "null", "none"}:
        return json.dumps(text)
    return text


def load_markets_document() -> dict[str, Any]:
    path = markets_config_path()
    if not path.exists():
        return {"markets": {}}
    data = load_yaml_text(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {"markets": {}}
    if not isinstance(data.get("markets"), dict):
        data["markets"] = {}
    return data


def load_configured_markets() -> list[dict[str, Any]]:
    markets = load_markets_document().get("markets") or {}
    configured = []
    for key, raw_market in markets.items():
        raw = raw_market if isinstance(raw_market, dict) else {}
        configured.append(
            {
                "key": str(key),
                "label": str(raw.get("label") or key),
                "state": str(raw.get("state") or ""),
                "priority": bool(raw.get("priority")),
                "included_cities": list_unique_strings(
                    raw.get("included_cities") or raw.get("cities") or []
                ),
                "notes": str(raw.get("notes") or ""),
                "raw": raw,
            }
        )
    return configured


def load_configured_niches() -> list[dict[str, str]]:
    try:
        niches = load_yaml_config("niches.yaml").get("niches") or {}
    except (FileNotFoundError, ValueError):
        niches = {}
    configured = []
    if not isinstance(niches, dict):
        return configured
    for key, raw_niche in niches.items():
        raw = raw_niche if isinstance(raw_niche, dict) else {}
        configured.append(
            {
                "key": str(key),
                "label": str(raw.get("label") or key).replace("_", " ").title(),
            }
        )
    return configured


def configured_market_keys() -> list[str]:
    return [market["key"] for market in load_configured_markets()]


def market_option_label(market: dict[str, Any]) -> str:
    label = str(market.get("label") or market.get("key") or "")
    key = str(market.get("key") or "")
    return label if label == key else f"{label} ({key})"


def build_market_options(selected_market: str = "") -> list[dict[str, Any]]:
    configured = load_configured_markets()
    options = [
        {
            "value": market["key"],
            "label": market_option_label(market),
            "can_run": True,
        }
        for market in configured
    ]
    option_values = {option["value"] for option in options}
    if selected_market and selected_market not in option_values and selected_market != UNKNOWN_MARKET_VALUE:
        options.append(
            {
                "value": selected_market,
                "label": f"{selected_market} (unconfigured)",
                "can_run": True,
            }
        )
        option_values.add(selected_market)
    if has_unconfigured_market_records([market["key"] for market in configured]):
        options.append(
            {
                "value": UNKNOWN_MARKET_VALUE,
                "label": "Unknown/Unconfigured",
                "can_run": False,
            }
        )
    return options


def has_unconfigured_market_records(configured_keys: list[str]) -> bool:
    clause, params = unconfigured_market_clause(configured_keys)
    row = get_connection().execute(
        f"SELECT COUNT(*) AS count FROM prospects WHERE {clause}",
        params,
    ).fetchone()
    return bool(row and int(row["count"]) > 0)


def unconfigured_market_clause(configured_keys: list[str]) -> tuple[str, list[Any]]:
    if not configured_keys:
        return "1 = 1", []
    placeholders = ", ".join("?" for _ in configured_keys)
    return (
        f"(market IS NULL OR market = '' OR market NOT IN ({placeholders}))",
        list(configured_keys),
    )


def selected_market_from_request() -> str:
    return str(request.args.get("market") or "").strip()


def selected_niches_from_request() -> list[str]:
    selected: list[str] = []
    for value in request.args.getlist("niche"):
        for niche in str(value or "").split(","):
            niche = niche.strip()
            if niche and niche not in selected:
                selected.append(niche)
    return selected


def selected_audit_mode_from_request() -> str:
    mode = str(request.args.get("audit_mode") or "deep").strip().lower()
    return mode if mode in {"deep", "fast"} else "deep"


def market_filter_context(selected_market: str = "") -> dict[str, Any]:
    return {
        "selected": selected_market,
        "options": build_market_options(selected_market),
    }


def market_where_clause(selected_market: str) -> tuple[str, list[Any]]:
    if not selected_market:
        return "1 = 1", []
    if selected_market == UNKNOWN_MARKET_VALUE:
        return unconfigured_market_clause(configured_market_keys())
    return "market = ?", [selected_market]


def append_market_filter(
    clauses: list[str],
    params: list[Any],
    selected_market: str,
) -> None:
    clause, clause_params = market_where_clause(selected_market)
    if clause != "1 = 1":
        clauses.append(clause)
        params.extend(clause_params)


def generate_market_key(label: str, state: str) -> str:
    base = f"{label}_{state}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    slug = re.sub(r"_+", "_", slug)[:64].strip("_")
    return slug or state.lower()


def parse_market_cities(value: Any) -> list[str]:
    raw = str(value or "")
    parts = [part.strip() for part in re.split(r"[\n,]+", raw) if part.strip()]
    return list_unique_strings(parts)


def add_market_from_form(form: Any) -> str:
    data = load_markets_document()
    markets = data.setdefault("markets", {})
    if not isinstance(markets, dict):
        raise ValueError("markets.yaml must contain a mapping named markets.")

    label = str(form.get("label") or "").strip()
    state = str(form.get("state") or "").strip().upper()
    market_key = str(form.get("market_key") or "").strip()
    cities = parse_market_cities(form.get("included_cities"))
    notes = str(form.get("notes") or "").strip()

    if not label:
        raise ValueError("Label is required.")
    if not re.fullmatch(r"[A-Z]{2}", state):
        raise ValueError("State must be a two-letter abbreviation.")
    if not market_key:
        market_key = generate_market_key(label, state)
    if not MARKET_KEY_PATTERN.fullmatch(market_key):
        raise ValueError("Market key can only use lowercase letters, numbers, and underscores.")
    if market_key in markets:
        raise ValueError(f"Market key '{market_key}' already exists.")
    if not cities:
        raise ValueError("Add at least one included city.")

    market_entry: dict[str, Any] = {
        "label": label,
        "priority": form.get("priority") == "1",
        "state": state,
        "included_cities": cities,
    }
    if notes:
        market_entry["notes"] = notes
    markets[market_key] = market_entry

    write_markets_document(data)
    return market_key


def write_markets_document(data: dict[str, Any]) -> None:
    path = markets_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
        shutil.copy2(path, backup_path)
    path.write_text(
        dump_yaml_text(data),
        encoding="utf-8",
    )


def market_message_from_query() -> dict[str, str] | None:
    message = str(request.args.get("message") or "").strip()
    if not message:
        return None
    status = str(request.args.get("status") or "").strip()
    return {"status": "error" if status == "error" else "success", "message": message}


def get_connection() -> sqlite3.Connection:
    """Open the dashboard database for the current request."""

    if "dashboard_db" not in g:
        db_path = resolve_project_path(current_app.config["DATABASE_PATH"])
        if db_path is None:
            raise RuntimeError("DATABASE_PATH is not configured.")
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.dashboard_db = connection
    return g.dashboard_db


def ensure_outreach_queue_schema(db_path: str | Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS outreach_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_key TEXT NOT NULL UNIQUE,
                prospect_id INTEGER NOT NULL,
                contact_id INTEGER,
                email TEXT NOT NULL,
                campaign TEXT NOT NULL,
                step INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                send_after TEXT,
                subject TEXT,
                draft_artifact_id INTEGER,
                public_packet_artifact_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (prospect_id) REFERENCES prospects(id) ON DELETE CASCADE,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
                FOREIGN KEY (draft_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL,
                FOREIGN KEY (public_packet_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_outreach_queue_status
                ON outreach_queue(status);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_queue_active_unique
                ON outreach_queue(prospect_id, email, campaign, step)
                WHERE status <> 'cancelled';
            """
        )
        connection.commit()
    finally:
        connection.close()


def load_prospect(prospect_id: int) -> dict[str, Any] | None:
    row = get_connection().execute(
        "SELECT * FROM prospects WHERE id = ?",
        (prospect_id,),
    ).fetchone()
    if row is None:
        return None
    prospect = _row_to_dict(row)
    prospect["pipeline_stage"] = compute_pipeline_stage(prospect)
    prospect["score_explanation"] = parse_json_field(prospect.get("score_explanation_json"))
    prospect["metadata"] = parse_json_field(prospect.get("metadata_json"))
    return prospect


def load_artifacts(prospect_id: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT *
        FROM artifacts
        WHERE prospect_id = ?
        ORDER BY artifact_type, id
        """,
        (prospect_id,),
    ).fetchall()
    artifacts = []
    for row in rows:
        artifact = _row_to_dict(row)
        resolved_path = resolve_project_path(artifact.get("path"))
        artifact["metadata"] = parse_json_field(artifact.get("metadata_json"))
        artifact["file_url"] = file_url_for(artifact.get("path"))
        artifact["file_exists"] = bool(resolved_path and resolved_path.is_file())
        artifact["resolved_path"] = str(resolved_path) if resolved_path else None
        artifacts.append(artifact)
    return artifacts


def load_audits(prospect_id: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT *
        FROM website_audits
        WHERE prospect_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    audits = []
    for row in rows:
        audit = _row_to_dict(row)
        audit["findings"] = parse_json_field(audit.get("findings_json"))
        audit["raw"] = parse_json_field(audit.get("raw_json"))
        audits.append(audit)
    return audits


def load_outreach_drafts(
    artifacts: list[dict[str, Any]],
    *,
    prospect: dict[str, Any] | None = None,
    primary_contact: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    packet_status = public_packet_review_status(artifacts)
    drafts = []
    for artifact in artifacts:
        if artifact.get("artifact_type") != "email_draft":
            continue
        draft = dict(artifact)
        text = ""
        resolved_path = draft.get("resolved_path")
        if draft.get("file_exists") and resolved_path:
            try:
                text = Path(resolved_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        fallback_subject = draft.get("metadata", {}).get("subject")
        subject, body = split_email_draft_text(text, fallback_subject)
        draft["step"] = email_draft_step(draft)
        draft["subject"] = subject
        draft["body"] = body
        enrich_outreach_draft_review_state(
            draft,
            prospect=prospect,
            primary_contact=primary_contact,
            packet_status=packet_status,
        )
        drafts.append(draft)
    drafts.sort(key=lambda item: (item.get("step") or 999, item.get("artifact_key") or ""))
    return drafts


def split_email_draft_text(text: str, fallback_subject: Any = None) -> tuple[str, str]:
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body_lines = lines[1:]
        if body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]
        return subject or str(fallback_subject or ""), "\n".join(body_lines).strip()
    return str(fallback_subject or ""), text.strip()


def email_draft_step(artifact: dict[str, Any]) -> int | None:
    metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    try:
        return int(metadata.get("step"))
    except (TypeError, ValueError):
        pass
    artifact_key = str(artifact.get("artifact_key") or "")
    marker = ":email_"
    if marker not in artifact_key:
        return None
    try:
        return int(artifact_key.rsplit(marker, 1)[-1])
    except ValueError:
        return None


def public_packet_review_status(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    packets = [artifact for artifact in artifacts if artifact.get("artifact_type") == "public_packet"]
    ready_packets = [packet for packet in packets if str(packet.get("status") or "").lower() == "ready"]
    packet = (ready_packets or packets)[-1] if (ready_packets or packets) else None
    metadata = packet.get("metadata") if packet and isinstance(packet.get("metadata"), dict) else {}
    url = ""
    if packet:
        url = str(
            packet.get("artifact_url")
            or metadata.get("public_packet_url")
            or metadata.get("public_url")
            or metadata.get("relative_url")
            or ""
        ).strip()
    return {
        "exists": bool(packet),
        "ready": bool(packet and str(packet.get("status") or "").lower() == "ready"),
        "status": packet.get("status") if packet else "missing",
        "url": url,
    }


def enrich_outreach_draft_review_state(
    draft: dict[str, Any],
    *,
    prospect: dict[str, Any] | None,
    primary_contact: dict[str, Any] | None,
    packet_status: dict[str, Any],
) -> None:
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    selected_issues = metadata.get("selected_issues")
    draft["selected_issues"] = selected_issues if isinstance(selected_issues, list) else []
    draft["style"] = str(metadata.get("style") or "owner_friendly")
    draft["variant_index"] = metadata.get("variant_index", 0)
    draft["manual_edited"] = bool(metadata.get("manual_edited"))
    draft["manual_edited_at"] = str(metadata.get("manual_edited_at") or "")

    metadata_packet_url = str(metadata.get("public_packet_url") or "").strip()
    packet_url = metadata_packet_url or str(packet_status.get("url") or "").strip()
    draft["public_packet_url"] = packet_url
    if metadata_packet_url:
        packet_label = "Linked in draft"
    elif packet_url:
        packet_label = "Packet exists; draft missing URL"
    else:
        packet_label = "Missing public packet URL"
    draft["public_packet_status_label"] = packet_label

    contact_email = str(
        metadata.get("recipient_email")
        or (primary_contact or {}).get("email")
        or ""
    ).strip()
    draft["contact_ready"] = bool(contact_email)
    draft["copy_flag_labels"] = draft_copy_quality_flag_labels(
        draft,
        prospect=prospect,
        packet_status=packet_status,
    )
    draft["ready_for_queue_review"] = bool(
        draft.get("step") == 1
        and draft["manual_edited"]
        and packet_url
        and contact_email
    )


def draft_copy_quality_flag_labels(
    draft: dict[str, Any],
    *,
    prospect: dict[str, Any] | None,
    packet_status: dict[str, Any],
) -> list[str]:
    flags = draft_copy_quality_flag_codes(
        subject=str(draft.get("subject") or ""),
        body=str(draft.get("body") or ""),
        metadata=draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {},
        prospect=prospect,
        step=draft.get("step"),
        packet_url=str(packet_status.get("url") or ""),
    )
    return [copy_quality_flag_label(flag) for flag in flags]


def draft_copy_quality_flag_codes(
    *,
    subject: str,
    body: str,
    metadata: dict[str, Any],
    prospect: dict[str, Any] | None,
    step: Any,
    packet_url: str,
) -> list[str]:
    text = f"{subject}\n{body}"
    lowered = text.lower()
    flags: list[str] = []
    raw_metadata_flags = metadata.get("copy_quality_flags")
    if isinstance(raw_metadata_flags, list):
        flags.extend(str(flag) for flag in raw_metadata_flags if str(flag or "").strip())

    draft_packet_url = str(metadata.get("public_packet_url") or packet_url or "").strip()
    try:
        step_number = int(step or metadata.get("step") or 0)
    except (TypeError, ValueError):
        step_number = 0
    if step_number == 1 and not draft_packet_url:
        flags.append("missing_public_packet_url")

    if "[your name]" in lowered or "local growth audit" in lowered:
        flags.append("placeholder_sender")

    for phrase in OUTREACH_BANNED_COPY_PHRASES:
        if phrase in lowered:
            flags.append(f"banned_phrase:{phrase}")

    word_count = len(re.findall(r"\b[\w']+\b", body))
    if (step_number == 1 and word_count > 220) or (
        step_number in {2, 3} and word_count > 160
    ):
        flags.append("too_long")

    if "- " not in body:
        flags.append("no_issue_bullets")

    business_name = str((prospect or {}).get("business_name") or "").strip().lower()
    if business_name and business_name not in lowered:
        flags.append("no_business_name")

    for phrase in OUTREACH_INTERNAL_JARGON_PHRASES:
        if phrase in lowered:
            flags.append(f"internal_jargon:{phrase}")

    return unique_text_values(flags)


def copy_quality_flag_label(flag: str) -> str:
    if flag.startswith("contains_banned_phrase:"):
        return f"Banned phrase present: {flag.split(':', 1)[1]}"
    if flag.startswith("banned_phrase:"):
        return f"Banned phrase present: {flag.split(':', 1)[1]}"
    if flag.startswith("internal_jargon:"):
        return f"Internal jargon present: {flag.split(':', 1)[1]}"
    labels = {
        "missing_public_packet_url": "Missing public packet URL",
        "public_packet_missing_step_1": "Missing public packet URL",
        "placeholder_sender": "Placeholder sender/name needs review",
        "contains_guaranteed": "Guaranteed claim present",
        "too_long": "Draft may be too long",
        "step_1_over_220_words": "Step 1 is over 220 words",
        "no_issue_bullets": "No issue bullets found",
        "no_specific_issue_bullets": "No issue bullets found",
        "no_business_name": "Business name is missing from the draft",
        "no_opt_out_line_variable": "Opt-out line is missing",
        "no_sender_name": "Sender name is missing",
        "more_than_5_issue_bullets": "Too many issue bullets",
        "unresolved_template_variable": "Unresolved template variable",
    }
    return labels.get(flag, flag.replace("_", " ").title())


def unique_text_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def dashboard_auth_enabled() -> bool:
    configured = any(
        str(os.environ.get(key) or "").strip()
        for key in ("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD", "DASHBOARD_PASSWORD_HASH")
    )
    explicit = str(os.environ.get("DASHBOARD_AUTH_ENABLED") or "").strip().lower()
    if explicit in {"1", "true", "yes", "y", "on"}:
        return True
    if explicit in {"0", "false", "no", "n", "off"}:
        return False
    return configured


def dashboard_auth_configured() -> bool:
    return bool(
        str(os.environ.get("DASHBOARD_USERNAME") or "").strip()
        and (
            str(os.environ.get("DASHBOARD_PASSWORD_HASH") or "").strip()
            or str(os.environ.get("DASHBOARD_PASSWORD") or "").strip()
        )
    )


def dashboard_username() -> str:
    return str(os.environ.get("DASHBOARD_USERNAME") or "").strip()


def dashboard_password_matches(password: str) -> bool:
    password_hash = str(os.environ.get("DASHBOARD_PASSWORD_HASH") or "").strip()
    if password_hash:
        try:
            return check_password_hash(password_hash, password)
        except ValueError:
            return False
    plain_password = str(os.environ.get("DASHBOARD_PASSWORD") or "")
    return bool(plain_password) and secrets.compare_digest(password, plain_password)


def safe_next_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path or not path.startswith("/") or path.startswith("//"):
        return url_for("overview")
    return path


def public_outreach_file_path(*parts: str) -> Path | None:
    root = project_path("public_outreach").resolve(strict=False)
    candidate = root.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = (
        os.environ.get("FLASK_SECRET_KEY")
        or os.environ.get("SECRET_KEY")
        or "local-dashboard-dev-secret"
    )
    app.config["DASHBOARD_AUTH_ENABLED"] = dashboard_auth_enabled()
    app.config["DATABASE_PATH"] = str(resolve_project_path(db_path or get_database_path()))
    dashboard_jobs.ensure_schema(app.config["DATABASE_PATH"])
    ensure_outreach_queue_schema(app.config["DATABASE_PATH"])
    dashboard_jobs.mark_stale_jobs(app.config["DATABASE_PATH"])

    @app.teardown_appcontext
    def close_connection(_exception: BaseException | None = None) -> None:
        connection = g.pop("dashboard_db", None)
        if connection is not None:
            connection.close()

    @app.template_filter("pretty_label")
    def pretty_label(value: Any) -> str:
        return str(value or "").replace("_", " ").strip().title()

    @app.template_filter("external_href")
    def external_href(value: Any) -> str | None:
        if not value:
            return None
        href = str(value).strip()
        if not href:
            return None
        if "://" not in href:
            return f"https://{href}"
        return href

    @app.context_processor
    def auth_template_context() -> dict[str, Any]:
        return {
            "dashboard_auth_enabled": app.config["DASHBOARD_AUTH_ENABLED"],
            "dashboard_user": session.get("dashboard_username", ""),
        }

    @app.before_request
    def require_dashboard_login():
        if not app.config["DASHBOARD_AUTH_ENABLED"]:
            return None
        if request.endpoint in PUBLIC_AUTH_ENDPOINTS:
            return None
        if session.get("dashboard_authenticated"):
            return None
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not app.config["DASHBOARD_AUTH_ENABLED"]:
            return redirect(url_for("overview"))
        if not dashboard_auth_configured():
            return (
                render_template(
                    "dashboard/login.html",
                    active_page="login",
                    error="Dashboard login is enabled, but username/password env vars are missing.",
                    next_url=url_for("overview"),
                ),
                503,
            )
        next_url = safe_next_path(request.values.get("next"))
        error = ""
        if request.method == "POST":
            username = str(request.form.get("username") or "").strip()
            password = str(request.form.get("password") or "")
            if secrets.compare_digest(username, dashboard_username()) and dashboard_password_matches(password):
                session.clear()
                session["dashboard_authenticated"] = True
                session["dashboard_username"] = username
                return redirect(next_url)
            error = "Login failed. Check the username and password."
        return render_template(
            "dashboard/login.html",
            active_page="login",
            error=error,
            next_url=next_url,
        )

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/p/<token>/")
    @app.get("/p/<token>/<path:filename>")
    def public_packet_page(token: str, filename: str = "index.html"):
        if not PUBLIC_PACKET_TOKEN_RE.match(token):
            abort(404)
        resolved = public_outreach_file_path("p", token, filename)
        if resolved is None:
            abort(404)
        return send_file(resolved)

    @app.get("/assets/<path:filename>")
    def public_packet_asset(filename: str):
        resolved = public_outreach_file_path("assets", filename)
        if resolved is None:
            abort(404)
        return send_file(resolved)

    @app.get("/")
    def overview() -> str:
        selected_market = selected_market_from_request()
        stage_counts = load_stage_counts(selected_market)
        total_prospects = sum(item["count"] for item in stage_counts)
        top_markets = load_group_counts("market", market=selected_market)
        top_niches = load_group_counts("niche", market=selected_market)
        return render_template(
            "dashboard/overview.html",
            active_page="overview",
            db_path=app.config["DATABASE_PATH"],
            market_filter=market_filter_context(selected_market),
            stage_counts=stage_counts,
            total_prospects=total_prospects,
            top_markets=top_markets,
            top_niches=top_niches,
            market_summary=load_market_summary_rows(selected_market),
        )

    @app.get("/review")
    def review_queue() -> str:
        selected_market = selected_market_from_request()
        review_rows = load_review_queue(selected_market)
        return render_template(
            "dashboard/review.html",
            active_page="review",
            market_filter=market_filter_context(selected_market),
            prospects=review_rows,
            message=review_queue_message_from_code(request.args.get("result")),
        )

    @app.post("/review/<int:prospect_id>/delete")
    def quick_delete_review_card(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        selected_market = request.form.get("market", "").strip()
        existing_notes = str(prospect.get("human_review_notes") or "").strip()
        quick_note = "Quick deleted from review queue after thumbnail scan."
        notes = f"{existing_notes}\n\n{quick_note}" if existing_notes else quick_note

        connection = get_connection()
        apply_review_decision(
            connection,
            prospect_id=prospect_id,
            action="reject",
            score=prospect.get("human_review_score"),
            notes=notes,
        )
        connection.commit()
        if selected_market:
            return redirect(url_for("review_queue", market=selected_market, result="quick_deleted"))
        return redirect(url_for("review_queue", result="quick_deleted"))

    @app.get("/leads")
    def leads() -> str:
        filters = {
            "stage": request.args.get("stage", "").strip().upper(),
            "market": request.args.get("market", "").strip(),
            "niche": request.args.get("niche", "").strip(),
            "q": request.args.get("q", "").strip(),
            "limit": parse_limit(request.args.get("limit")),
        }
        lead_rows = load_leads(filters)
        return render_template(
            "dashboard/leads.html",
            active_page="leads",
            filters=filters,
            leads=lead_rows,
            stage_options=PIPELINE_STAGE_BUCKETS,
            market_options=build_market_options(filters["market"]),
            niche_options=load_distinct_values("niche", market=filters["market"]),
        )

    @app.get("/crm")
    def crm() -> str:
        selected_market = selected_market_from_request()
        columns = load_crm_columns(selected_market)
        return render_template(
            "dashboard/crm.html",
            active_page="crm",
            market_filter=market_filter_context(selected_market),
            columns=columns,
            stages=CRM_STAGES,
        )

    @app.get("/outbound")
    def outbound() -> str:
        filters = outbound_filters_from_request()
        readiness = load_outbound_readiness(filters)
        return render_template(
            "dashboard/outbound.html",
            active_page="outbound",
            filters=filters,
            market_options=build_market_options(filters["market"]),
            niche_options=load_distinct_values("niche", market=filters["market"]),
            infra=readiness["infra"],
            counts=readiness["counts"],
            ready_rows=readiness["ready_rows"],
            blocked_groups=readiness["blocked_groups"],
            queue_limit_default=OUTBOUND_DEFAULT_QUEUE_LIMIT,
            queue_limit_max=OUTBOUND_MAX_QUEUE_LIMIT,
            message=outbound_message_from_code(request.args.get("result")),
        )

    @app.post("/outbound/queue")
    def create_outbound_queue():
        filters = outbound_filters_from_request(source=request.form)
        limit = parse_outbound_queue_limit(request.form.get("queue_limit"))
        try:
            created, skipped = create_step_1_send_queue(filters, limit=limit)
        except ValueError as exc:
            return redirect(outbound_url(filters, result=f"error:{exc}"))
        return redirect(outbound_url(filters, result=f"queued:{created}:{skipped}"))

    @app.get("/send")
    def send_page() -> str:
        send_state = load_send_dashboard_state()
        return render_template(
            "dashboard/send.html",
            active_page="send",
            infra=send_state["infra"],
            config=send_state["config"],
            daily_sent_count=send_state["daily_sent_count"],
            daily_remaining=send_state["daily_remaining"],
            queued_rows=send_state["queued_rows"],
            blocked_rows=send_state["blocked_rows"],
            skipped_rows=send_state["skipped_rows"],
            queued_sendable_count=send_state["queued_sendable_count"],
            inbox_sync=send_state["inbox_sync"],
            send_limit_default=SEND_DEFAULT_LIMIT,
            send_limit_max=SEND_MAX_LIMIT,
            message=send_message_from_code(request.args.get("result")),
        )

    @app.post("/send/test")
    def send_test_email():
        if request.form.get("confirm_test") != "1":
            return redirect(url_for("send_page", result="error:Confirm the test send first."))
        recipient = normalize_email(request.form.get("test_email"))
        if not recipient:
            return redirect(url_for("send_page", result="error:Enter a valid test recipient email."))
        try:
            send_dashboard_test_email(recipient)
        except ValueError as exc:
            return redirect(url_for("send_page", result=f"error:{exc}"))
        except Exception as exc:
            write_send_test_log(
                {
                    "status": "failed",
                    "recipient_redacted": redact_email(recipient),
                    "error": str(exc)[:500],
                    "created_at": utc_now(),
                }
            )
            return redirect(url_for("send_page", result="error:Test email failed. Check runs/latest/dashboard_send_test.txt."))
        return redirect(url_for("send_page", result="test_sent"))

    @app.post("/send/batch")
    def send_batch():
        if request.form.get("confirm_send") != "1":
            return redirect(url_for("send_page", result="error:Confirm the real send first. No email was sent."))
        try:
            limit = parse_send_limit(request.form.get("limit"))
            result = send_dashboard_batch(
                limit=limit,
                include_attachments=request.form.get("include_attachments") == "1",
            )
        except ValueError as exc:
            return redirect(url_for("send_page", result=f"error:{exc}"))
        code = f"batch:{result['sent']}:{result['failed']}:{result['skipped']}"
        return redirect(url_for("send_page", result=code))

    @app.get("/crm/stage/<stage>")
    def crm_stage(stage: str) -> str:
        normalized_stage = _normalize_token(stage)
        if normalized_stage not in CRM_STAGE_LABELS:
            abort(404)
        selected_market = selected_market_from_request()
        prospects = load_crm_stage_prospects(
            normalized_stage,
            market=selected_market,
            limit=MAX_LIMIT,
        )
        return render_template(
            "dashboard/crm_stage.html",
            active_page="crm",
            stage=normalized_stage,
            stage_label=CRM_STAGE_LABELS[normalized_stage],
            market_filter=market_filter_context(selected_market),
            prospects=prospects,
            stages=CRM_STAGES,
        )

    @app.get("/sales-packet/<int:prospect_id>")
    def sales_packet(prospect_id: int) -> str:
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        if not sales_packet_available(prospect):
            abort(404)

        artifacts = load_artifacts(prospect_id)
        audits = load_audits(prospect_id)
        contacts = load_contacts(prospect_id)
        primary_contact = primary_contact_from_contacts(contacts)
        packet = build_sales_packet(
            prospect=prospect,
            audits=audits,
            artifacts=artifacts,
            primary_contact=primary_contact,
        )
        return render_template(
            "dashboard/sales_packet.html",
            active_page="crm",
            prospect=prospect,
            primary_contact=primary_contact,
            packet=packet,
            message=sales_packet_message_from_code(request.args.get("result")),
        )

    @app.post("/sales-packet/<int:prospect_id>/notes")
    def save_sales_packet_notes(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        if not sales_packet_available(prospect):
            abort(404)
        notes = str(request.form.get("sales_notes") or "").strip()
        connection = get_connection()
        save_sales_notes(connection, prospect=prospect, notes=notes)
        connection.commit()
        return redirect(url_for("sales_packet", prospect_id=prospect_id, result="notes_saved"))

    @app.get("/pipeline")
    def pipeline() -> str:
        selected_market = selected_market_from_request()
        if selected_market:
            return redirect(url_for("run_controls", market=selected_market))
        return redirect(url_for("run_controls"))

    @app.get("/run")
    def run_controls() -> str:
        selected_market = selected_market_from_request()
        selected_niches = selected_niches_from_request()
        market_options = build_market_options(selected_market)
        return render_template(
            "dashboard/run.html",
            active_page="run",
            market_filter=market_filter_context(selected_market),
            market_options=market_options,
            job_market_options=[option for option in market_options if option.get("can_run")],
            niches=load_configured_niches(),
            selected_niches=selected_niches,
            primary_niche=selected_niches[0] if selected_niches else "",
            run_counts=load_run_counts(selected_market),
            recommended_action=load_run_recommended_action(
                selected_market,
                selected_niches,
            ),
            recent_jobs=dashboard_jobs.list_jobs(limit=10, db_path=app.config["DATABASE_PATH"]),
            message=job_message_from_code(request.args.get("result")),
            places_limit_default=50,
            places_limit_max=PLACES_JOB_LIMIT,
            audit_limit_default=20,
            audit_limit_max=AUDIT_JOB_LIMIT,
            selected_audit_mode=selected_audit_mode_from_request(),
            artifact_limit_default=25,
        )

    @app.post("/run/full-pipeline")
    def start_full_market_pipeline():
        try:
            job_key = start_full_pipeline_from_form(request.form)
        except ValueError as exc:
            return redirect(run_controls_url_from_form(request.form, result=f"error:{exc}"))
        return redirect(url_for("job_detail", job_key=job_key))

    @app.get("/jobs")
    def jobs() -> str:
        selected_market = selected_market_from_request()
        market_options = build_market_options(selected_market)
        return render_template(
            "dashboard/jobs.html",
            active_page="jobs",
            jobs=dashboard_jobs.list_jobs(db_path=app.config["DATABASE_PATH"]),
            job_types={
                key: job
                for key, job in dashboard_jobs.ALLOWED_JOBS.items()
                if key != "full_pipeline"
            },
            market_filter=market_filter_context(selected_market),
            job_market_options=[option for option in market_options if option.get("can_run")],
            niche_options=load_distinct_values("niche"),
            message=job_message_from_code(request.args.get("result")),
        )

    @app.get("/jobs/<job_key>")
    def job_detail(job_key: str) -> str:
        job = dashboard_jobs.get_job(job_key, db_path=app.config["DATABASE_PATH"])
        if job is None:
            abort(404)
        return render_template(
            "dashboard/job_detail.html",
            active_page="jobs",
            job=job,
            job_summary=dashboard_jobs.read_job_summary(
                job_key,
                db_path=app.config["DATABASE_PATH"],
            ),
            log_text=dashboard_jobs.read_job_log(
                job_key,
                db_path=app.config["DATABASE_PATH"],
            ),
        )

    @app.post("/jobs/start")
    def start_dashboard_job():
        try:
            job_key = start_job_from_form(request.form)
        except ValueError as exc:
            if request.form.get("source") == "run":
                return redirect(run_controls_url_from_form(request.form, result=f"error:{exc}"))
            return redirect(url_for("jobs", result=f"error:{exc}"))
        return redirect(url_for("job_detail", job_key=job_key))

    @app.get("/jobs/<job_key>/status")
    def job_status(job_key: str):
        job = dashboard_jobs.get_job(job_key, db_path=app.config["DATABASE_PATH"])
        if job is None:
            abort(404)
        return jsonify(
            {
                "job_key": job["job_key"],
                "status": job["status"],
                "status_label": str(job["status"] or "").replace("_", " ").strip().title(),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "error_summary": job.get("metadata", {}).get("error_summary"),
                "summary": dashboard_jobs.read_job_summary(
                    job_key,
                    db_path=app.config["DATABASE_PATH"],
                ),
                "log_tail": dashboard_jobs.read_job_log(
                    job_key,
                    tail_chars=6000,
                    db_path=app.config["DATABASE_PATH"],
                ),
            }
        )

    @app.post("/pipeline/run")
    def run_pipeline_job():
        return redirect(
            run_controls_url_from_form(
                request.form,
                result="error:The old Pipeline runner has been retired. Use the Run tab.",
            )
        )

    @app.get("/markets")
    def markets() -> str:
        return render_template(
            "dashboard/markets.html",
            active_page="markets",
            markets=load_market_manager_rows(),
            message=market_message_from_query(),
        )

    @app.post("/markets/add")
    def add_market():
        try:
            market_key = add_market_from_form(request.form)
        except ValueError as exc:
            return redirect(
                url_for(
                    "markets",
                    status="error",
                    message=str(exc),
                )
            )
        return redirect(
            url_for(
                "markets",
                status="success",
                message=f"Added market '{market_key}'.",
            )
        )

    @app.get("/case/<int:prospect_id>")
    def case_file(prospect_id: int) -> str:
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        artifacts = load_artifacts(prospect_id)
        audits = load_audits(prospect_id)
        contacts = load_contacts(prospect_id)
        primary_contact = primary_contact_from_contacts(contacts)
        stage_history = load_stage_history(prospect_id)
        outreach_drafts = load_outreach_drafts(
            artifacts,
            prospect=prospect,
            primary_contact=primary_contact,
        )
        step_one_draft = next(
            (draft for draft in outreach_drafts if draft.get("step") == 1),
            None,
        )
        followup_drafts = [draft for draft in outreach_drafts if draft.get("step") != 1]
        public_packet_status = public_packet_review_status(artifacts)
        artifact_map = artifacts_by_type(artifacts)
        audit_map = audits_by_type(audits)
        score_explanation = prospect.get("score_explanation") or {}
        signals = score_explanation.get("signals") or {}
        email_candidates = list_unique_strings(signals.get("email_candidates") or [])
        site_audit = audit_map.get("site")
        site_findings = site_audit.get("findings") if site_audit else {}
        visual_review = audit_map.get("visual_review")
        visual_findings = visual_review.get("findings") if visual_review else {}
        visual_issue_map = visual_findings.get("issues") or {}
        top_visual_issues = visual_findings.get("top_issues") or []
        return render_template(
            "dashboard/case.html",
            active_page="leads",
            prospect=prospect,
            artifacts=artifacts,
            artifact_map=artifact_map,
            outreach_drafts=outreach_drafts,
            step_one_draft=step_one_draft,
            followup_drafts=followup_drafts,
            public_packet_status=public_packet_status,
            contacts=contacts,
            primary_contact=primary_contact,
            stage_history=stage_history,
            crm_stages=CRM_STAGES,
            audits=audits,
            audit_map=audit_map,
            site_audit=site_audit,
            site_findings=site_findings,
            score_explanation=score_explanation,
            signals=signals,
            top_reasons=score_explanation.get("top_reasons") or [],
            email_candidates=email_candidates,
            business_domain_emails=list_unique_strings(signals.get("business_domain_emails") or []),
            visual_review=visual_review,
            visual_findings=visual_findings,
            visual_issue_categories=VISUAL_REVIEW_CATEGORIES,
            visual_issue_map=visual_issue_map,
            top_visual_issues=top_visual_issues,
            review_message=review_message_from_code(request.args.get("review")),
        )

    @app.post("/case/<int:prospect_id>/review")
    def record_case_review(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)

        action = request.form.get("action", "").strip().lower()
        if action not in {"approve", "reject", "hold"}:
            abort(400)

        try:
            score = parse_review_score(request.form.get("human_review_score"))
        except ValueError:
            abort(400)
        notes = request.form.get("human_review_notes", "").strip() or None
        primary_email = request.form.get("primary_email", "").strip()
        contact_role = (
            request.form.get("contact_role", "").strip() or "owner/operator candidate"
        )

        connection = get_connection()
        apply_review_decision(
            connection,
            prospect_id=prospect_id,
            action=action,
            score=score,
            notes=notes,
        )

        saved_email = False
        if action == "approve" and primary_email:
            upsert_dashboard_contact(
                connection,
                prospect_id=prospect_id,
                email=primary_email,
                role=contact_role,
            )
            saved_email = True

        connection.commit()
        if action == "approve" and not saved_email:
            message_code = "approved_missing_email"
        elif action == "reject":
            message_code = "rejected"
        else:
            message_code = f"{action}d" if action != "hold" else "held"
        return redirect(url_for("case_file", prospect_id=prospect_id, review=message_code))

    @app.post("/case/<int:prospect_id>/stage")
    def update_case_stage(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        new_status = _normalize_token(request.form.get("new_status"))
        if new_status not in CRM_STAGE_LABELS:
            abort(400)
        note = str(request.form.get("stage_note") or "").strip() or None
        connection = get_connection()
        apply_crm_stage_change(
            connection,
            prospect=prospect,
            new_status=new_status,
            note=note,
        )
        connection.commit()
        if request.form.get("return_to") == "sales_packet" and new_status in SALES_PACKET_STAGES:
            return redirect(
                url_for("sales_packet", prospect_id=prospect_id, result="stage_updated")
            )
        return redirect(url_for("case_file", prospect_id=prospect_id, review="stage_updated"))

    @app.post("/case/<int:prospect_id>/contact")
    def save_case_contact(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        try:
            contact_id = parse_optional_int(request.form.get("contact_id"))
        except ValueError:
            abort(400)

        connection = get_connection()
        upsert_primary_contact(
            connection,
            prospect_id=prospect_id,
            contact_id=contact_id,
            name=str(request.form.get("contact_name") or "").strip() or None,
            role=str(request.form.get("contact_role") or "").strip() or None,
            email=str(request.form.get("contact_email") or "").strip() or None,
            phone=str(request.form.get("contact_phone") or "").strip() or None,
            notes=str(request.form.get("contact_notes") or "").strip() or None,
        )
        connection.commit()
        return redirect(url_for("case_file", prospect_id=prospect_id, review="contact_saved"))

    @app.post("/case/<int:prospect_id>/visual-review")
    def record_visual_review(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)

        try:
            score, findings, summary = parse_visual_review_form(request.form)
        except ValueError:
            abort(400)

        connection = get_connection()
        save_visual_review(
            connection,
            prospect=prospect,
            score=score,
            findings=findings,
            summary=summary,
        )
        connection.commit()
        return redirect(url_for("case_file", prospect_id=prospect_id, review="visual_review_saved"))

    @app.post("/case/<int:prospect_id>/outreach-drafts")
    def generate_case_outreach_drafts(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        force = request.form.get("force") == "1"
        try:
            style = parse_outreach_copy_style(request.form.get("style"))
            variant_index = parse_variant_index(request.form.get("variant_index"))
            steps = parse_outreach_regenerate_steps(request.form.get("step"))
        except ValueError:
            abort(400)
        try:
            result_code = run_case_outreach_draft_job(
                prospect_id,
                force=force,
                style=style,
                variant_index=variant_index,
                steps=steps,
            )
        except ValueError as exc:
            write_pipeline_job_log(
                title="Rejected outreach draft job",
                command=[],
                returncode=None,
                stdout="",
                stderr=str(exc),
            )
            result_code = "outreach_drafts_failed"
        return redirect(url_for("case_file", prospect_id=prospect_id, review=result_code))

    @app.post("/case/<int:prospect_id>/drafts/regenerate")
    def regenerate_case_outreach_drafts(prospect_id: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        try:
            style = parse_outreach_copy_style(request.form.get("style"))
            variant_index = parse_variant_index(request.form.get("variant_index"))
            steps = parse_outreach_regenerate_steps(request.form.get("step"))
        except ValueError:
            abort(400)
        force = request.form.get("force") == "1"
        try:
            result_code = run_case_outreach_draft_job(
                prospect_id,
                force=force,
                style=style,
                variant_index=variant_index,
                steps=steps,
            )
        except ValueError as exc:
            write_pipeline_job_log(
                title="Rejected outreach draft regeneration",
                command=[],
                returncode=None,
                stdout="",
                stderr=str(exc),
            )
            result_code = "outreach_drafts_failed"
        return redirect(url_for("case_file", prospect_id=prospect_id, review=result_code))

    @app.post("/case/<int:prospect_id>/draft/<int:step>/save")
    def save_case_outreach_draft(prospect_id: int, step: int):
        prospect = load_prospect(prospect_id)
        if prospect is None:
            abort(404)
        if step not in OUTREACH_DRAFT_STEPS:
            abort(404)

        subject = str(request.form.get("subject") or "").strip()
        body = normalize_draft_body(request.form.get("body"))
        if not subject or not body:
            abort(400)

        connection = get_connection()
        artifact = load_email_draft_artifact(connection, prospect_id, step)
        if artifact is None:
            abort(404)

        try:
            save_email_draft_artifact(
                connection,
                prospect=prospect,
                artifact=artifact,
                step=step,
                subject=subject,
                body=body,
                packet_url=str(public_packet_review_status(load_artifacts(prospect_id)).get("url") or ""),
            )
        except ValueError:
            abort(400)
        connection.commit()
        return redirect(url_for("case_file", prospect_id=prospect_id, review="draft_saved"))

    @app.get("/media/<path:relative_path>")
    def project_media(relative_path: str):
        resolved = resolve_media_path(relative_path)
        if resolved is None or not resolved.is_file():
            abort(404)
        return send_file(resolved)

    @app.get("/files/<path:file_path>")
    def project_file(file_path: str):
        resolved = resolve_project_path(file_path)
        if resolved is None:
            abort(404)
        try:
            resolved.relative_to(PROJECT_ROOT.resolve(strict=False))
        except ValueError:
            abort(404)
        if not resolved.is_file():
            abort(404)
        return send_file(resolved)

    @app.get("/health")
    def health() -> tuple[str, int, dict[str, str]]:
        return "OK\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    return app


def load_stage_counts(market: str = "") -> list[dict[str, Any]]:
    counts = {stage: 0 for stage in PIPELINE_STAGE_BUCKETS}
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_market_filter(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT id, status, qualification_status, audit_data_status,
               human_review_status, human_review_decision, next_action, market
        FROM prospects
        WHERE {" AND ".join(clauses)}
        """
        ,
        params,
    ).fetchall()
    for row in rows:
        stage = compute_pipeline_stage(row)
        counts[stage] = counts.get(stage, 0) + 1
    return [{"stage": stage, "count": counts.get(stage, 0)} for stage in PIPELINE_STAGE_BUCKETS]


def load_pipeline_counts(market: str = "") -> list[dict[str, Any]]:
    connection = get_connection()
    count_queries = {
        "DISCOVERED": "qualification_status = 'DISCOVERED'",
        "QUALIFIED": "qualification_status = 'QUALIFIED'",
        "DISQUALIFIED": "qualification_status = 'DISQUALIFIED'",
        "ELIGIBLE_FOR_AUDIT": "status = 'ELIGIBLE_FOR_AUDIT' OR next_action = 'RUN_AUDIT'",
        "AUDITED": "qualification_status = 'AUDITED'",
        "READY": "audit_data_status = 'READY'",
        "PENDING_REVIEW": "human_review_status = 'PENDING' AND next_action = 'HUMAN_REVIEW'",
        "APPROVED_FOR_OUTREACH": (
            "status = 'APPROVED_FOR_OUTREACH' "
            "OR next_action = 'APPROVED_FOR_OUTREACH' "
            "OR human_review_decision = 'APPROVED'"
        ),
    }
    counts = []
    market_clause, market_params = market_where_clause(market)
    for bucket in PIPELINE_COUNT_BUCKETS:
        clauses = [f"({count_queries[bucket]})"]
        params = list(market_params)
        if market_clause != "1 = 1":
            clauses.append(market_clause)
        row = connection.execute(
            f"SELECT COUNT(*) AS count FROM prospects WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        counts.append({"bucket": bucket, "count": int(row["count"])})
    return counts


def load_run_counts(market: str = "") -> list[dict[str, Any]]:
    keys = [
        ("total", "Total Prospects"),
        ("discovered_new", "Discovered/New"),
        ("qualified_eligible", "Qualified / Eligible"),
        ("ineligible", "Ineligible"),
        ("audited", "Audited"),
        ("audit_ready", "Audit Ready"),
        ("pending_review", "Pending Review"),
        ("approved_for_outreach", "Approved For Outreach"),
        ("outreach_drafted", "Outreach Drafted"),
        ("outreach_sent", "Outreach Sent"),
        ("discarded_rejected", "Discarded/Rejected"),
    ]
    counts = {key: 0 for key, _label in keys}
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_market_filter(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT id, status, qualification_status, audit_data_status,
               human_review_status, human_review_decision, next_action, market
        FROM prospects
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()

    for row in rows:
        stage = compute_pipeline_stage(row)
        status = _normalize_token(row["status"])
        qualification_status = _normalize_token(row["qualification_status"])
        audit_data_status = _normalize_token(row["audit_data_status"])
        next_action = _normalize_token(row["next_action"])

        counts["total"] += 1
        if stage in {"DISCARDED", "REJECTED_REVIEW", "CLOSED_LOST"}:
            bucket = "discarded_rejected"
        elif stage == "INELIGIBLE" or qualification_status == "DISQUALIFIED":
            bucket = "ineligible"
        elif stage == "PENDING_REVIEW":
            bucket = "pending_review"
        elif stage == "APPROVED_FOR_OUTREACH":
            bucket = "approved_for_outreach"
        elif stage == "OUTREACH_DRAFTED":
            bucket = "outreach_drafted"
        elif stage == "OUTREACH_SENT":
            bucket = "outreach_sent"
        elif stage == "AUDIT_READY" or audit_data_status == "READY":
            bucket = "audit_ready"
        elif qualification_status == "AUDITED":
            bucket = "audited"
        elif stage == "ELIGIBLE_FOR_AUDIT" or (
            qualification_status == "QUALIFIED"
            or status == "ELIGIBLE_FOR_AUDIT"
            or next_action == "RUN_AUDIT"
        ):
            bucket = "qualified_eligible"
        elif stage == "NEW" or qualification_status == "DISCOVERED":
            bucket = "discovered_new"
        else:
            bucket = "discovered_new"
        counts[bucket] += 1

    return [{"key": key, "label": label, "count": counts[key]} for key, label in keys]


def load_run_recommended_action(market: str = "", niches: list[str] | None = None) -> dict[str, Any]:
    niches = [str(niche).strip() for niche in (niches or []) if str(niche).strip()]
    base_clauses = ["1 = 1"]
    base_params: list[Any] = []
    append_market_filter(base_clauses, base_params, market)
    if niches:
        placeholders = ", ".join("?" for _ in niches)
        base_clauses.append(f"niche IN ({placeholders})")
        base_params.extend(niches)

    def count_matching(extra_clauses: list[str], extra_params: list[Any] | None = None) -> int:
        clauses = list(base_clauses) + extra_clauses
        params = list(base_params) + list(extra_params or [])
        row = get_connection().execute(
            f"SELECT COUNT(*) AS count FROM prospects WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row["count"] if row else 0)

    counts = {
        "discovered_new": count_matching(["qualification_status = 'DISCOVERED'"]),
        "eligible_for_audit": count_matching(
            [
                "qualification_status = 'QUALIFIED'",
                "status IN ('ELIGIBLE_FOR_AUDIT', 'AUDIT_READY')",
            ]
        ),
        "audit_selectable": count_matching(
            [
                "qualification_status = 'QUALIFIED'",
                "(status IS NULL OR status NOT IN ('INELIGIBLE', 'DISCARDED', 'REJECTED_REVIEW', 'CLOSED_LOST'))",
                "(next_action IS NULL OR next_action NOT IN ('DISCARD', 'DISQUALIFIED'))",
                "("
                "status IN ('ELIGIBLE_FOR_AUDIT', 'AUDIT_READY', 'PENDING_REVIEW') "
                "OR next_action IN ('RUN_AUDIT', 'NEEDS_SITE_AUDIT', 'HUMAN_REVIEW')"
                ")",
                "website_url IS NOT NULL",
                "website_url <> ''",
                "(audit_data_status IS NULL OR audit_data_status <> 'READY')",
            ]
        ),
        "audited_but_unscored": count_matching(
            [
                "EXISTS ("
                "SELECT 1 FROM website_audits "
                "WHERE website_audits.prospect_id = prospects.id "
                "AND website_audits.audit_type = 'site' "
                "AND website_audits.status = 'succeeded'"
                ")",
                "(score_explanation_json IS NULL OR score_explanation_json = '' OR score_explanation_json = '{}')",
            ]
        ),
        "audit_ready_pending_review": count_matching(
            [
                "audit_data_status = 'READY'",
                "human_review_status = 'PENDING'",
                "next_action = 'HUMAN_REVIEW'",
            ]
        ),
        "approved_for_outreach": count_matching(["human_review_decision = 'APPROVED'"]),
        "outreach_drafted": count_matching(["status = 'OUTREACH_DRAFTED'"]),
        "outreach_sent": count_matching(["status = 'OUTREACH_SENT'"]),
    }

    if counts["discovered_new"] > 0 and counts["eligible_for_audit"] == 0:
        action = "Run Eligibility"
    elif counts["audit_selectable"] > 0:
        action = "Run Audit"
    elif counts["audited_but_unscored"] > 0:
        action = "Run Score"
    elif counts["audit_ready_pending_review"] > 0:
        action = "Open Review Queue"
    elif counts["approved_for_outreach"] > 0 and counts["outreach_drafted"] == 0:
        action = "Generate Outreach Drafts"
    else:
        action = "No immediate pipeline action detected for this filter"

    labels = {
        "discovered_new": "Discovered/New",
        "eligible_for_audit": "Eligible For Audit",
        "audit_selectable": "Audit-Selectable Leads",
        "audited_but_unscored": "Audited But Unscored",
        "audit_ready_pending_review": "Pending Review",
        "approved_for_outreach": "Approved For Outreach",
        "outreach_drafted": "Outreach Drafted",
        "outreach_sent": "Outreach Sent",
    }
    return {
        "action": action,
        "counts": [
            {"key": key, "label": labels[key], "count": value}
            for key, value in counts.items()
        ],
        "count_map": counts,
    }


def load_review_queue(market: str = "") -> list[dict[str, Any]]:
    clauses = [
        "audit_data_status = 'READY'",
        "human_review_status = 'PENDING'",
        "next_action = 'HUMAN_REVIEW'",
    ]
    params: list[Any] = []
    append_market_filter(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY expected_close_score DESC, website_pain_score DESC, id
        """,
        params,
    ).fetchall()

    prospects = []
    for row in rows:
        prospect = _row_to_dict(row)
        score_explanation = parse_json_field(prospect.get("score_explanation_json"))
        prospect["score_explanation"] = score_explanation
        prospect["top_reasons"] = score_explanation.get("top_reasons", [])[:3]
        prospect["pipeline_stage"] = compute_pipeline_stage(prospect)
        prospect["artifact_map"] = artifacts_by_type(load_artifacts(prospect["id"]))
        prospects.append(prospect)
    return prospects


def load_group_counts(column: str, limit: int = 8, market: str = "") -> list[dict[str, Any]]:
    if column not in {"market", "niche"}:
        raise ValueError(f"Unsupported group count column: {column}")
    clauses = [f"{column} IS NOT NULL", f"{column} <> ''"]
    params: list[Any] = []
    append_market_filter(clauses, params, market)
    params.append(limit)
    rows = get_connection().execute(
        f"""
        SELECT {column} AS label, COUNT(*) AS count
        FROM prospects
        WHERE {" AND ".join(clauses)}
        GROUP BY {column}
        ORDER BY count DESC, {column}
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def load_distinct_values(column: str, market: str = "") -> list[str]:
    if column not in {"market", "niche"}:
        raise ValueError(f"Unsupported distinct value column: {column}")
    clauses = [f"{column} IS NOT NULL", f"{column} <> ''"]
    params: list[Any] = []
    if column != "market":
        append_market_filter(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT DISTINCT {column} AS value
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY {column}
        """,
        params,
    ).fetchall()
    return [str(row["value"]) for row in rows]


def empty_market_summary_counts() -> dict[str, int]:
    return {
        "total": 0,
        "eligible": 0,
        "pending_review": 0,
        "approved_for_outreach": 0,
        "outreach_sent": 0,
        "contact_made": 0,
        "discarded_ineligible": 0,
    }


def add_stage_to_market_summary(counts: dict[str, int], stage: str) -> None:
    counts["total"] += 1
    if stage == "ELIGIBLE_FOR_AUDIT":
        counts["eligible"] += 1
    elif stage == "PENDING_REVIEW":
        counts["pending_review"] += 1
    elif stage == "APPROVED_FOR_OUTREACH":
        counts["approved_for_outreach"] += 1
    elif stage == "OUTREACH_SENT":
        counts["outreach_sent"] += 1
    elif stage in {
        "CONTACT_MADE",
        "CALL_BOOKED",
        "PROPOSAL_SENT",
        "CLOSED_WON",
        "PROJECT_ACTIVE",
        "PROJECT_COMPLETE",
    }:
        counts["contact_made"] += 1
    elif stage in {"INELIGIBLE", "REJECTED_REVIEW", "DISCARDED", "CLOSED_LOST"}:
        counts["discarded_ineligible"] += 1


def market_summary_key(
    market_value: Any,
    *,
    configured_keys_set: set[str],
    selected_market: str,
) -> str:
    market = str(market_value or "").strip()
    if market in configured_keys_set:
        return market
    if selected_market and selected_market != UNKNOWN_MARKET_VALUE:
        return selected_market
    return UNKNOWN_MARKET_VALUE


def load_market_summary_counts(market: str = "") -> dict[str, dict[str, int]]:
    configured_keys_set = set(configured_market_keys())
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_market_filter(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT market, status, qualification_status, audit_data_status,
               human_review_status, human_review_decision, next_action
        FROM prospects
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        key = market_summary_key(
            row["market"],
            configured_keys_set=configured_keys_set,
            selected_market=market,
        )
        counts = summary.setdefault(key, empty_market_summary_counts())
        add_stage_to_market_summary(counts, compute_pipeline_stage(row))
    return summary


def load_market_summary_rows(market: str = "") -> list[dict[str, Any]]:
    configured = load_configured_markets()
    summary_counts = load_market_summary_counts(market)
    rows: list[dict[str, Any]] = []

    for configured_market in configured:
        key = configured_market["key"]
        if market and market not in {key, UNKNOWN_MARKET_VALUE}:
            continue
        if market == UNKNOWN_MARKET_VALUE:
            continue
        counts = summary_counts.get(key, empty_market_summary_counts())
        rows.append(
            {
                "key": key,
                "label": configured_market["label"],
                "state": configured_market["state"],
                **counts,
            }
        )

    unknown_counts = summary_counts.get(UNKNOWN_MARKET_VALUE)
    if unknown_counts and (unknown_counts["total"] > 0 or market == UNKNOWN_MARKET_VALUE):
        rows.append(
            {
                "key": UNKNOWN_MARKET_VALUE,
                "label": "Unknown/Unconfigured",
                "state": "",
                **unknown_counts,
            }
        )

    if market and market != UNKNOWN_MARKET_VALUE and not any(row["key"] == market for row in rows):
        counts = summary_counts.get(market, empty_market_summary_counts())
        rows.append(
            {
                "key": market,
                "label": f"{market} (unconfigured)",
                "state": "",
                **counts,
            }
        )

    return rows


def load_stage_counts_by_market() -> dict[str, dict[str, Any]]:
    configured_keys_set = set(configured_market_keys())
    rows = get_connection().execute(
        """
        SELECT market, status, qualification_status, audit_data_status,
               human_review_status, human_review_decision, next_action
        FROM prospects
        """
    ).fetchall()
    counts_by_market: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = market_summary_key(
            row["market"],
            configured_keys_set=configured_keys_set,
            selected_market="",
        )
        bucket = counts_by_market.setdefault(
            key,
            {"total": 0, "stages": {stage: 0 for stage in PIPELINE_STAGE_BUCKETS}},
        )
        stage = compute_pipeline_stage(row)
        bucket["total"] += 1
        bucket["stages"][stage] = bucket["stages"].get(stage, 0) + 1
    return counts_by_market


def load_market_manager_rows() -> list[dict[str, Any]]:
    counts_by_market = load_stage_counts_by_market()
    rows = []
    for market in load_configured_markets():
        counts = counts_by_market.get(
            market["key"],
            {"total": 0, "stages": {stage: 0 for stage in PIPELINE_STAGE_BUCKETS}},
        )
        rows.append(
            {
                **market,
                "prospect_count": counts["total"],
                "stage_counts": [
                    {"stage": stage, "count": count}
                    for stage, count in counts["stages"].items()
                    if count
                ],
                "summary": load_market_summary_counts(market["key"]).get(
                    market["key"],
                    empty_market_summary_counts(),
                ),
            }
        )
    return rows


def load_leads(filters: dict[str, Any]) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []

    if filters.get("market"):
        append_market_filter(clauses, params, filters["market"])
    if filters.get("niche"):
        clauses.append("niche = ?")
        params.append(filters["niche"])
    if filters.get("q"):
        pattern = f"%{filters['q']}%"
        clauses.append(
            """
            (
                CAST(id AS TEXT) LIKE ?
                OR business_name LIKE ?
                OR website_url LIKE ?
                OR phone LIKE ?
                OR market LIKE ?
                OR niche LIKE ?
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])

    sql = f"""
        SELECT {", ".join(LEADS_COLUMNS)}
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY expected_close_score DESC, website_pain_score DESC, id
    """
    selected_stage = filters.get("stage")
    if not selected_stage:
        sql += " LIMIT ?"
        params.append(filters.get("limit", DEFAULT_LIMIT))

    rows = [_row_to_dict(row) for row in get_connection().execute(sql, params).fetchall()]
    for row in rows:
        row["pipeline_stage"] = compute_pipeline_stage(row)
        row["metadata"] = parse_json_field(row.get("metadata_json"))
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else {}
        row["franchise_exclusion"] = metadata.get("franchise_exclusion") or {}

    if selected_stage:
        rows = [row for row in rows if row["pipeline_stage"] == selected_stage]

    return rows[: filters.get("limit", DEFAULT_LIMIT)]


def load_crm_columns(market: str = "", recent_limit: int = 8) -> list[dict[str, Any]]:
    prospects = load_crm_prospects(market)
    columns = []
    for stage, label in CRM_STAGES:
        stage_prospects = [
            prospect for prospect in prospects
            if prospect["pipeline_stage"] == stage
        ]
        columns.append(
            {
                "stage": stage,
                "label": label,
                "count": len(stage_prospects),
                "prospects": stage_prospects[:recent_limit],
            }
        )
    return columns


def load_crm_stage_prospects(
    stage: str,
    *,
    market: str = "",
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    prospects = [
        prospect for prospect in load_crm_prospects(market)
        if prospect["pipeline_stage"] == stage
    ]
    return prospects[:limit]


def load_crm_prospects(market: str = "") -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_market_filter(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT {", ".join(LEADS_COLUMNS)}, updated_at
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY updated_at DESC, id DESC
        """,
        params,
    ).fetchall()
    prospects = []
    for row in rows:
        prospect = _row_to_dict(row)
        prospect["pipeline_stage"] = compute_pipeline_stage(prospect)
        prospects.append(prospect)
    return prospects


def load_contacts(prospect_id: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT *
        FROM contacts
        WHERE prospect_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    contacts = []
    for row in rows:
        contact = _row_to_dict(row)
        contact["metadata"] = parse_json_field(contact.get("metadata_json"))
        metadata = contact["metadata"] if isinstance(contact["metadata"], dict) else {}
        contact["is_primary"] = bool(
            metadata.get("primary_email")
            or metadata.get("selected_primary_email")
            or metadata.get("is_primary")
        )
        contacts.append(contact)
    contacts.sort(key=lambda contact: str(contact.get("updated_at") or ""), reverse=True)
    contacts.sort(key=lambda contact: not contact["is_primary"])
    return contacts


def primary_contact_from_contacts(contacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for contact in contacts:
        metadata = contact.get("metadata") if isinstance(contact.get("metadata"), dict) else {}
        if metadata.get("primary_email") or metadata.get("is_primary"):
            return contact
    return contacts[0] if contacts else None


def load_stage_history(prospect_id: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT *
        FROM outreach_events
        WHERE prospect_id = ?
          AND (event_type = 'crm_stage_change' OR channel = 'email')
        ORDER BY created_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    events = []
    for row in rows:
        event = _row_to_dict(row)
        event["metadata"] = parse_json_field(event.get("metadata_json"))
        events.append(event)
    return events


def outbound_filters_from_request(source: Any | None = None) -> dict[str, str]:
    data = source or request.args
    return {
        "market": str(data.get("market") or "").strip(),
        "niche": str(data.get("niche") or "").strip(),
    }


def outbound_url(filters: dict[str, str], *, result: str | None = None) -> str:
    params = {
        key: value
        for key, value in {
            "market": filters.get("market"),
            "niche": filters.get("niche"),
            "result": result,
        }.items()
        if value
    }
    return url_for("outbound", **params)


def outbound_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    text = str(code)
    if text.startswith("error:"):
        return {"status": "error", "message": text.split(":", 1)[1]}
    if text.startswith("queued:"):
        parts = text.split(":")
        created = parts[1] if len(parts) > 1 else "0"
        skipped = parts[2] if len(parts) > 2 else "0"
        return {
            "status": "success",
            "message": (
                f"Created {created} queued send record(s). Skipped {skipped} already queued record(s). "
                "No email was sent."
            ),
        }
    return None


def parse_outbound_queue_limit(value: str | None) -> int:
    if not value:
        return OUTBOUND_DEFAULT_QUEUE_LIMIT
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("Queue size must be a number.") from exc
    if parsed < 1:
        raise ValueError("Queue size must be at least 1.")
    if parsed > OUTBOUND_MAX_QUEUE_LIMIT:
        raise ValueError(f"Queue size cannot exceed {OUTBOUND_MAX_QUEUE_LIMIT}.")
    return parsed


def normalize_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return None
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return None
    return email


def redact_email(value: Any) -> str:
    email = normalize_email(value)
    if not email:
        return ""
    local, domain = email.rsplit("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


def load_public_packet_base_url() -> str:
    value = os.environ.get("PUBLIC_PACKET_BASE_URL")
    if value and value.strip():
        return value.strip().rstrip("/")
    try:
        config = load_yaml_config("outreach.yaml")
    except FileNotFoundError:
        return ""
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    for candidate in (
        defaults.get("public_packet_base_url") if isinstance(defaults, dict) else None,
        config.get("PUBLIC_PACKET_BASE_URL"),
        config.get("public_packet_base_url"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip().rstrip("/")
    return ""


def load_email_infra_status() -> dict[str, Any]:
    path = project_path("runs/latest/email_infra_check.json")
    if not path.is_file():
        return {
            "exists": False,
            "ready": False,
            "summary": {"PASS": 0, "WARN": 0, "FAIL": 1},
            "results": [
                {
                    "category": "infra",
                    "check": "email_infra_check",
                    "status": "FAIL",
                    "detail": "No email infrastructure report found. Run python -m src.email_infra_check first.",
                }
            ],
            "exit_code": None,
            "doc_path": "docs/EMAIL_INFRA_SETUP.md",
        }
    payload = parse_json_field(path.read_text(encoding="utf-8", errors="replace"))
    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list):
        results = []
    summary = {"PASS": 0, "WARN": 0, "FAIL": 0}
    normalized_results = []
    for item in results:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        if status not in summary:
            status = "WARN"
        summary[status] += 1
        normalized_results.append(
            {
                "category": str(item.get("category") or ""),
                "check": str(item.get("check") or ""),
                "status": status,
                "detail": str(item.get("detail") or ""),
            }
        )
    return {
        "exists": True,
        "ready": summary["FAIL"] == 0 and bool(normalized_results),
        "summary": summary,
        "results": normalized_results,
        "exit_code": payload.get("exit_code") if isinstance(payload, dict) else None,
        "doc_path": "docs/EMAIL_INFRA_SETUP.md",
    }


def outbound_where_clause(filters: dict[str, str]) -> tuple[str, list[Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_market_filter(clauses, params, filters.get("market", ""))
    if filters.get("niche"):
        clauses.append("niche = ?")
        params.append(filters["niche"])
    return " AND ".join(clauses), params


def load_outbound_prospects(filters: dict[str, str]) -> list[dict[str, Any]]:
    where_sql, params = outbound_where_clause(filters)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM prospects
        WHERE {where_sql}
          AND {OUTBOUND_APPROVED_SQL}
        ORDER BY expected_close_score DESC, website_pain_score DESC, id
        """,
        params,
    ).fetchall()
    prospects = []
    for row in rows:
        prospect = _row_to_dict(row)
        prospect["metadata"] = parse_json_field(prospect.get("metadata_json"))
        prospects.append(prospect)
    return prospects


def load_outbound_not_approved_group(filters: dict[str, str]) -> dict[str, Any] | None:
    where_sql, params = outbound_where_clause(filters)
    count_row = get_connection().execute(
        f"""
        SELECT COUNT(*) AS count
        FROM prospects
        WHERE {where_sql}
          AND NOT ({OUTBOUND_APPROVED_SQL})
        """,
        params,
    ).fetchone()
    count = int(count_row["count"] if count_row else 0)
    if count == 0:
        return None

    examples = [
        {
            "prospect_id": row["id"],
            "business_name": row["business_name"],
            "market": row["market"],
            "niche": row["niche"],
        }
        for row in get_connection().execute(
            f"""
            SELECT id, business_name, market, niche
            FROM prospects
            WHERE {where_sql}
              AND NOT ({OUTBOUND_APPROVED_SQL})
            ORDER BY updated_at DESC, id DESC
            LIMIT 8
            """,
            params,
        ).fetchall()
    ]
    return {
        "reason": "not approved",
        "count": count,
        "examples": examples,
    }


def load_artifact_map_for_prospects(prospect_ids: list[int]) -> dict[int, dict[str, list[dict[str, Any]]]]:
    if not prospect_ids:
        return {}
    placeholders = ",".join("?" for _ in prospect_ids)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM artifacts
        WHERE prospect_id IN ({placeholders})
          AND artifact_type IN ('email_draft', 'public_packet')
        ORDER BY artifact_type, id
        """,
        prospect_ids,
    ).fetchall()
    result: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        artifact = _row_to_dict(row)
        artifact["metadata"] = parse_json_field(artifact.get("metadata_json"))
        resolved_path = resolve_project_path(artifact.get("path"))
        artifact["file_exists"] = bool(resolved_path and resolved_path.is_file())
        artifact["file_url"] = file_url_for(artifact.get("path"))
        result.setdefault(int(artifact["prospect_id"]), {}).setdefault(
            str(artifact["artifact_type"]),
            [],
        ).append(artifact)
    return result


def load_contact_map_for_prospects(prospect_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not prospect_ids:
        return {}
    placeholders = ",".join("?" for _ in prospect_ids)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM contacts
        WHERE prospect_id IN ({placeholders})
          AND email IS NOT NULL
          AND TRIM(email) <> ''
        ORDER BY updated_at DESC, id DESC
        """,
        prospect_ids,
    ).fetchall()
    contacts_by_prospect: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        contact = _row_to_dict(row)
        contact["email"] = normalize_email(contact.get("email"))
        if not contact["email"]:
            continue
        contact["metadata"] = parse_json_field(contact.get("metadata_json"))
        metadata = contact["metadata"] if isinstance(contact["metadata"], dict) else {}
        contact["is_primary"] = bool(
            metadata.get("primary_email")
            or metadata.get("selected_primary_email")
            or metadata.get("is_primary")
        )
        contacts_by_prospect.setdefault(int(contact["prospect_id"]), []).append(contact)
    for contacts in contacts_by_prospect.values():
        contacts.sort(key=outbound_contact_sort_key)
    return contacts_by_prospect


def outbound_contact_sort_key(contact: dict[str, Any]) -> tuple[int, int, float, int]:
    dashboard_manual = str(contact.get("source") or "") == "dashboard_manual"
    try:
        confidence = float(contact.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        -int(bool(contact.get("is_primary"))),
        -int(dashboard_manual),
        -confidence,
        int(contact.get("id") or 0),
    )


def active_suppressed_emails(emails: list[str]) -> set[str]:
    normalized = sorted({email for email in (normalize_email(email) for email in emails) if email})
    if not normalized:
        return set()
    placeholders = ",".join("?" for _ in normalized)
    rows = get_connection().execute(
        f"""
        SELECT LOWER(TRIM(value)) AS email
        FROM suppression_list
        WHERE LOWER(TRIM(suppression_type)) = 'email'
          AND LOWER(TRIM(value)) IN ({placeholders})
          AND (expires_at IS NULL OR TRIM(expires_at) = '' OR expires_at > ?)
        """,
        [*normalized, utc_now()],
    ).fetchall()
    return {str(row["email"]) for row in rows}


def load_last_email_events(prospect_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not prospect_ids:
        return {}
    placeholders = ",".join("?" for _ in prospect_ids)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM outreach_events
        WHERE prospect_id IN ({placeholders})
          AND channel = 'email'
        ORDER BY created_at DESC, id DESC
        """,
        prospect_ids,
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        prospect_id = int(row["prospect_id"])
        if prospect_id not in result:
            event = _row_to_dict(row)
            event["metadata"] = parse_json_field(event.get("metadata_json"))
            result[prospect_id] = event
    return result


def load_already_sent_prospect_ids(prospect_ids: list[int]) -> set[int]:
    if not prospect_ids:
        return set()
    placeholders = ",".join("?" for _ in prospect_ids)
    rows = get_connection().execute(
        f"""
        SELECT DISTINCT prospect_id
        FROM outreach_events
        WHERE prospect_id IN ({placeholders})
          AND channel = 'email'
          AND campaign_key = ?
          AND (LOWER(event_type) = 'sent' OR LOWER(status) = 'sent')
        """,
        [*prospect_ids, OUTBOUND_DEFAULT_CAMPAIGN],
    ).fetchall()
    return {int(row["prospect_id"]) for row in rows}


def load_active_queue_rows(prospect_ids: list[int]) -> dict[tuple[int, str, str, int], dict[str, Any]]:
    if not prospect_ids:
        return {}
    placeholders = ",".join("?" for _ in prospect_ids)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM outreach_queue
        WHERE prospect_id IN ({placeholders})
          AND status <> 'cancelled'
        ORDER BY created_at DESC, id DESC
        """,
        prospect_ids,
    ).fetchall()
    result: dict[tuple[int, str, str, int], dict[str, Any]] = {}
    for row in rows:
        queue = _row_to_dict(row)
        key = (
            int(queue["prospect_id"]),
            str(queue["email"] or "").lower(),
            str(queue["campaign"] or ""),
            int(queue["step"] or 0),
        )
        result.setdefault(key, queue)
    return result


def first_step_draft(artifacts: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    drafts = artifacts.get("email_draft") or []
    parsed = []
    for draft in drafts:
        draft = dict(draft)
        draft["step"] = email_draft_step(draft)
        parsed.append(draft)
    parsed.sort(key=lambda item: (item.get("step") or 999, item.get("id") or 0))
    for draft in parsed:
        if (draft.get("step") or OUTBOUND_DEFAULT_STEP) == OUTBOUND_DEFAULT_STEP:
            return draft
    return parsed[0] if parsed else None


def public_packet_artifact(artifacts: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    packets = artifacts.get("public_packet") or []
    ready = [
        packet
        for packet in packets
        if str(packet.get("status") or "").lower() == "ready"
    ]
    return ready[-1] if ready else (packets[-1] if packets else None)


def artifact_public_packet_url(packet: dict[str, Any] | None, base_url: str) -> str:
    if not packet:
        return ""
    metadata = packet.get("metadata") if isinstance(packet.get("metadata"), dict) else {}
    relative = str(packet.get("artifact_url") or metadata.get("relative_url") or "").strip()
    if not relative:
        return ""
    if relative.startswith("http://") or relative.startswith("https://"):
        return relative
    if base_url:
        return f"{base_url}/{relative.lstrip('/')}"
    return relative


def artifact_subject(draft: dict[str, Any] | None) -> str:
    if not draft:
        return ""
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    subject = str(metadata.get("subject") or "").strip()
    if subject:
        return subject
    resolved = resolve_project_path(draft.get("path"))
    if resolved and resolved.is_file():
        text = resolved.read_text(encoding="utf-8", errors="replace")
        subject, _body = split_email_draft_text(text)
        return subject
    return ""


def is_approved_for_outbound(prospect: dict[str, Any]) -> bool:
    decision = _normalize_token(prospect.get("human_review_decision"))
    status = _normalize_token(prospect.get("status"))
    return decision == "APPROVED" and status in {"APPROVED_FOR_OUTREACH", "OUTREACH_DRAFTED"}


def build_outbound_row(
    prospect: dict[str, Any],
    *,
    contacts: list[dict[str, Any]],
    artifacts: dict[str, list[dict[str, Any]]],
    suppressed_emails: set[str],
    last_email_event: dict[str, Any] | None,
    already_sent: bool,
    active_queue: dict[tuple[int, str, str, int], dict[str, Any]],
    public_base_url: str,
    infra_ready: bool,
) -> dict[str, Any]:
    contact = contacts[0] if contacts else None
    email = normalize_email(contact.get("email") if contact else None)
    packet = public_packet_artifact(artifacts)
    draft = first_step_draft(artifacts)
    draft_subject = artifact_subject(draft)
    packet_url = artifact_public_packet_url(packet, public_base_url)
    approved = is_approved_for_outbound(prospect)
    contact_ready = bool(email)
    packet_ready = bool(
        packet
        and str(packet.get("status") or "").lower() == "ready"
        and packet_url
    )
    draft_ready = bool(
        draft
        and str(draft.get("status") or "").lower() == "ready"
        and draft.get("file_exists")
        and draft_subject
    )
    suppressed = bool(email and email in suppressed_emails)
    queue_key = (
        int(prospect["id"]),
        email or "",
        OUTBOUND_DEFAULT_CAMPAIGN,
        OUTBOUND_DEFAULT_STEP,
    )
    queue = active_queue.get(queue_key)

    blockers: list[str] = []
    if not approved:
        blockers.append("not approved")
    else:
        if not contact_ready:
            blockers.append("missing contact email")
        if not packet_ready:
            blockers.append("missing public packet")
        if not draft_ready:
            blockers.append("missing draft")
        if suppressed:
            blockers.append("suppressed")
        if already_sent:
            blockers.append("already sent")
        if queue:
            blockers.append("already queued")
        if not infra_ready:
            blockers.append("infra not ready")

    last_status = ""
    if queue:
        last_status = f"queue:{queue.get('status')}"
    elif last_email_event:
        last_status = str(last_email_event.get("status") or last_email_event.get("event_type") or "")

    return {
        "prospect": prospect,
        "prospect_id": prospect["id"],
        "business_name": prospect.get("business_name") or "",
        "market": prospect.get("market") or "",
        "niche": prospect.get("niche") or "",
        "approved": approved,
        "contact": contact,
        "contact_id": contact.get("id") if contact else None,
        "email": email or "",
        "email_redacted": redact_email(email),
        "contact_ready": contact_ready,
        "public_packet": packet,
        "public_packet_ready": packet_ready,
        "public_packet_url": packet_url,
        "public_packet_link": packet_url
        if packet_url.startswith("http://") or packet_url.startswith("https://")
        else "",
        "draft": draft,
        "draft_ready": draft_ready,
        "draft_subject": draft_subject,
        "draft_url": draft.get("file_url") if draft else None,
        "suppressed": suppressed,
        "already_sent": already_sent,
        "queue": queue,
        "last_outreach_status": last_status,
        "blockers": blockers,
        "send_ready": not blockers,
    }


def load_outbound_readiness(filters: dict[str, str]) -> dict[str, Any]:
    infra = load_email_infra_status()
    public_base_url = load_public_packet_base_url()
    prospects = load_outbound_prospects(filters)
    prospect_ids = [int(prospect["id"]) for prospect in prospects]
    contacts_by_prospect = load_contact_map_for_prospects(prospect_ids)
    artifact_map = load_artifact_map_for_prospects(prospect_ids)
    selected_emails = [
        contacts[0]["email"]
        for contacts in contacts_by_prospect.values()
        if contacts and contacts[0].get("email")
    ]
    suppressed_emails = active_suppressed_emails(selected_emails)
    last_events = load_last_email_events(prospect_ids)
    already_sent_ids = load_already_sent_prospect_ids(prospect_ids)
    active_queue = load_active_queue_rows(prospect_ids)

    rows = [
        build_outbound_row(
            prospect,
            contacts=contacts_by_prospect.get(int(prospect["id"]), []),
            artifacts=artifact_map.get(int(prospect["id"]), {}),
            suppressed_emails=suppressed_emails,
            last_email_event=last_events.get(int(prospect["id"])),
            already_sent=int(prospect["id"]) in already_sent_ids,
            active_queue=active_queue,
            public_base_url=public_base_url,
            infra_ready=bool(infra["ready"]),
        )
        for prospect in prospects
    ]

    counts = {
        "approved_for_outreach": sum(1 for row in rows if row["approved"]),
        "contact_ready": sum(1 for row in rows if row["approved"] and row["contact_ready"]),
        "public_packet_ready": sum(1 for row in rows if row["approved"] and row["public_packet_ready"]),
        "draft_ready": sum(1 for row in rows if row["approved"] and row["draft_ready"]),
        "suppressed": sum(1 for row in rows if row["approved"] and row["suppressed"]),
        "already_sent": sum(1 for row in rows if row["approved"] and row["already_sent"]),
        "send_ready": sum(1 for row in rows if row["send_ready"]),
    }
    ready_rows = [row for row in rows if row["send_ready"]]
    blocked_groups = build_blocked_groups(
        rows,
        not_approved_group=load_outbound_not_approved_group(filters),
    )
    return {
        "infra": infra,
        "counts": counts,
        "ready_rows": ready_rows,
        "blocked_groups": blocked_groups,
    }


def build_blocked_groups(
    rows: list[dict[str, Any]],
    *,
    not_approved_group: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    labels = [
        "missing contact email",
        "missing public packet",
        "missing draft",
        "suppressed",
        "already sent",
        "already queued",
        "infra not ready",
    ]
    groups: list[dict[str, Any]] = []
    if not_approved_group:
        groups.append(not_approved_group)
    for label in labels:
        matching = [row for row in rows if label in row["blockers"]]
        if matching:
            groups.append(
                {
                    "reason": label,
                    "count": len(matching),
                    "examples": matching[:8],
                }
            )
    return groups


def queue_duplicate_exists(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    email: str,
    campaign: str,
    step: int,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM outreach_queue
        WHERE prospect_id = ?
          AND LOWER(email) = ?
          AND campaign = ?
          AND step = ?
          AND status <> 'cancelled'
        LIMIT 1
        """,
        (prospect_id, email.lower(), campaign, step),
    ).fetchone()
    return row is not None


def create_step_1_send_queue(filters: dict[str, str], *, limit: int) -> tuple[int, int]:
    readiness = load_outbound_readiness(filters)
    if not readiness["infra"]["ready"]:
        raise ValueError("Email infrastructure is not ready. Run and fix src.email_infra_check first.")

    connection = get_connection()
    created = 0
    skipped = 0
    now = utc_now()
    for row in readiness["ready_rows"]:
        if created >= limit:
            break
        email = normalize_email(row["email"])
        if not email:
            skipped += 1
            continue
        prospect_id = int(row["prospect_id"])
        if queue_duplicate_exists(
            connection,
            prospect_id=prospect_id,
            email=email,
            campaign=OUTBOUND_DEFAULT_CAMPAIGN,
            step=OUTBOUND_DEFAULT_STEP,
        ):
            skipped += 1
            continue
        nonce = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        # Duplicate safety comes from the partial unique index; queue_key is just a row id.
        key_seed = f"{prospect_id}|{email}|{OUTBOUND_DEFAULT_CAMPAIGN}|{OUTBOUND_DEFAULT_STEP}|{nonce}"
        queue_key = f"outbound:{stable_hash(key_seed)[:24]}"
        metadata = {
            "source": "dashboard_outbound",
            "public_packet_url": row.get("public_packet_url"),
            "created_copy": "Queued emails are not sent until you run the send step.",
        }
        connection.execute(
            """
            INSERT INTO outreach_queue (
                queue_key, prospect_id, contact_id, email, campaign, step, status,
                send_after, subject, draft_artifact_id, public_packet_artifact_id,
                created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_key,
                prospect_id,
                row.get("contact_id"),
                email,
                OUTBOUND_DEFAULT_CAMPAIGN,
                OUTBOUND_DEFAULT_STEP,
                row.get("draft_subject"),
                row.get("draft", {}).get("id") if row.get("draft") else None,
                row.get("public_packet", {}).get("id") if row.get("public_packet") else None,
                now,
                now,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        created += 1
    connection.commit()
    return created, skipped


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_project_file(path: str | Path | None) -> Path | None:
    resolved = resolve_project_path(path)
    if resolved is None or not resolved.is_file():
        return None
    try:
        resolved.relative_to(PROJECT_ROOT.resolve(strict=False))
    except ValueError:
        return None
    return resolved


def send_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    text = str(code)
    if text == "test_sent":
        return {
            "status": "success",
            "message": "Sent one test email. No prospect status was touched.",
        }
    if text.startswith("batch:"):
        _prefix, sent, failed, skipped, *_rest = [*text.split(":"), "0", "0", "0"]
        return {
            "status": "success" if failed == "0" else "error",
            "message": (
                f"Batch finished: sent {sent}, failed {failed}, skipped {skipped}. "
                "No automatic retries were scheduled."
            ),
        }
    if text.startswith("error:"):
        return {"status": "error", "message": text.split(":", 1)[1]}
    return None


def parse_send_limit(value: str | None) -> int:
    if value is None or str(value).strip() == "":
        return SEND_DEFAULT_LIMIT
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("Send limit must be a number.") from exc
    if parsed < 1:
        raise ValueError("Send limit must be at least 1.")
    if parsed > SEND_MAX_LIMIT:
        raise ValueError(f"Send limit cannot exceed {SEND_MAX_LIMIT}.")
    return parsed


def load_outreach_defaults() -> dict[str, Any]:
    try:
        config = load_yaml_config("outreach.yaml")
    except FileNotFoundError:
        return {}
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    return defaults if isinstance(defaults, dict) else {}


def env_or_default(env_key: str, default: Any = None) -> str | None:
    value = os.environ.get(env_key)
    if value is None or str(value).strip() == "":
        return default if default in (None, "") else str(default).strip()
    return str(value).strip()


def load_send_config() -> dict[str, Any]:
    defaults = load_outreach_defaults()
    port_value = env_or_default("SMTP_PORT")
    port_configured = bool(port_value)
    port_valid = True
    try:
        port = int(port_value or 587)
        if port < 1 or port > 65535:
            port_valid = False
            port = 587
    except (TypeError, ValueError):
        port_valid = False
        port = 587

    starttls_raw = env_or_default("SMTP_STARTTLS") or env_or_default("OUTREACH_SMTP_STARTTLS")
    starttls = port != 465 if starttls_raw is None else truthy(starttls_raw)
    reply_to_env = str(defaults.get("reply_to_env") or "").strip()
    reply_to = env_or_default(reply_to_env) if reply_to_env else None
    daily_cap_value = defaults.get("daily_cap")
    if daily_cap_value in (None, ""):
        daily_cap_value = defaults.get("max_emails_per_run")
    try:
        daily_cap = int(daily_cap_value)
    except (TypeError, ValueError):
        daily_cap = SEND_DEFAULT_DAILY_CAP
    if daily_cap < 1:
        daily_cap = SEND_DEFAULT_DAILY_CAP

    physical_address = (
        env_or_default("OUTREACH_PHYSICAL_ADDRESS")
        or env_or_default("PHYSICAL_MAILING_ADDRESS")
        or str(defaults.get("physical_address") or "").strip()
    )
    unsubscribe_email = normalize_email(
        env_or_default("OUTREACH_UNSUBSCRIBE_EMAIL")
        or env_or_default("UNSUBSCRIBE_EMAIL")
        or defaults.get("unsubscribe_email")
    )
    unsubscribe_instruction = str(defaults.get("unsubscribe_instruction") or "").strip()
    if not unsubscribe_instruction:
        if unsubscribe_email:
            unsubscribe_instruction = (
                f'To opt out, reply "unsubscribe" or email {unsubscribe_email}.'
            )
        else:
            unsubscribe_instruction = 'To opt out, reply "unsubscribe".'

    from_name = (
        env_or_default("OUTREACH_FROM_NAME")
        or str(defaults.get("from_name") or "").strip()
        or "Local Growth Audit"
    )
    return {
        "smtp_host": env_or_default("SMTP_HOST"),
        "smtp_port": port,
        "smtp_port_configured": port_configured,
        "smtp_port_valid": port_valid,
        "smtp_username": env_or_default("SMTP_USERNAME"),
        "smtp_password": env_or_default("SMTP_PASSWORD"),
        "smtp_starttls": starttls,
        "from_email": normalize_email(env_or_default("OUTREACH_FROM_EMAIL")),
        "from_name": from_name,
        "reply_to": normalize_email(reply_to),
        "business_name": (
            env_or_default("OUTREACH_BUSINESS_NAME")
            or str(defaults.get("business_name") or "").strip()
            or None
        ),
        "physical_address": physical_address or None,
        "unsubscribe_email": unsubscribe_email,
        "unsubscribe_instruction": unsubscribe_instruction,
        "daily_cap": daily_cap,
        "attach_screenshots_default": truthy(defaults.get("attach_screenshots_default")),
    }


def send_config_blockers(config: dict[str, Any], *, require_compliance: bool) -> list[str]:
    blockers = []
    required = {
        "SMTP_HOST": config.get("smtp_host"),
        "SMTP_USERNAME": config.get("smtp_username"),
        "SMTP_PASSWORD": config.get("smtp_password"),
        "OUTREACH_FROM_EMAIL": config.get("from_email"),
        "OUTREACH_FROM_NAME": config.get("from_name"),
    }
    for label, value in required.items():
        if not value:
            blockers.append(f"missing {label}")
    if not config.get("smtp_port_configured"):
        blockers.append("missing SMTP_PORT")
    elif not config.get("smtp_port_valid"):
        blockers.append("invalid SMTP_PORT")
    if require_compliance:
        if not config.get("business_name"):
            blockers.append("missing sender business name")
        if not config.get("physical_address"):
            blockers.append("missing physical mailing address")
        if not config.get("unsubscribe_email"):
            blockers.append("missing unsubscribe email")
    return blockers


def load_send_dashboard_state() -> dict[str, Any]:
    infra = load_email_infra_status()
    config = load_send_config()
    daily_sent_count = send_daily_sent_count(OUTBOUND_DEFAULT_CAMPAIGN)
    queue_rows = load_send_queue_rows(limit=250)
    suppressed_emails = active_suppressed_emails([row.get("email") for row in queue_rows])
    prepared = [
        prepare_send_queue_row(
            row,
            infra_ready=bool(infra["ready"]),
            suppressed_emails=suppressed_emails,
        )
        for row in queue_rows
    ]
    queued_rows = [row for row in prepared if row["queue_status"] == "queued"]
    blocked_rows = [row for row in queued_rows if not row["sendable"]]
    skipped_rows = [row for row in prepared if row["queue_status"] != "queued"]
    return {
        "infra": infra,
        "config": config,
        "daily_sent_count": daily_sent_count,
        "daily_remaining": max(0, int(config["daily_cap"]) - daily_sent_count),
        "queued_rows": queued_rows,
        "blocked_rows": blocked_rows,
        "skipped_rows": skipped_rows,
        "queued_sendable_count": sum(1 for row in queued_rows if row["sendable"]),
        "inbox_sync": load_inbox_sync_summary(),
    }


def load_inbox_sync_summary() -> dict[str, Any]:
    path = project_path(INBOX_SYNC_JSON_PATH)
    if not path.is_file():
        return {
            "exists": False,
            "created_at": None,
            "mode": "",
            "source": "",
            "counts": {
                "matched": 0,
                "unsubscribe": 0,
                "bounce": 0,
                "interested": 0,
                "unknown_reply": 0,
            },
            "rows": [],
        }
    payload = parse_json_field(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(payload, dict):
        payload = {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    return {
        "exists": True,
        "created_at": payload.get("created_at"),
        "mode": payload.get("mode") or "",
        "source": payload.get("source") or "",
        "counts": {
            "matched": int_value(counts.get("matched"), default=0),
            "unsubscribe": int_value(counts.get("unsubscribe"), default=0),
            "bounce": int_value(counts.get("bounce"), default=0),
            "interested": int_value(counts.get("interested"), default=0),
            "unknown_reply": int_value(counts.get("unknown_reply"), default=0),
        },
        "rows": rows[:10],
    }


def send_daily_sent_count(campaign: str) -> int:
    start = datetime.now(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).isoformat()
    row = get_connection().execute(
        """
        SELECT COUNT(*) AS count
        FROM outreach_events
        WHERE channel = 'email'
          AND campaign_key = ?
          AND status = 'sent'
          AND sent_at IS NOT NULL
          AND sent_at >= ?
        """,
        (campaign, start),
    ).fetchone()
    return int(row["count"] if row else 0)


def load_send_queue_rows(*, limit: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT
            q.*,
            p.business_name,
            p.market,
            p.niche,
            p.website_url,
            p.status AS prospect_status,
            p.next_action,
            p.human_review_decision,
            c.email AS contact_email,
            c.metadata_json AS contact_metadata_json,
            d.path AS draft_path,
            d.status AS draft_status,
            d.metadata_json AS draft_metadata_json,
            pp.path AS public_packet_path,
            pp.status AS public_packet_status,
            pp.artifact_url AS public_packet_artifact_url,
            pp.metadata_json AS public_packet_metadata_json
        FROM outreach_queue q
        JOIN prospects p ON p.id = q.prospect_id
        LEFT JOIN contacts c ON c.id = q.contact_id
        LEFT JOIN artifacts d ON d.id = q.draft_artifact_id
        LEFT JOIN artifacts pp ON pp.id = q.public_packet_artifact_id
        WHERE q.step = ?
          AND q.campaign = ?
        ORDER BY
            CASE LOWER(q.status)
              WHEN 'queued' THEN 0
              WHEN 'failed' THEN 1
              WHEN 'skipped' THEN 2
              WHEN 'sent' THEN 3
              ELSE 4
            END,
            q.created_at ASC,
            q.id ASC
        LIMIT ?
        """,
        (OUTBOUND_DEFAULT_STEP, OUTBOUND_DEFAULT_CAMPAIGN, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = _row_to_dict(row)
        item["metadata"] = parse_json_field(item.get("metadata_json"))
        item["contact_metadata"] = parse_json_field(item.get("contact_metadata_json"))
        item["draft_metadata"] = parse_json_field(item.get("draft_metadata_json"))
        item["public_packet_metadata"] = parse_json_field(item.get("public_packet_metadata_json"))
        result.append(item)
    return result


def prepare_send_queue_row(
    row: dict[str, Any],
    *,
    infra_ready: bool,
    suppressed_emails: set[str],
) -> dict[str, Any]:
    queue_status = str(row.get("status") or "").strip().lower()
    email = normalize_email(row.get("email")) or normalize_email(row.get("contact_email"))
    public_packet_url = artifact_public_packet_url(
        {
            "artifact_url": row.get("public_packet_artifact_url"),
            "metadata": row.get("public_packet_metadata") or {},
        },
        load_public_packet_base_url(),
    )
    draft_subject = str(row.get("subject") or "").strip()
    draft_body = ""
    draft_file = safe_project_file(row.get("draft_path"))
    if draft_file:
        raw_draft = draft_file.read_text(encoding="utf-8", errors="replace")
        parsed_subject, draft_body = split_email_draft_text(
            raw_draft,
            row.get("draft_metadata", {}).get("subject"),
        )
        draft_subject = draft_subject or parsed_subject

    blockers: list[str] = []
    if not infra_ready:
        blockers.append("infra not ready")
    if queue_status != "queued":
        blockers.append(f"queue status {queue_status or 'unknown'}")
    if int(row.get("step") or 0) != OUTBOUND_DEFAULT_STEP:
        blockers.append("not step 1")
    if _normalize_token(row.get("human_review_decision")) != "APPROVED":
        blockers.append("not approved")
    if _normalize_token(row.get("prospect_status")) not in {
        "OUTREACH_DRAFTED",
        "APPROVED_FOR_OUTREACH",
    }:
        blockers.append("status not send-compatible")
    if _normalize_token(row.get("next_action")) != "SEND_OUTREACH":
        blockers.append("next action is not SEND_OUTREACH")
    if not email:
        blockers.append("missing email")
    elif email in suppressed_emails:
        blockers.append("suppressed")
    if not row.get("draft_artifact_id"):
        blockers.append("missing draft")
    elif str(row.get("draft_status") or "").lower() != "ready":
        blockers.append("draft not ready")
    elif not draft_file:
        blockers.append("draft file missing")
    elif not draft_subject:
        blockers.append("draft subject missing")
    elif not draft_body:
        blockers.append("draft body missing")
    if not row.get("public_packet_artifact_id"):
        blockers.append("missing public packet")
    elif str(row.get("public_packet_status") or "").lower() != "ready":
        blockers.append("public packet not ready")
    elif not public_packet_url:
        blockers.append("public packet URL missing")
    if email and send_duplicate_exists(
        prospect_id=int(row.get("prospect_id") or 0),
        email=email,
        campaign=str(row.get("campaign") or OUTBOUND_DEFAULT_CAMPAIGN),
        step=int(row.get("step") or 0),
    ):
        blockers.append("already sent")
    send_after = str(row.get("send_after") or "").strip()
    if send_after and send_after > utc_now():
        blockers.append("scheduled for later")

    prepared = dict(row)
    prepared.update(
        {
            "queue_status": queue_status,
            "email": email or "",
            "email_redacted": redact_email(email),
            "public_packet_url": public_packet_url,
            "public_packet_link": public_packet_url
            if public_packet_url.startswith("http://") or public_packet_url.startswith("https://")
            else "",
            "draft_subject": draft_subject,
            "draft_body": draft_body,
            "draft_file": draft_file,
            "blockers": blockers,
            "sendable": not blockers,
            "case_url": url_for("case_file", prospect_id=row.get("prospect_id")),
        }
    )
    if draft_file:
        prepared["draft_url"] = url_for(
            "project_file",
            file_path=draft_file.relative_to(PROJECT_ROOT.resolve(strict=False)).as_posix(),
        )
    else:
        prepared["draft_url"] = None
    return prepared


def send_duplicate_exists(
    *,
    prospect_id: int,
    email: str,
    campaign: str,
    step: int,
) -> bool:
    event_key = send_event_key(prospect_id, email, campaign, step)
    row = get_connection().execute(
        """
        SELECT 1
        FROM outreach_events
        WHERE event_key = ?
          AND channel = 'email'
          AND campaign_key = ?
          AND (LOWER(event_type) = 'sent' OR LOWER(status) = 'sent')
        LIMIT 1
        """,
        (event_key, campaign),
    ).fetchone()
    return row is not None


def send_email_suppressed(connection: sqlite3.Connection, email: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM suppression_list
        WHERE LOWER(TRIM(suppression_type)) = 'email'
          AND LOWER(TRIM(value)) = ?
          AND (expires_at IS NULL OR TRIM(expires_at) = '' OR expires_at > ?)
        LIMIT 1
        """,
        (email.lower(), utc_now()),
    ).fetchone()
    return row is not None


def send_event_key(prospect_id: int, email: str, campaign: str, step: int) -> str:
    return f"{prospect_id}:{campaign}:email:{step}:{email.lower()}"


def send_email_footer(config: dict[str, Any]) -> str:
    lines = ["-- ", str(config.get("from_name") or "Local Growth Audit")]
    if config.get("business_name"):
        lines.append(str(config["business_name"]))
    if config.get("physical_address"):
        lines.append(str(config["physical_address"]))
    if config.get("unsubscribe_instruction"):
        lines.append(str(config["unsubscribe_instruction"]))
    return "\n".join(lines)


def build_dashboard_email_message(
    *,
    to_email: str,
    subject: str,
    body: str,
    config: dict[str, Any],
    attachments: list[Path] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((str(config.get("from_name") or ""), str(config["from_email"])))
    message["To"] = to_email
    if config.get("reply_to"):
        message["Reply-To"] = str(config["reply_to"])
    if config.get("unsubscribe_email"):
        message["List-Unsubscribe"] = f"<mailto:{config['unsubscribe_email']}?subject=unsubscribe>"
    message.set_content(f"{body.rstrip()}\n\n{send_email_footer(config)}\n")

    for path in attachments or []:
        mime_type, _encoding = mimetypes.guess_type(path.name)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return message


def send_smtp_message(message: EmailMessage, config: dict[str, Any]) -> None:
    host = str(config["smtp_host"])
    port = int(config["smtp_port"])
    timeout = 30
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as smtp:
            smtp.login(str(config["smtp_username"]), str(config["smtp_password"]))
            smtp.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if config.get("smtp_starttls") and smtp.has_extn("starttls"):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(str(config["smtp_username"]), str(config["smtp_password"]))
        smtp.send_message(message)


def write_send_test_log(payload: dict[str, Any]) -> None:
    json_path = project_path(SEND_TEST_LOG_JSON_PATH)
    text_path = project_path(SEND_TEST_LOG_TEXT_PATH)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(
        "\n".join(f"{key}: {value}" for key, value in payload.items()) + "\n",
        encoding="utf-8",
    )


def send_dashboard_test_email(recipient: str) -> None:
    config = load_send_config()
    blockers = send_config_blockers(config, require_compliance=False)
    if blockers:
        raise ValueError("SMTP test is blocked: " + ", ".join(blockers))
    message = build_dashboard_email_message(
        to_email=recipient,
        subject="Outbound infrastructure test",
        body=(
            "This is a one-off dashboard infrastructure test. "
            "It is not outreach and did not touch prospect records."
        ),
        config=config,
    )
    send_smtp_message(message, config)
    write_send_test_log(
        {
            "status": "sent",
            "recipient_redacted": redact_email(recipient),
            "created_at": utc_now(),
            "note": "One test email only. No prospect outreach status was changed.",
        }
    )


def send_dashboard_batch(*, limit: int, include_attachments: bool) -> dict[str, int]:
    infra = load_email_infra_status()
    if not infra["ready"]:
        raise ValueError("Latest email infrastructure check has FAIL rows. No email was sent.")
    config = load_send_config()
    config_blockers = send_config_blockers(config, require_compliance=True)
    if config_blockers:
        raise ValueError("Send config is incomplete: " + ", ".join(config_blockers))

    daily_sent = send_daily_sent_count(OUTBOUND_DEFAULT_CAMPAIGN)
    remaining = int(config["daily_cap"]) - daily_sent
    if remaining <= 0:
        raise ValueError(f"Daily cap reached ({config['daily_cap']}). No email was sent.")
    send_limit = min(limit, remaining, SEND_MAX_LIMIT)

    connection = get_connection()
    queue_rows = load_send_queue_rows(limit=250)
    suppressed_emails = active_suppressed_emails([row.get("email") for row in queue_rows])
    prepared = [
        prepare_send_queue_row(row, infra_ready=True, suppressed_emails=suppressed_emails)
        for row in queue_rows
    ]

    sent = 0
    failed = 0
    skipped = 0
    for row in prepared:
        if sent >= send_limit:
            break
        if row["queue_status"] != "queued":
            continue
        if not row["sendable"]:
            mark_queue_skipped(connection, row, blockers=row["blockers"])
            skipped += 1
            connection.commit()
            continue
        if send_email_suppressed(connection, row["email"]):
            blockers = [*row["blockers"], "suppressed"]
            mark_queue_skipped(connection, row, blockers=blockers)
            skipped += 1
            connection.commit()
            continue
        attachments = (
            send_queue_screenshot_attachments(connection, row)
            if include_attachments
            else []
        )
        body = body_with_public_packet(row)
        message = build_dashboard_email_message(
            to_email=row["email"],
            subject=row["draft_subject"],
            body=body,
            config=config,
            attachments=attachments,
        )
        try:
            send_smtp_message(message, config)
        except Exception as exc:
            failed += 1
            mark_queue_failed(connection, row, error_summary=str(exc)[:500])
            connection.commit()
            continue

        sent += 1
        mark_queue_sent(connection, row, attachments=attachments)
        connection.commit()
    return {"sent": sent, "failed": failed, "skipped": skipped}


def body_with_public_packet(row: dict[str, Any]) -> str:
    body = str(row.get("draft_body") or "").strip()
    packet_url = str(row.get("public_packet_url") or "").strip()
    if packet_url and packet_url not in body:
        body = f"{body}\n\nI put the short audit draft here: {packet_url}".strip()
    return body


def queue_row_allows_attachments(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    approved = metadata.get("approved_outreach") if isinstance(metadata, dict) else None
    return truthy(metadata.get("attach_screenshots")) or (
        isinstance(approved, dict) and truthy(approved.get("attach_screenshots"))
    )


def send_queue_screenshot_attachments(
    connection: sqlite3.Connection,
    row: dict[str, Any],
) -> list[Path]:
    if not queue_row_allows_attachments(row):
        return []
    rows = connection.execute(
        """
        SELECT artifact_type, path
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type IN ('screenshot_desktop', 'screenshot_mobile')
          AND status = 'ready'
        ORDER BY CASE artifact_type
          WHEN 'screenshot_desktop' THEN 1
          WHEN 'screenshot_mobile' THEN 2
          ELSE 3
        END
        """,
        (row["prospect_id"],),
    ).fetchall()
    attachments = []
    for artifact in rows[:2]:
        path = resolve_media_path(artifact["path"])
        if path and path.is_file() and path.stat().st_size <= SEND_MAX_ATTACHMENT_BYTES:
            attachments.append(path)
    return attachments


def outreach_event_metadata(
    row: dict[str, Any],
    *,
    status: str,
    attachments: list[Path] | None = None,
    error_summary: str | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    metadata = {
        "source": "dashboard_send",
        "queue_key": row.get("queue_key"),
        "recipient": row.get("email"),
        "recipient_redacted": row.get("email_redacted"),
        "public_packet_url": row.get("public_packet_url"),
        "draft_path": row.get("draft_path"),
        "step": row.get("step"),
        "status": status,
        "attach_screenshots": bool(attachments),
        "attachments": [str(path) for path in attachments or []],
    }
    if error_summary:
        metadata["error"] = error_summary
    if blockers:
        metadata["blockers"] = blockers
    return metadata


def upsert_send_outreach_event(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    event_type: str,
    status: str,
    metadata: dict[str, Any],
    sent_at: str | None = None,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO outreach_events (
            event_key, prospect_id, contact_id, campaign_key, channel,
            event_type, status, subject, body_path, provider_message_id,
            metadata_json, scheduled_for, sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'email', ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            contact_id = excluded.contact_id,
            event_type = excluded.event_type,
            status = excluded.status,
            subject = excluded.subject,
            body_path = excluded.body_path,
            metadata_json = excluded.metadata_json,
            sent_at = excluded.sent_at,
            updated_at = excluded.updated_at
        """,
        (
            send_event_key(
                int(row["prospect_id"]),
                str(row["email"]),
                str(row["campaign"]),
                int(row["step"]),
            ),
            row["prospect_id"],
            row.get("contact_id"),
            row["campaign"],
            event_type,
            status,
            row.get("draft_subject") or row.get("subject"),
            row.get("draft_path"),
            json.dumps(metadata, sort_keys=True),
            sent_at,
            now,
            now,
        ),
    )


def mark_queue_sent(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    attachments: list[Path],
) -> None:
    now = utc_now()
    metadata = outreach_event_metadata(row, status="sent", attachments=attachments)
    connection.execute(
        """
        UPDATE outreach_queue
        SET status = 'sent',
            updated_at = ?,
            metadata_json = ?
        WHERE id = ?
        """,
        (
            now,
            json.dumps(merge_queue_metadata(row, metadata), sort_keys=True),
            row["id"],
        ),
    )
    connection.execute(
        """
        UPDATE prospects
        SET status = 'OUTREACH_SENT',
            next_action = 'WAIT_FOR_REPLY',
            updated_at = ?
        WHERE id = ?
        """,
        (now, row["prospect_id"]),
    )
    upsert_send_outreach_event(
        connection,
        row,
        event_type="sent",
        status="sent",
        metadata=metadata,
        sent_at=now,
    )


def mark_queue_failed(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    error_summary: str,
) -> None:
    now = utc_now()
    metadata = outreach_event_metadata(row, status="failed", error_summary=error_summary)
    connection.execute(
        """
        UPDATE outreach_queue
        SET status = 'failed',
            updated_at = ?,
            metadata_json = ?
        WHERE id = ?
        """,
        (
            now,
            json.dumps(merge_queue_metadata(row, metadata), sort_keys=True),
            row["id"],
        ),
    )
    upsert_send_outreach_event(
        connection,
        row,
        event_type="send_failed",
        status="failed",
        metadata=metadata,
        sent_at=None,
    )


def mark_queue_skipped(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    blockers: list[str],
) -> None:
    now = utc_now()
    metadata = outreach_event_metadata(row, status="skipped", blockers=blockers)
    connection.execute(
        """
        UPDATE outreach_queue
        SET status = 'skipped',
            updated_at = ?,
            metadata_json = ?
        WHERE id = ?
        """,
        (
            now,
            json.dumps(merge_queue_metadata(row, metadata), sort_keys=True),
            row["id"],
        ),
    )


def merge_queue_metadata(row: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    merged = dict(metadata)
    merged.update(updates)
    merged["dashboard_send_updated_at"] = utc_now()
    return merged


def sales_packet_available(prospect: dict[str, Any]) -> bool:
    return str(prospect.get("pipeline_stage") or compute_pipeline_stage(prospect)) in SALES_PACKET_STAGES


def build_sales_packet(
    *,
    prospect: dict[str, Any],
    audits: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    primary_contact: dict[str, Any] | None,
) -> dict[str, Any]:
    audit_map = audits_by_type(audits)
    artifact_map = artifacts_by_type(artifacts)
    score_explanation = prospect.get("score_explanation") or {}
    if not isinstance(score_explanation, dict):
        score_explanation = {}
    lead_score = audit_map.get("lead_score")
    lead_score_findings = lead_score.get("findings") if lead_score else {}
    if isinstance(lead_score_findings, dict):
        if not score_explanation.get("top_reasons") and lead_score_findings.get("top_reasons"):
            score_explanation["top_reasons"] = lead_score_findings["top_reasons"]
        if not score_explanation.get("signals") and lead_score_findings.get("signals"):
            score_explanation["signals"] = lead_score_findings["signals"]
    site_audit = audit_map.get("site")
    site_findings = site_audit.get("findings") if site_audit else {}
    if not isinstance(site_findings, dict):
        site_findings = {}
    visual_review = audit_map.get("visual_review")
    visual_findings = visual_review.get("findings") if visual_review else {}
    if not isinstance(visual_findings, dict):
        visual_findings = {}

    automated_issues = sales_packet_automated_issues(
        audit_map=audit_map,
        score_explanation=score_explanation,
        site_findings=site_findings,
    )
    visual_issues = sales_packet_visual_issues(visual_findings)
    pagespeed = sales_packet_pagespeed(audit_map, score_explanation)
    cta_signals = sales_packet_cta_signals(site_findings)
    sales_points = sales_packet_sales_points(
        automated_issues=automated_issues,
        visual_issues=visual_issues,
        cta_signals=cta_signals,
        pagespeed=pagespeed,
    )
    package = recommend_sales_package(
        prospect=prospect,
        visual_issues=visual_issues,
        pagespeed=pagespeed,
    )

    primary_issue = sales_points[0]["label"] if sales_points else "the stored website findings"
    opening = (
        f"I reviewed {prospect.get('business_name')}'s public website and pulled together "
        "the main items worth discussing. Use the audit evidence below as the call agenda."
    )
    conversion_path = (
        "Frame the conversation around making it easier for a local service buyer to understand "
        "the offer, trust the business, and call or request service without extra friction. Do not "
        "claim lost revenue; keep it to the documented website friction."
    )

    return {
        "artifact_map": artifact_map,
        "automated_issues": automated_issues,
        "visual_issues": visual_issues,
        "pagespeed": pagespeed,
        "cta_signals": cta_signals,
        "sales_points": sales_points,
        "opening": opening,
        "conversion_path": conversion_path,
        "package": package,
        "scope": sales_packet_scope(),
        "objections": sales_packet_objections(primary_issue),
        "response_language": sales_packet_response_language(),
        "sales_notes": sales_notes_from_metadata(prospect),
        "contact_email": primary_contact.get("email") if primary_contact else None,
        "contact_name": primary_contact.get("name") if primary_contact else None,
        "desktop": artifact_map.get("screenshot_desktop"),
        "mobile": artifact_map.get("screenshot_mobile"),
    }


def sales_packet_automated_issues(
    *,
    audit_map: dict[str, dict[str, Any]],
    score_explanation: dict[str, Any],
    site_findings: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    for reason in score_explanation.get("top_reasons") or []:
        if not isinstance(reason, dict) or reason.get("category") != "website_pain":
            continue
        label = str(reason.get("reason") or "").strip()
        if not label:
            continue
        add_sales_issue(
            issues,
            seen,
            key=f"lead_score:{label.lower()}",
            label=label,
            evidence=(
                "lead_score top website_pain reason "
                f"({int_value(reason.get('points'), default=0):+} points)"
            ),
            source="lead_score",
        )

    mobile_score = pagespeed_score(audit_map.get("pagespeed_mobile"))
    if mobile_score is not None and mobile_score < 50:
        add_sales_issue(
            issues,
            seen,
            key="pagespeed_mobile_low",
            label=f"Mobile PageSpeed performance score recorded at {mobile_score}",
            evidence="pagespeed_mobile audit",
            source="pagespeed_mobile",
        )
    desktop_score = pagespeed_score(audit_map.get("pagespeed_desktop"))
    if desktop_score is not None and desktop_score < 60:
        add_sales_issue(
            issues,
            seen,
            key="pagespeed_desktop_low",
            label=f"Desktop PageSpeed performance score recorded at {desktop_score}",
            evidence="pagespeed_desktop audit",
            source="pagespeed_desktop",
        )

    if "tel_links" in site_findings and not list_value(site_findings.get("tel_links")):
        add_sales_issue(
            issues,
            seen,
            key="site_no_tel_links",
            label="Site audit did not find tel links",
            evidence="site audit: tel_links empty",
            source="site",
        )
    if "forms" in site_findings and not list_value(site_findings.get("forms")):
        add_sales_issue(
            issues,
            seen,
            key="site_no_forms",
            label="Site audit did not find forms",
            evidence="site audit: forms empty",
            source="site",
        )
    if "service_page_links" in site_findings and not list_value(site_findings.get("service_page_links")):
        add_sales_issue(
            issues,
            seen,
            key="site_no_service_pages",
            label="Site audit did not find obvious service-page links",
            evidence="site audit: service_page_links empty",
            source="site",
        )
    tracking = site_findings.get("tracking") if isinstance(site_findings.get("tracking"), dict) else {}
    if tracking and not (
        tracking.get("has_ga4_or_gtag")
        or tracking.get("has_gtm")
        or tracking.get("has_facebook_pixel")
    ):
        add_sales_issue(
            issues,
            seen,
            key="site_no_tracking",
            label="Site audit did not detect GA4/GTM/Facebook Pixel tracking",
            evidence="site audit: tracking flags false",
            source="site",
        )
    schema = site_findings.get("schema") if isinstance(site_findings.get("schema"), dict) else {}
    if schema and not ((schema.get("json_ld_count") or 0) > 0 or schema.get("types")):
        add_sales_issue(
            issues,
            seen,
            key="site_no_schema",
            label="Site audit did not detect structured schema markup",
            evidence="site audit: schema empty",
            source="site",
        )

    return issues[:6]


def sales_packet_visual_issues(visual_findings: dict[str, Any]) -> list[dict[str, Any]]:
    raw_issues = visual_findings.get("top_issues") or []
    if not raw_issues and isinstance(visual_findings.get("issues"), dict):
        raw_issues = list(visual_findings["issues"].values())

    issues = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        severity = int_value(raw.get("severity"), default=0)
        present = raw.get("present", True)
        if severity < 3 or present is False:
            continue
        label = str(raw.get("label") or raw.get("category") or "Visual issue").strip()
        note = str(raw.get("note") or "").strip()
        evidence_area = str(raw.get("evidence_area") or "").strip()
        safe_claim = str(raw.get("email_safe_claim") or "").strip()
        evidence = "; ".join(part for part in [evidence_area, note, safe_claim] if part)
        issues.append(
            {
                "label": label,
                "severity": severity,
                "evidence": evidence or f"visual_review severity {severity}/5",
                "source": "visual_review",
            }
        )
    issues.sort(key=lambda issue: issue["severity"], reverse=True)
    return issues[:5]


def sales_packet_pagespeed(
    audit_map: dict[str, dict[str, Any]],
    score_explanation: dict[str, Any],
) -> dict[str, Any]:
    signals = score_explanation.get("signals") if isinstance(score_explanation, dict) else {}
    if not isinstance(signals, dict):
        signals = {}
    mobile = pagespeed_summary(audit_map.get("pagespeed_mobile"), signals, "mobile")
    desktop = pagespeed_summary(audit_map.get("pagespeed_desktop"), signals, "desktop")
    return {"mobile": mobile, "desktop": desktop}


def pagespeed_summary(
    audit: dict[str, Any] | None,
    signals: dict[str, Any],
    strategy: str,
) -> dict[str, Any]:
    findings = audit.get("findings") if audit else {}
    if not isinstance(findings, dict):
        findings = {}
    score = pagespeed_score(audit)
    if score is None:
        score = int_or_none(signals.get(f"{strategy}_pagespeed_score"))
    metrics = findings.get("metrics") if isinstance(findings.get("metrics"), dict) else {}
    metric_rows = []
    for key, label in [
        ("first-contentful-paint", "First contentful paint"),
        ("largest-contentful-paint", "Largest contentful paint"),
        ("interactive", "Time to interactive"),
        ("total-blocking-time", "Total blocking time"),
        ("speed-index", "Speed index"),
    ]:
        metric = metrics.get(key) if isinstance(metrics.get(key), dict) else {}
        if metric.get("display_value"):
            metric_rows.append({"label": label, "value": metric["display_value"]})
    return {
        "score": score,
        "status": audit.get("status") if audit else signals.get(f"{strategy}_pagespeed_status"),
        "source": findings.get("source") or signals.get(f"{strategy}_pagespeed_source"),
        "summary": audit.get("summary") if audit else None,
        "metrics": metric_rows[:5],
    }


def sales_packet_cta_signals(site_findings: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    for key, label in [
        ("tel_links", "Tel links"),
        ("booking_links", "Booking/quote links"),
        ("forms", "Forms"),
        ("contact_page_links", "Contact-page links"),
        ("service_page_links", "Service-page links"),
    ]:
        if key not in site_findings:
            continue
        values = list_value(site_findings.get(key))
        status = "Needs attention" if not values else "Detected"
        signals.append(
            {
                "label": label,
                "count": len(values),
                "status": status,
                "evidence": f"site audit: {key} count {len(values)}",
            }
        )
    return signals


def sales_packet_sales_points(
    *,
    automated_issues: list[dict[str, Any]],
    visual_issues: list[dict[str, Any]],
    cta_signals: list[dict[str, Any]],
    pagespeed: dict[str, Any],
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for issue in visual_issues[:2]:
        candidates.append(
            {
                "label": f"First impression: {issue['label']}",
                "evidence": issue["evidence"],
            }
        )
    for issue in automated_issues:
        candidates.append({"label": issue["label"], "evidence": issue["evidence"]})
    mobile_score = pagespeed.get("mobile", {}).get("score")
    if mobile_score is not None:
        candidates.append(
            {
                "label": f"Mobile buyer experience: PageSpeed recorded {mobile_score}",
                "evidence": "pagespeed_mobile audit",
            }
        )
    for signal in cta_signals:
        if signal["status"] == "Needs attention":
            candidates.append(
                {
                    "label": f"Conversion path: {signal['label']} missing in site audit",
                    "evidence": signal["evidence"],
                }
            )

    unique = []
    seen = set()
    for candidate in candidates:
        key = candidate["label"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
        if len(unique) == 3:
            break
    if not unique:
        unique.append(
            {
                "label": "Use the stored audit as the agenda",
                "evidence": "No high-severity packet issue was available beyond the case file.",
            }
        )
    return unique


def recommend_sales_package(
    *,
    prospect: dict[str, Any],
    visual_issues: list[dict[str, Any]],
    pagespeed: dict[str, Any],
) -> dict[str, Any]:
    niche = str(prospect.get("niche") or "").lower()
    high_ticket = any(keyword in niche for keyword in HIGH_TICKET_NICHE_KEYWORDS)
    pain_score = int_value(prospect.get("website_pain_score"), default=0)
    close_score = int_value(prospect.get("expected_close_score"), default=0)
    max_visual = max((int_value(issue.get("severity"), default=0) for issue in visual_issues), default=0)

    high_severity = pain_score >= 18 or close_score >= 50 or max_visual >= 4
    moderate = high_ticket or pain_score >= 8 or close_score >= 25 or max_visual >= 3

    if high_ticket and high_severity:
        return {
            "tier": "High-ticket local service rebuild",
            "price": "$6,500-$8,500 upfront + $299/mo managed web ops",
            "rationale": [
                "High-ticket niche and high stored pain/visual severity.",
                "Use when the call confirms a full rebuild and ongoing web ops are valuable.",
            ],
        }
    if moderate:
        return {
            "tier": "Standard local service site",
            "price": "$4,500-$6,500 upfront + $199-$299/mo",
            "rationale": [
                "Stored audit shows enough website friction for a focused rebuild conversation.",
                "Use when scope is a core site without heavy custom systems.",
            ],
        }
    return {
        "tier": "Core site monthly",
        "price": "$0 upfront + $399/mo for 12 months",
        "rationale": [
            "Use when upfront budget is the main objection and the call supports a smaller first scope.",
            "Scope limited to homepage, contact/request page, and basic local trust signals.",
        ],
    }


def sales_packet_scope() -> dict[str, list[str]]:
    return {
        "included": [
            "Homepage and core conversion path",
            "Main service pages for the agreed scope",
            "Contact/request-service page",
            "Basic on-page structure and local trust sections",
            "Manual WordPress fulfillment for Phase 1",
        ],
        "excluded": [
            "Ad spend, media buying, or guaranteed lead volume",
            "Legal copy, licensing verification, warranties, or compliance promises",
            "Photography, video, logo redesign, and brand identity systems",
            "Custom CRM, payment processing, or contract automation",
        ],
        "upsells": [
            "Additional service/location pages beyond the agreed scope",
            "Ongoing SEO content, reporting, or call tracking",
            "Booking integrations, advanced forms, or CRM handoff",
            "Copywriting depth, photography coordination, or maintenance beyond the base plan",
        ],
    }


def sales_packet_objections(primary_issue: str) -> list[dict[str, str]]:
    return [
        {
            "objection": "We already have someone.",
            "response": (
                "That may be fine. I am not trying to replace anyone blindly. I found a few specific "
                f"items in the audit, especially {primary_issue.lower()}, and it may be worth having "
                "your current person address them if you are happy with the relationship."
            ),
        },
        {
            "objection": "Send me information.",
            "response": (
                "I can send the short version, but the useful part is the teardown itself. Give me "
                "ten minutes and I will walk through the few points I would fix first."
            ),
        },
        {
            "objection": "How much?",
            "response": (
                "For most local service sites I would expect a defined rebuild range, then optional "
                "monthly web ops if you want someone keeping the site handled. I would confirm scope "
                "before treating that as a proposal."
            ),
        },
        {
            "objection": "We are not interested.",
            "response": (
                "Understood. I will not push it. The only reason I reached out is that the public-site "
                "audit showed fixable friction, not because I know anything about your internal pipeline."
            ),
        },
        {
            "objection": "We do not need a website.",
            "response": (
                "Fair. I would only argue for it if the site is part of how people check you before "
                "calling. The goal would be making that first check easier, not adding something fancy."
            ),
        },
    ]


def sales_packet_response_language() -> list[str]:
    return [
        "Keep it concrete: public website, documented audit, fixable friction.",
        "Avoid claiming they lost leads or that the current site is broken.",
        "Use contractor-owner language: easier to call, clearer service path, stronger first impression.",
        "Position price as a scoped recommendation, not a contract or guarantee.",
    ]


def save_sales_notes(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    notes: str,
) -> None:
    metadata = prospect.get("metadata")
    if not isinstance(metadata, dict):
        metadata = parse_json_field(prospect.get("metadata_json"))
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["sales_notes"] = notes
    metadata["sales_notes_updated_at"] = utc_now()
    connection.execute(
        """
        UPDATE prospects
        SET metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, sort_keys=True), metadata["sales_notes_updated_at"], prospect["id"]),
    )


def sales_notes_from_metadata(prospect: dict[str, Any]) -> str:
    metadata = prospect.get("metadata")
    if not isinstance(metadata, dict):
        metadata = parse_json_field(prospect.get("metadata_json"))
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("sales_notes") or "")


def sales_packet_message_from_code(code: str | None) -> str | None:
    messages = {
        "notes_saved": "Sales packet notes saved.",
        "stage_updated": "CRM stage updated.",
    }
    return messages.get(code or "")


def add_sales_issue(
    issues: list[dict[str, Any]],
    seen: set[str],
    *,
    key: str,
    label: str,
    evidence: str,
    source: str,
) -> None:
    if key in seen:
        return
    seen.add(key)
    issues.append({"label": label, "evidence": evidence, "source": source})


def pagespeed_score(audit: dict[str, Any] | None) -> int | None:
    if not audit:
        return None
    findings = audit.get("findings") if isinstance(audit.get("findings"), dict) else {}
    return int_or_none(findings.get("performance_score") or audit.get("score"))


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def int_value(value: Any, *, default: int = 0) -> int:
    parsed = int_or_none(value)
    return default if parsed is None else parsed


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def artifacts_by_type(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {artifact["artifact_type"]: artifact for artifact in artifacts}


def audits_by_type(audits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {audit["audit_type"]: audit for audit in audits}


def list_unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def review_message_from_code(code: str | None) -> str | None:
    messages = {
        "approved": "Approved for outreach.",
        "approved_missing_email": "Approved for outreach. No primary email was saved.",
        "rejected": "Rejected and discarded from review.",
        "held": "Held for later review.",
        "stage_updated": "CRM stage updated.",
        "contact_saved": "Primary contact saved.",
        "visual_review_saved": "Visual critique saved.",
        "draft_saved": "Outreach draft saved to the local artifact. No email was sent.",
        "outreach_drafts_generated": (
            "Outreach draft job finished. Drafts are local only; no email was sent."
        ),
        "outreach_drafts_failed": "Outreach draft job failed. Review the latest job log.",
        "outreach_drafts_timeout": "Outreach draft job timed out. Review the latest job log.",
    }
    return messages.get(code or "")


def review_queue_message_from_code(code: str | None) -> dict[str, str] | None:
    messages = {
        "quick_deleted": {
            "status": "success",
            "message": "Deleted from the review queue.",
        },
    }
    return messages.get(code or "")


def parse_review_score(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        score = int(value)
    except ValueError as exc:
        raise ValueError("Review score must be a number.") from exc
    if score < 0 or score > 100:
        raise ValueError("Review score must be between 0 and 100.")
    return score


def apply_review_decision(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    action: str,
    score: int | None,
    notes: str | None,
) -> None:
    now = utc_now()
    current = connection.execute(
        """
        SELECT status, next_action
        FROM prospects
        WHERE id = ?
        """,
        (prospect_id,),
    ).fetchone()
    old_status = current["status"] if current else None
    old_next_action = current["next_action"] if current else None
    if action == "approve":
        review_status = "APPROVED"
        review_decision = "APPROVED"
        next_action = "APPROVED_FOR_OUTREACH"
        status = "APPROVED_FOR_OUTREACH"
    elif action == "reject":
        review_status = "REJECTED"
        review_decision = "REJECTED"
        next_action = "REJECTED_BY_REVIEW"
        status = "REJECTED_REVIEW"
    elif action == "hold":
        review_status = "PENDING"
        review_decision = None
        next_action = "HUMAN_REVIEW"
        status = "PENDING_REVIEW"
    else:
        raise ValueError(f"Unsupported review action: {action}")

    connection.execute(
        """
        UPDATE prospects
        SET human_review_status = ?,
            human_review_decision = ?,
            human_review_score = ?,
            human_review_notes = ?,
            human_reviewed_at = ?,
            next_action = ?,
            status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            review_status,
            review_decision,
            score,
            notes,
            now,
            next_action,
            status,
            now,
            prospect_id,
        ),
    )
    if _normalize_token(old_status) != status or _normalize_token(old_next_action) != next_action:
        insert_crm_stage_event(
            connection,
            prospect_id=prospect_id,
            old_status=old_status,
            old_next_action=old_next_action,
            new_status=status,
            next_action=next_action,
            note=f"Review decision: {action}",
            metadata={
                "source": "manual_review",
                "review_action": action,
                "review_status": review_status,
                "human_review_score": score,
            },
        )


def parse_optional_int(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("Expected a numeric id.") from exc


def apply_crm_stage_change(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    new_status: str,
    note: str | None,
) -> None:
    old_status = prospect.get("status")
    old_next_action = prospect.get("next_action")
    next_action = CRM_NEXT_ACTIONS[new_status]
    if (
        _normalize_token(old_status) == new_status
        and _normalize_token(old_next_action) == next_action
    ):
        return

    now = utc_now()

    connection.execute(
        """
        UPDATE prospects
        SET status = ?,
            next_action = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (new_status, next_action, now, prospect["id"]),
    )
    insert_crm_stage_event(
        connection,
        prospect_id=prospect["id"],
        old_status=old_status,
        old_next_action=old_next_action,
        new_status=new_status,
        next_action=next_action,
        note=note,
        metadata={"source": "crm_stage_form"},
    )


def insert_crm_stage_event(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    old_status: Any,
    old_next_action: Any,
    new_status: str,
    next_action: str,
    note: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    event_nonce = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    event_key_seed = f"{old_status}|{new_status}|{note or ''}|{event_nonce}"
    event_key = f"{prospect_id}:crm_stage_change:{stable_hash(event_key_seed)[:16]}"
    event_metadata = {
        "old_status": old_status,
        "old_next_action": old_next_action,
        "new_status": new_status,
        "next_action": next_action,
        "note": note,
    }
    if metadata:
        event_metadata.update(metadata)
    connection.execute(
        """
        INSERT INTO outreach_events (
            event_key, prospect_id, campaign_key, channel, event_type, status,
            metadata_json, created_at, updated_at
        ) VALUES (?, ?, 'crm', 'dashboard', ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            prospect_id,
            state.OutreachEventType.CRM_STAGE_CHANGE,
            state.OutreachEventStatus.RECORDED,
            json.dumps(event_metadata, sort_keys=True),
            now,
            now,
        ),
    )


def upsert_primary_contact(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    contact_id: int | None,
    name: str | None,
    role: str | None,
    email: str | None,
    phone: str | None,
    notes: str | None,
) -> int | None:
    normalized_email = (email or "").strip().lower() or None
    clean_name = (name or "").strip() or None
    clean_role = (role or "").strip() or None
    clean_phone = (phone or "").strip() or None
    clean_notes = (notes or "").strip() or None
    if not any([clean_name, clean_role, normalized_email, clean_phone, clean_notes]):
        return None

    now = utc_now()
    target = None
    if normalized_email:
        target = connection.execute(
            """
            SELECT *
            FROM contacts
            WHERE prospect_id = ?
              AND LOWER(COALESCE(email, '')) = ?
            LIMIT 1
            """,
            (prospect_id, normalized_email),
        ).fetchone()
    if target is None and contact_id is not None:
        target = connection.execute(
            """
            SELECT *
            FROM contacts
            WHERE prospect_id = ?
              AND id = ?
            LIMIT 1
            """,
            (prospect_id, contact_id),
        ).fetchone()

    target_id = int(target["id"]) if target is not None else None
    current_metadata = parse_json_field(target["metadata_json"]) if target is not None else {}
    if not isinstance(current_metadata, dict):
        current_metadata = {}
    current_metadata.update({"primary_email": True, "is_primary": True})
    if clean_notes:
        current_metadata["notes"] = clean_notes
    elif "notes" in current_metadata:
        current_metadata.pop("notes")
    metadata_json = json.dumps(current_metadata, sort_keys=True)

    if target_id is not None:
        existing = _row_to_dict(target)
        connection.execute(
            """
            UPDATE contacts
            SET contact_type = 'business',
                name = ?,
                role = ?,
                email = ?,
                phone = ?,
                source = 'dashboard_manual',
                confidence = ?,
                metadata_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                clean_name if clean_name is not None else existing.get("name"),
                clean_role if clean_role is not None else existing.get("role"),
                normalized_email if normalized_email is not None else existing.get("email"),
                clean_phone if clean_phone is not None else existing.get("phone"),
                0.8 if normalized_email else 0.5,
                metadata_json,
                now,
                target_id,
            ),
        )
    else:
        contact_nonce = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        key_seed = normalized_email or f"{clean_name or ''}|{clean_phone or ''}|{contact_nonce}"
        contact_key = f"dashboard_manual:{prospect_id}:{stable_hash(key_seed)[:16]}"
        connection.execute(
            """
            INSERT INTO contacts (
                contact_key, prospect_id, contact_type, name, role, email, phone,
                source, confidence, metadata_json, created_at, updated_at
            ) VALUES (?, ?, 'business', ?, ?, ?, ?, 'dashboard_manual', ?, ?, ?, ?)
            """,
            (
                contact_key,
                prospect_id,
                clean_name,
                clean_role,
                normalized_email,
                clean_phone,
                0.8 if normalized_email else 0.5,
                metadata_json,
                now,
                now,
            ),
        )
        target_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    demote_other_contacts(connection, prospect_id=prospect_id, primary_contact_id=target_id)
    return target_id


def demote_other_contacts(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    primary_contact_id: int,
) -> None:
    rows = connection.execute(
        """
        SELECT id, metadata_json
        FROM contacts
        WHERE prospect_id = ?
          AND id <> ?
        """,
        (prospect_id, primary_contact_id),
    ).fetchall()
    now = utc_now()
    for row in rows:
        metadata = parse_json_field(row["metadata_json"])
        if not isinstance(metadata, dict):
            metadata = {}
        changed = False
        for key in ("primary_email", "selected_primary_email", "is_primary"):
            if metadata.get(key):
                metadata[key] = False
                changed = True
        if changed:
            connection.execute(
                """
                UPDATE contacts
                SET metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(metadata, sort_keys=True), now, row["id"]),
            )


def upsert_dashboard_contact(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    email: str,
    role: str,
) -> None:
    upsert_primary_contact(
        connection,
        prospect_id=prospect_id,
        contact_id=None,
        name=None,
        role=role,
        email=email,
        phone=None,
        notes=None,
    )


def parse_visual_review_form(form: Any) -> tuple[int | None, dict[str, Any], str]:
    score = parse_review_score(form.get("visual_total_score"))
    issues: dict[str, dict[str, Any]] = {}

    for key, label in VISUAL_REVIEW_CATEGORIES:
        severity = parse_issue_severity(form.get(f"{key}_severity"))
        present = form.get(f"{key}_present") == "1"
        note = str(form.get(f"{key}_note") or "").strip()
        evidence_area = str(form.get(f"{key}_evidence_area") or "").strip()
        claim = VISUAL_REVIEW_CLAIMS[key] if present and severity >= 3 else ""
        issues[key] = {
            "present": present,
            "severity": severity,
            "note": note,
            "email_safe_claim": claim,
            "evidence_area": evidence_area,
            "label": label,
        }

    top_issues = top_visual_issues_from_map(issues)
    findings = {
        "visual_total_score": score,
        "issues": issues,
        "top_issues": top_issues,
        "taxonomy_version": 1,
    }
    return score, findings, summarize_visual_review(top_issues)


def parse_issue_severity(value: str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        severity = int(value)
    except ValueError as exc:
        raise ValueError("Issue severity must be a number.") from exc
    if severity < 0 or severity > 5:
        raise ValueError("Issue severity must be between 0 and 5.")
    return severity


def top_visual_issues_from_map(issues: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        issues.items(),
        key=lambda item: (
            int(item[1].get("severity") or 0),
            -visual_category_index(item[0]),
        ),
        reverse=True,
    )
    top_issues = []
    for key, issue in ordered:
        severity = int(issue.get("severity") or 0)
        if severity < 3 or not issue.get("present"):
            continue
        top_issues.append(
            {
                "category": key,
                "label": issue.get("label") or visual_category_label(key),
                "severity": severity,
                "note": issue.get("note") or "",
                "email_safe_claim": issue.get("email_safe_claim") or "",
                "evidence_area": issue.get("evidence_area") or "",
            }
        )
    return top_issues[:5]


def summarize_visual_review(top_issues: list[dict[str, Any]]) -> str:
    if not top_issues:
        return "Manual visual review did not flag any major visual issues."
    labels = [str(issue["label"]).lower() for issue in top_issues[:3]]
    return f"Manual visual review flagged {human_join(labels)} as the strongest issues."


def human_join(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def visual_category_index(key: str) -> int:
    for index, (category_key, _label) in enumerate(VISUAL_REVIEW_CATEGORIES):
        if category_key == key:
            return index
    return len(VISUAL_REVIEW_CATEGORIES)


def visual_category_label(key: str) -> str:
    for category_key, label in VISUAL_REVIEW_CATEGORIES:
        if category_key == key:
            return label
    return key.replace("_", " ").title()


def run_controls_url_from_form(form: Any, *, result: str) -> str:
    query: dict[str, Any] = {"result": result}
    market = str(form.get("market") or "").strip()
    if market:
        query["market"] = market
    niches = [str(value or "").strip() for value in form.getlist("niches")]
    if not niches:
        niches = [str(value or "").strip() for value in form.getlist("niche")]
    niches = [niche for niche in niches if niche]
    if niches:
        query["niche"] = niches
    audit_mode = str(form.get("audit_mode") or "").strip()
    if audit_mode:
        query["audit_mode"] = audit_mode
    return f"{url_for('run_controls')}?{urlencode(query, doseq=True)}"


def start_job_from_form(form: Any) -> str:
    job_type = str(form.get("job_type") or "").strip()
    if job_type not in dashboard_jobs.ALLOWED_JOBS:
        raise ValueError("Unsupported dashboard job type.")
    if job_type == "full_pipeline":
        raise ValueError("Use the Run page to start a full market pipeline.")

    limit_count = parse_job_limit(form.get("limit_count"))
    is_external = job_type in dashboard_jobs.EXTERNAL_JOB_TYPES
    if job_type == "places_pull" and limit_count is not None and limit_count > PLACES_JOB_LIMIT:
        raise ValueError(f"Places Pull is limited to {PLACES_JOB_LIMIT} rows.")
    if job_type == "audit" and limit_count is not None and limit_count > AUDIT_JOB_LIMIT:
        raise ValueError(f"Audit jobs are limited to {AUDIT_JOB_LIMIT} rows.")

    dry_run = form.get("dry_run") == "1"
    confirmed = form.get("confirm_run") == "1"
    if is_external and not dry_run and not confirmed:
        raise ValueError("Confirm the run before starting a non-dry-run external job.")
    if job_type == "reconcile_statuses" and not dry_run and not confirmed:
        raise ValueError("Confirm apply before reconciling statuses.")

    market = str(form.get("market") or "").strip() or None
    if market == UNKNOWN_MARKET_VALUE:
        raise ValueError("Select a specific configured market before starting a job.")
    niche = str(form.get("niche") or "").strip() or None
    if job_type == "places_pull" and (not market or not niche):
        raise ValueError("Places Pull requires a specific market and niche.")
    if job_type == "audit" and not market and form.get("allow_all_markets") != "1":
        raise ValueError("Audit jobs require a market unless Allow all markets is checked.")

    command_options: dict[str, Any] = {}
    metadata: dict[str, Any] = {"db_path": current_app.config["DATABASE_PATH"]}
    if job_type == "audit":
        audit_mode = str(form.get("audit_mode") or "deep").strip().lower()
        if audit_mode not in {"deep", "fast"}:
            raise ValueError("Choose a valid audit mode.")
        metadata["audit_mode"] = audit_mode
        if audit_mode == "fast":
            command_options["audit_fast"] = True
            command_options["skip_pagespeed"] = True
        metadata["command_options"] = command_options

    job_key = dashboard_jobs.create_job(
        job_type,
        market=market,
        niche=niche,
        limit_count=limit_count,
        dry_run=dry_run,
        metadata=metadata,
    )
    dashboard_jobs.run_job_async(job_key, db_path=current_app.config["DATABASE_PATH"])
    return job_key


def start_full_pipeline_from_form(form: Any) -> str:
    market = str(form.get("market") or "").strip()
    if not market or market == UNKNOWN_MARKET_VALUE:
        raise ValueError("Choose one configured market before starting a full pipeline.")
    if market not in set(configured_market_keys()):
        raise ValueError("Full pipeline requires a market from config/markets.yaml.")

    valid_niches = {niche["key"] for niche in load_configured_niches()}
    niches = [str(value or "").strip() for value in form.getlist("niches")]
    niches = [niche for niche in niches if niche]
    if not niches:
        raise ValueError("Select at least one niche for the full pipeline.")
    unknown_niches = [niche for niche in niches if niche not in valid_niches]
    if unknown_niches:
        raise ValueError(f"Unknown niche: {', '.join(unknown_niches)}.")

    places_limit = parse_job_limit(form.get("per_niche_places_limit")) or 50
    audit_limit = parse_job_limit(form.get("audit_limit")) or 20
    artifact_limit = parse_job_limit(form.get("artifact_limit")) or 25
    if places_limit > PLACES_JOB_LIMIT:
        raise ValueError(f"Places Pull is limited to {PLACES_JOB_LIMIT} rows per niche.")
    if audit_limit > AUDIT_JOB_LIMIT:
        raise ValueError(f"Audit jobs are limited to {AUDIT_JOB_LIMIT} rows.")

    dry_run_all = form.get("dry_run_all") == "1"
    if not dry_run_all and form.get("confirm_run") != "1":
        raise ValueError("Confirm the full pipeline before starting live external calls.")
    audit_mode = str(form.get("audit_mode") or "deep").strip().lower()
    if audit_mode not in {"deep", "fast"}:
        raise ValueError("Choose a valid audit mode.")

    job_key = dashboard_jobs.create_full_pipeline_job(
        market=market,
        niches=niches,
        per_niche_places_limit=places_limit,
        audit_limit=audit_limit,
        artifact_limit=artifact_limit,
        dry_run_all=dry_run_all,
        audit_fast=audit_mode == "fast",
        metadata={"db_path": current_app.config["DATABASE_PATH"]},
    )
    dashboard_jobs.run_job_async(job_key, db_path=current_app.config["DATABASE_PATH"])
    return job_key


def parse_job_limit(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("Limit must be a positive integer.") from exc
    if parsed < 1:
        raise ValueError("Limit must be a positive integer.")
    if parsed > MAX_LIMIT:
        raise ValueError(f"Limit cannot exceed {MAX_LIMIT}.")
    return parsed


def job_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    if str(code).startswith("error:"):
        return {"status": "error", "message": str(code).split(":", 1)[1]}
    return None


def run_dashboard_pipeline_job(form: Any) -> str:
    job_type = str(form.get("job_type") or "").strip()
    if job_type not in PIPELINE_JOBS:
        raise ValueError("Unsupported dashboard pipeline job.")

    limit = parse_pipeline_limit(form.get("limit"))
    if limit > PIPELINE_EXTERNAL_LIMIT and job_type != "score":
        raise ValueError("Limit above 100 is only allowed for the local score job.")

    dry_run = form.get("dry_run") == "1"
    confirmed = form.get("confirm_run") == "1"
    if not dry_run and not confirmed:
        raise ValueError("Confirm the run before executing a non-dry-run job.")

    market = str(form.get("market") or "").strip()
    if market == UNKNOWN_MARKET_VALUE:
        raise ValueError("Select a specific configured market before running a pipeline job.")

    command = build_pipeline_command(
        job_type=job_type,
        market=market,
        niche=str(form.get("niche") or "").strip(),
        limit=limit,
        dry_run=dry_run,
        skip_pagespeed=form.get("skip_pagespeed") == "1",
    )

    if not PIPELINE_JOB_LOCK.acquire(blocking=False):
        raise ValueError("A dashboard pipeline job is already running.")

    try:
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                shell=False,
                timeout=PIPELINE_JOB_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            write_pipeline_job_log(
                title=f"{PIPELINE_JOBS[job_type]['label']} timed out",
                command=command,
                returncode=None,
                stdout=normalize_subprocess_output(exc.stdout),
                stderr=(
                    normalize_subprocess_output(exc.stderr)
                    or f"Timed out after {PIPELINE_JOB_TIMEOUT_SECONDS} seconds."
                ),
            )
            return "timeout"
    finally:
        PIPELINE_JOB_LOCK.release()

    write_pipeline_job_log(
        title=PIPELINE_JOBS[job_type]["label"],
        command=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    return "success" if result.returncode == 0 else "failed"


def parse_outreach_copy_style(value: Any) -> str:
    style = str(value or "owner_friendly").strip()
    if not style:
        style = "owner_friendly"
    if style not in OUTREACH_COPY_STYLES:
        raise ValueError(f"Unsupported outreach style: {style}")
    return style


def parse_variant_index(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        variant_index = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Variant index must be an integer.") from exc
    return max(-1000, min(1000, variant_index))


def parse_outreach_regenerate_steps(value: Any) -> str:
    steps = str(value or "all").strip().lower()
    if steps not in OUTREACH_REGENERATE_STEPS:
        raise ValueError(f"Unsupported draft step scope: {steps}")
    return steps


def normalize_draft_body(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def format_email_draft_text(subject: str, body: str) -> str:
    return f"Subject: {subject.strip()}\n\n{normalize_draft_body(body)}\n"


def load_email_draft_artifact(
    connection: sqlite3.Connection,
    prospect_id: int,
    step: int,
) -> dict[str, Any] | None:
    rows = connection.execute(
        """
        SELECT *
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type = 'email_draft'
        ORDER BY id DESC
        """,
        (prospect_id,),
    ).fetchall()
    expected_key = f"{prospect_id}:email_{step}"
    for row in rows:
        artifact = _row_to_dict(row)
        artifact["metadata"] = parse_json_field(artifact.get("metadata_json"))
        if artifact.get("artifact_key") == expected_key or email_draft_step(artifact) == step:
            return artifact
    return None


def safe_project_write_path(path: str | Path | None) -> Path:
    resolved = resolve_project_path(path)
    if resolved is None:
        raise ValueError("Draft artifact path is missing.")
    try:
        resolved.relative_to(PROJECT_ROOT.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("Draft artifact path is outside the project.") from exc
    return resolved


def save_email_draft_artifact(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    artifact: dict[str, Any],
    step: int,
    subject: str,
    body: str,
    packet_url: str = "",
) -> None:
    artifact_path = artifact.get("path") or f"runs/latest/outreach_drafts/{prospect['id']}/email_{step}.txt"
    output_path = safe_project_write_path(artifact_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = format_email_draft_text(subject, body)
    output_path.write_text(text, encoding="utf-8")

    metadata = parse_json_field(artifact.get("metadata_json"))
    if not isinstance(metadata, dict):
        metadata = {}
    now = utc_now()
    metadata["subject"] = subject
    metadata["step"] = step
    metadata["manual_edited"] = True
    metadata["manual_edited_at"] = now
    metadata["dashboard_saved_at"] = now
    metadata["copy_quality_flags"] = draft_copy_quality_flag_codes(
        subject=subject,
        body=body,
        metadata=metadata,
        prospect=prospect,
        step=step,
        packet_url=str(metadata.get("public_packet_url") or packet_url or ""),
    )

    connection.execute(
        """
        UPDATE artifacts
        SET path = ?,
            content_hash = ?,
            status = 'ready',
            metadata_json = ?,
            updated_at = ?
        WHERE id = ?
          AND prospect_id = ?
          AND artifact_type = 'email_draft'
        """,
        (
            str(artifact_path),
            stable_hash(text),
            json.dumps(metadata, sort_keys=True),
            now,
            artifact["id"],
            prospect["id"],
        ),
    )


def run_case_outreach_draft_job(
    prospect_id: int,
    *,
    force: bool,
    style: str = "owner_friendly",
    variant_index: int = 0,
    steps: str = "all",
) -> str:
    command = [
        sys.executable,
        "-m",
        "src.outreach_drafts",
        "--db-path",
        current_app.config["DATABASE_PATH"],
        "--prospect-id",
        str(prospect_id),
        "--style",
        style,
        "--variant-index",
        str(variant_index),
        "--steps",
        steps,
    ]
    if steps == "1":
        command.append("--first-batch")
    if force:
        command.append("--force")

    if not PIPELINE_JOB_LOCK.acquire(blocking=False):
        raise ValueError("A dashboard job is already running.")

    try:
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                shell=False,
                timeout=OUTREACH_DRAFT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            write_pipeline_job_log(
                title="Generate outreach drafts timed out",
                command=command,
                returncode=None,
                stdout=normalize_subprocess_output(exc.stdout),
                stderr=(
                    normalize_subprocess_output(exc.stderr)
                    or f"Timed out after {OUTREACH_DRAFT_TIMEOUT_SECONDS} seconds."
                ),
            )
            return "outreach_drafts_timeout"
    finally:
        PIPELINE_JOB_LOCK.release()

    write_pipeline_job_log(
        title="Generate outreach drafts",
        command=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    return "outreach_drafts_generated" if result.returncode == 0 else "outreach_drafts_failed"


def build_pipeline_command(
    *,
    job_type: str,
    market: str,
    niche: str,
    limit: int,
    dry_run: bool,
    skip_pagespeed: bool,
) -> list[str]:
    job = PIPELINE_JOBS[job_type]
    command = [
        sys.executable,
        "-m",
        str(job["module"]),
        "--db-path",
        current_app.config["DATABASE_PATH"],
        "--limit",
        str(limit),
    ]
    if market:
        command.extend(["--market", market])
    if niche:
        command.extend(["--niche", niche])
    if dry_run:
        command.append("--dry-run")
    if job_type == "audit" and skip_pagespeed:
        command.append("--skip-pagespeed")
    return command


def parse_pipeline_limit(value: str | None) -> int:
    if value is None or str(value).strip() == "":
        return PIPELINE_DEFAULT_LIMIT
    try:
        limit = int(value)
    except ValueError as exc:
        raise ValueError("Limit must be a positive number.") from exc
    if limit < 1:
        raise ValueError("Limit must be a positive number.")
    if limit > MAX_LIMIT:
        raise ValueError(f"Limit cannot exceed {MAX_LIMIT}.")
    return limit


def load_pipeline_job_log() -> str:
    path = project_path(PIPELINE_LOG_PATH)
    if not path.exists():
        return "No dashboard job has been run yet."
    return path.read_text(encoding="utf-8", errors="replace")[-12000:]


def write_pipeline_job_log(
    *,
    title: str,
    command: list[str],
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> None:
    path = project_path(PIPELINE_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    command_text = " ".join(command) if command else "(not started)"
    text = "\n".join(
        [
            f"Dashboard job: {title}",
            f"Started: {utc_now()}",
            f"Command: {command_text}",
            f"Return code: {returncode if returncode is not None else 'n/a'}",
            "",
            "STDOUT:",
            stdout.strip() or "(empty)",
            "",
            "STDERR:",
            stderr.strip() or "(empty)",
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")


def normalize_subprocess_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def pipeline_result_from_code(code: str | None) -> dict[str, str] | None:
    messages = {
        "success": ("success", "Pipeline job finished successfully."),
        "failed": ("error", "Pipeline job finished with errors. Review the log below."),
        "rejected": ("error", "Pipeline job was not started. Review the log below."),
        "timeout": ("error", "Pipeline job timed out. Review the log below."),
    }
    if not code or code not in messages:
        return None
    status, message = messages[code]
    return {"status": status, "message": message}


def save_visual_review(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    score: int | None,
    findings: dict[str, Any],
    summary: str,
) -> None:
    now = utc_now()
    audit_key = f"{prospect['id']}:visual_review"
    findings_json = json.dumps(findings, sort_keys=True)
    connection.execute(
        """
        INSERT INTO website_audits (
            audit_key, prospect_id, audit_type, url, status, score, summary,
            findings_json, raw_json, audited_at, created_at, updated_at
        ) VALUES (?, ?, 'visual_review', ?, 'reviewed', ?, ?, ?, '{}', ?, ?, ?)
        ON CONFLICT(audit_key) DO UPDATE SET
            url = excluded.url,
            status = 'reviewed',
            score = excluded.score,
            summary = excluded.summary,
            findings_json = excluded.findings_json,
            raw_json = excluded.raw_json,
            audited_at = excluded.audited_at,
            updated_at = excluded.updated_at
        """,
        (
            audit_key,
            prospect["id"],
            prospect.get("website_url"),
            score,
            summary,
            findings_json,
            now,
            now,
            now,
        ),
    )
    connection.execute(
        "UPDATE prospects SET updated_at = ? WHERE id = ?",
        (now, prospect["id"]),
    )


def compute_pipeline_stage(prospect: sqlite3.Row | dict[str, Any]) -> str:
    return state.compute_pipeline_stage(prospect)


def parse_limit(value: str | None) -> int:
    if not value:
        return DEFAULT_LIMIT
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_LIMIT
    if parsed < 1:
        return DEFAULT_LIMIT
    return min(parsed, MAX_LIMIT)


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local lead review dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind. Defaults to 8787.")
    parser.add_argument("--db-path", default=None, help="Override DATABASE_PATH.")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode.")
    return parser


def main() -> int:
    load_env()
    args = build_parser().parse_args()
    db_path = resolve_project_path(args.db_path or get_database_path())
    app = create_app(db_path)
    print(f"Dashboard: http://{args.host}:{args.port}")
    print(f"Database: {db_path}")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
