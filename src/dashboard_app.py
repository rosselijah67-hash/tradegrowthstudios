"""Local Flask dashboard for reviewing and triaging SQLite lead data."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
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
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from email.utils import formataddr
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from flask import Flask, abort, current_app, g, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from flask_login import LoginManager, current_user as flask_current_user, login_required, login_user as flask_login_user, logout_user as flask_logout_user
from markupsafe import Markup, escape

from . import auth as auth_service
from . import contract_exports, contracts as contract_service, dashboard_jobs, db as pipeline_db, docusign_client, quote_exports, quotes as quote_service, state
from . import tasks as task_service
from . import territories
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
USERS_CONFIG_PATH = "config/users.yaml"
UNKNOWN_MARKET_VALUE = "__unknown__"
MARKET_KEY_PATTERN = re.compile(r"^[a-z0-9_]+$")
ADMIN_USERNAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
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
MEDIA_IMPORT_ROOTS = ("screenshots", "artifacts", "runs", "public_outreach")
PUBLIC_AUTH_ENDPOINTS = {
    "login",
    "health",
    "static",
    "public_packet_page",
    "public_packet_asset",
    "docusign_webhook",
}
CSRF_SESSION_KEY = "_csrf_token"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAMES = ("X-CSRF-Token", "X-CSRFToken")
CSRF_EXEMPT_ENDPOINTS = {
    "health",
    "static",
    "public_packet_page",
    "public_packet_asset",
    "docusign_webhook",
}
CSRF_ERROR_MESSAGE = "Security token missing or expired. Refresh the page and try again."
TRASH_MEDIA_RETENTION_DAYS = 3
TRASH_STATUSES = ("INELIGIBLE", "REJECTED_REVIEW", "DISCARDED")
TRASH_QUALIFICATION_STATUSES = ("DISQUALIFIED",)
TRASH_VISIBLE_STATUSES = (*TRASH_STATUSES, "CLOSED_LOST")
TRASH_CATEGORY_OPTIONS = (
    ("all", "All Trash"),
    ("manual_deleted", "Manual Deleted"),
    ("system_deleted", "System Deleted"),
    ("closed_lost", "Closed Lost"),
    ("legacy", "Legacy/Other"),
)
TRASH_MEDIA_ARTIFACT_TYPES = ("screenshot_desktop", "screenshot_mobile")
PROTECTED_JOB_STATUSES = (
    "NO_WEBSITE",
    "INELIGIBLE",
    "REJECTED_REVIEW",
    "DISCARDED",
    "CLOSED_WON",
    "CLOSED_LOST",
    "PROJECT_ACTIVE",
    "PROJECT_COMPLETE",
)
PROTECTED_JOB_NEXT_ACTIONS = ("REJECTED_BY_REVIEW", "COLD_CALL_WEBSITE")

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
    "NO_WEBSITE",
    "QUALIFIED",
    "DISQUALIFIED",
    "ELIGIBLE_FOR_AUDIT",
    "AUDITED",
    "READY",
    "PENDING_REVIEW",
    "APPROVED_FOR_OUTREACH",
]

PIPELINE_JOB_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

PIPELINE_STAGE_BUCKETS = list(state.PIPELINE_STAGE_BUCKETS)

CRM_STAGES = list(state.CRM_STAGES)

CRM_STAGE_LABELS = dict(CRM_STAGES)

CRM_NEXT_ACTIONS = dict(state.CRM_NEXT_ACTIONS)

CRM_BOARD_GROUPS = (
    ("active_conversation", "Active Conversation", ("CONTACT_MADE", "CALL_BOOKED")),
    ("proposal", "Proposal", ("PROPOSAL_SENT",)),
    ("won_lost", "Won/Lost", ("CLOSED_WON", "CLOSED_LOST")),
    ("project", "Project", ("PROJECT_ACTIVE", "PROJECT_COMPLETE")),
    ("inactive", "Inactive", ("DISCARDED", "REJECTED_REVIEW")),
)

CRM_BOARD_STAGE_LABELS = {
    **CRM_STAGE_LABELS,
    "REJECTED_REVIEW": "Rejected Review",
}

SALES_PACKET_STAGES = {"CONTACT_MADE", "CALL_BOOKED", "PROPOSAL_SENT", "CLOSED_WON"}
QUOTE_MARK_SENT_PROTECTED_STATUSES = {
    state.ProspectStatus.CLOSED_WON,
    state.ProspectStatus.CLOSED_LOST,
    state.ProspectStatus.PROJECT_ACTIVE,
    state.ProspectStatus.PROJECT_COMPLETE,
}

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
    "source",
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
    "NO_WEBSITE": "NO_WEBSITE",
    "MISSING_WEBSITE": "NO_WEBSITE",
    "COLD_CALL_WEBSITE": "NO_WEBSITE",
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


def generate_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def get_csrf_token() -> str:
    token = str(session.get(CSRF_SESSION_KEY) or "")
    if not token:
        token = generate_csrf_token()
    return token


def validate_csrf_token(token: str | None) -> bool:
    stored_token = str(session.get(CSRF_SESSION_KEY) or "")
    submitted_token = str(token or "")
    return bool(stored_token and submitted_token and secrets.compare_digest(stored_token, submitted_token))


def csrf_input() -> Markup:
    return Markup(
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{escape(get_csrf_token())}">'
    )


def csrf_token_from_request() -> str | None:
    token = request.form.get(CSRF_FIELD_NAME)
    if token:
        return token
    for header_name in CSRF_HEADER_NAMES:
        token = request.headers.get(header_name)
        if token:
            return token
    return None


def csrf_failure_response():
    return make_response(CSRF_ERROR_MESSAGE, 400)


def resolve_project_path(path: str | Path | None) -> Path | None:
    """Resolve a local path relative to the project root."""

    if path is None:
        return None
    raw_path = Path(path)
    if raw_path.is_absolute():
        return raw_path.resolve(strict=False)
    return project_path(raw_path).resolve(strict=False)


MEDIA_ROOT_MAP = {
    directory: (PROJECT_ROOT / directory).resolve(strict=False)
    for directory in ("screenshots", "artifacts", "runs")
}
MEDIA_ROOTS = tuple(MEDIA_ROOT_MAP.values())


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
    for root_name, media_root in MEDIA_ROOT_MAP.items():
        try:
            tail_path = resolved.relative_to(media_root)
        except ValueError:
            continue
        relative_path = Path(root_name) / tail_path
        return url_for("project_media", relative_path=relative_path.as_posix())
    return None


def markets_config_path() -> Path:
    return project_path(MARKETS_CONFIG_PATH)


def users_config_path() -> Path:
    return project_path(USERS_CONFIG_PATH)


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
        raw_state = raw.get("state")
        state_code = territories.normalize_state(raw_state)
        configured.append(
            {
                "key": str(key),
                "label": str(raw.get("label") or key),
                "state": state_code or str(raw_state or "").strip(),
                "state_warning": state_code is None,
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


def territory_denial_message() -> str:
    return territories.territory_error_message()


def abort_territory_denied() -> None:
    abort(make_response(territory_denial_message(), 403))


def current_dashboard_user() -> auth_service.User | None:
    return auth_service.current_app_user()


def dashboard_user_is_admin(user: auth_service.User | None = None) -> bool:
    return auth_service.is_admin(user if user is not None else current_dashboard_user())


def dashboard_permission_map(user: auth_service.User | None = None) -> dict[str, bool]:
    user = user if user is not None else current_dashboard_user()
    return {
        key: auth_service.user_has_permission(user, key)
        for key in auth_service.USER_PERMISSION_KEYS
    }


def dashboard_user_has_permission(permission_key: str, user: auth_service.User | None = None) -> bool:
    return auth_service.user_has_permission(user if user is not None else current_dashboard_user(), permission_key)


def require_dashboard_permission(permission_key: str):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not dashboard_user_has_permission(permission_key):
                abort(403)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def current_user_allowed_states(
    user: auth_service.User | None = None,
) -> tuple[str, ...]:
    user = user if user is not None else current_dashboard_user()
    if user is None or dashboard_user_is_admin(user):
        return ()
    return tuple(
        state_code
        for state_code in (territories.normalize_state(value) for value in user.allowed_states)
        if state_code
    )


def current_user_can_access_state(state_code: str | None, user: auth_service.User | None = None) -> bool:
    return auth_service.user_can_access_state(
        user if user is not None else current_dashboard_user(),
        state_code,
    )


def visible_configured_markets(user: auth_service.User | None = None) -> list[dict[str, Any]]:
    user = user if user is not None else current_dashboard_user()
    markets = load_configured_markets()
    if dashboard_user_is_admin(user):
        return markets
    return [
        market
        for market in markets
        if not market.get("state_warning")
        and current_user_can_access_state(str(market.get("state") or ""), user)
    ]


def visible_market_keys(user: auth_service.User | None = None) -> list[str]:
    return [market["key"] for market in visible_configured_markets(user)]


def accessible_market_keys_for_current_user(
    user: auth_service.User | None = None,
) -> list[str]:
    return visible_market_keys(user)


def configured_market_state(market_key: str | None) -> str | None:
    if not market_key:
        return None
    return territories.get_market_state(market_key, load_markets_document())


def current_user_can_access_market(
    market_key: str | None,
    user: auth_service.User | None = None,
) -> bool:
    if not market_key or market_key == UNKNOWN_MARKET_VALUE:
        return False
    return current_user_can_access_state(configured_market_state(market_key), user)


def normalize_selected_market_for_user(
    selected_market: str,
    *,
    allow_unknown_for_admin: bool = False,
    user: auth_service.User | None = None,
) -> tuple[str, str]:
    selected_market = str(selected_market or "").strip()
    if not selected_market:
        return "", ""

    user = user if user is not None else current_dashboard_user()
    if selected_market == UNKNOWN_MARKET_VALUE:
        if allow_unknown_for_admin and dashboard_user_is_admin(user):
            return selected_market, ""
        return "", territory_denial_message()

    if dashboard_user_is_admin(user):
        return selected_market, ""
    if current_user_can_access_market(selected_market, user):
        return selected_market, ""
    return "", territory_denial_message()


def market_visibility_clause(user: auth_service.User | None = None) -> tuple[str, list[Any]]:
    user = user if user is not None else current_dashboard_user()
    if dashboard_user_is_admin(user):
        return "1 = 1", []
    keys = visible_market_keys(user)
    if not keys:
        return "1 = 0", []
    placeholders = ", ".join("?" for _ in keys)
    return f"market IN ({placeholders})", list(keys)


def sql_column(table_alias: str | None, column_name: str) -> str:
    if not table_alias:
        return column_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_alias):
        raise ValueError("table_alias must be a simple SQL identifier.")
    return f"{table_alias}.{column_name}"


def prospect_scope_clause(
    table_alias: str | None = "prospects",
    *,
    user: auth_service.User | None = None,
) -> tuple[str, list[Any]]:
    user = user if user is not None else current_dashboard_user()
    if dashboard_user_is_admin(user):
        return "1 = 1", []

    allowed_states = list(current_user_allowed_states(user))
    if not allowed_states:
        return "1 = 0", []

    state_col = sql_column(table_alias, "market_state")
    market_col = sql_column(table_alias, "market")
    raw_state_col = sql_column(table_alias, "state")
    state_guess_col = sql_column(table_alias, "state_guess")
    state_placeholders = ", ".join("?" for _ in allowed_states)
    clauses = [
        f"UPPER(COALESCE({state_col}, '')) IN ({state_placeholders})",
    ]
    params: list[Any] = list(allowed_states)

    market_keys = accessible_market_keys_for_current_user(user)
    if market_keys:
        market_placeholders = ", ".join("?" for _ in market_keys)
        clauses.append(
            "("
            f"TRIM(COALESCE({state_col}, '')) = '' "
            f"AND {market_col} IN ({market_placeholders})"
            ")"
        )
        params.extend(market_keys)

    configured_keys = configured_market_keys()
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


def apply_prospect_scope(
    sql_or_clauses: str | list[str],
    params: list[Any],
    table_alias: str | None = "prospects",
    *,
    user: auth_service.User | None = None,
) -> tuple[str, list[Any]] | None:
    clause, clause_params = prospect_scope_clause(table_alias, user=user)
    if clause == "1 = 1":
        if isinstance(sql_or_clauses, str):
            return sql_or_clauses, params
        return None
    if isinstance(sql_or_clauses, list):
        sql_or_clauses.append(clause)
        params.extend(clause_params)
        return None
    params.extend(clause_params)
    return f"({sql_or_clauses}) AND {clause}", params


def append_visible_market_scope(
    clauses: list[str],
    params: list[Any],
    selected_market: str = "",
    *,
    user: auth_service.User | None = None,
) -> None:
    append_market_filter(clauses, params, selected_market)
    user = user if user is not None else current_dashboard_user()
    if (
        selected_market
        and not dashboard_user_is_admin(user)
        and (
            selected_market == UNKNOWN_MARKET_VALUE
            or not current_user_can_access_market(selected_market, user)
        )
    ):
        clauses.append("1 = 0")
        return
    apply_prospect_scope(clauses, params, "prospects", user=user)


def record_value(record: dict[str, Any] | sqlite3.Row | None, key: str) -> Any:
    if record is None:
        return None
    if isinstance(record, sqlite3.Row):
        return record[key] if key in record.keys() else None
    if isinstance(record, dict):
        return record.get(key)
    return None


def prospect_state_from_record(record: dict[str, Any] | sqlite3.Row | None) -> str | None:
    for key in ("market_state", "canonical_state"):
        state_code = territories.normalize_state(record_value(record, key))
        if state_code:
            return state_code
    market_state = configured_market_state(str(record_value(record, "market") or "").strip())
    if market_state:
        return market_state
    for key in ("state", "state_guess"):
        state_code = territories.normalize_state(record_value(record, key))
        if state_code:
            return state_code
    return None


def current_user_can_access_prospect_record(
    prospect: dict[str, Any] | sqlite3.Row | None,
    user: auth_service.User | None = None,
) -> bool:
    user = user if user is not None else current_dashboard_user()
    if dashboard_user_is_admin(user):
        return prospect is not None
    return current_user_can_access_state(prospect_state_from_record(prospect), user)


def require_prospect_access(prospect_id: int) -> dict[str, Any]:
    prospect = load_prospect(prospect_id)
    if prospect is None:
        abort(404)
    if not current_user_can_access_prospect_record(prospect):
        abort_territory_denied()
    return prospect


def require_quote_access(quote_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    quote = quote_service.get_quote(get_connection(), quote_id)
    if quote is None:
        abort(404)
    prospect = require_prospect_access(int(quote["prospect_id"]))
    return quote, prospect


def require_contract_access(
    contract_id: int,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    contract = contract_service.load_contract(get_connection(), contract_id)
    if contract is None:
        abort(404)
    prospect = require_prospect_access(int(contract["prospect_id"]))
    quote = None
    if contract.get("quote_id") is not None:
        quote = quote_service.get_quote(get_connection(), int(contract["quote_id"]))
        if quote is not None and int(quote["prospect_id"]) != int(contract["prospect_id"]):
            abort(404)
    return contract, quote, prospect


def require_queue_access(queue_id: int | str) -> dict[str, Any]:
    row = get_connection().execute(
        """
        SELECT q.*, p.market AS prospect_market, p.market_state AS prospect_market_state,
               p.state AS prospect_state, p.state_guess AS prospect_state_guess
        FROM outreach_queue q
        JOIN prospects p ON p.id = q.prospect_id
        WHERE q.id = ? OR q.queue_key = ?
        LIMIT 1
        """,
        (queue_id, str(queue_id)),
    ).fetchone()
    if row is None:
        abort(404)
    queue = _row_to_dict(row)
    prospect = {
        "id": queue.get("prospect_id"),
        "market": queue.get("prospect_market"),
        "market_state": queue.get("prospect_market_state"),
        "state": queue.get("prospect_state"),
        "state_guess": queue.get("prospect_state_guess"),
    }
    if not current_user_can_access_prospect_record(prospect):
        abort_territory_denied()
    return queue


def build_market_options(
    selected_market: str = "",
    user: auth_service.User | None = None,
) -> list[dict[str, Any]]:
    user = user if user is not None else current_dashboard_user()
    configured = visible_configured_markets(user)
    options = [
        {
            "value": market["key"],
            "label": market_option_label(market),
            "can_run": True,
        }
        for market in configured
    ]
    option_values = {option["value"] for option in options}
    if (
        selected_market
        and selected_market not in option_values
        and selected_market != UNKNOWN_MARKET_VALUE
        and dashboard_user_is_admin(user)
    ):
        options.append(
            {
                "value": selected_market,
                "label": f"{selected_market} (unconfigured)",
                "can_run": True,
            }
        )
        option_values.add(selected_market)
    if dashboard_user_is_admin(user) and has_unconfigured_market_records(configured_market_keys()):
        options.append(
            {
                "value": UNKNOWN_MARKET_VALUE,
                "label": "Unknown/Unconfigured",
                "can_run": False,
            }
        )
    return options


def admin_user_diagnostics() -> list[dict[str, Any]]:
    users = auth_service.load_auth_users()
    markets = load_configured_markets()
    return [
        {
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "allowed_states": dashboard_allowed_states_summary(user),
            "allowed_states_list": list(user.allowed_states),
            "allowed_states_input": "" if "*" in user.allowed_states else ", ".join(user.allowed_states),
            "password_hash_env": user.password_hash_env,
            "password_hash_present": user.can_login,
            "permissions": dashboard_permission_map(user),
            "owned_market_count": admin_owned_market_count(user, markets),
            **admin_user_prospect_counts(user),
        }
        for user in users.values()
    ]


def admin_owned_market_count(
    user: auth_service.User,
    markets: list[dict[str, Any]],
) -> int:
    if auth_service.is_admin(user) or "*" in user.allowed_states:
        return len(markets)
    allowed_states = set(territories.normalize_state_list(user.allowed_states))
    return sum(
        1
        for market in markets
        if not market.get("state_warning")
        and territories.normalize_state(market.get("state")) in allowed_states
    )


def admin_user_prospect_counts(user: auth_service.User) -> dict[str, int]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    apply_prospect_scope(clauses, params, "prospects", user=user)
    rows = get_connection().execute(
        f"""
        SELECT status, qualification_status, audit_data_status,
               human_review_status, human_review_decision, next_action,
               market, market_state, state, state_guess
        FROM prospects
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    counts = {
        "prospect_count": 0,
        "pending_review_count": 0,
        "outreach_drafted_count": 0,
        "outreach_sent_count": 0,
    }
    for row in rows:
        counts["prospect_count"] += 1
        stage = compute_pipeline_stage(row)
        if stage == "PENDING_REVIEW":
            counts["pending_review_count"] += 1
        elif stage == "OUTREACH_DRAFTED":
            counts["outreach_drafted_count"] += 1
        elif stage == "OUTREACH_SENT":
            counts["outreach_sent_count"] += 1
    return counts


def admin_user_config_rows() -> list[dict[str, Any]]:
    users = admin_user_diagnostics()
    return sorted(users, key=lambda user: (user["role"] != "admin", user["username"]))


def admin_non_admin_user_options() -> list[dict[str, str]]:
    config = load_users_document()
    options = []
    for username, user in config.get("users", {}).items():
        if str(user.get("role") or "").strip().lower() == "admin":
            continue
        options.append(
            {
                "username": str(username),
                "label": str(user.get("display_name") or username),
            }
        )
    return sorted(options, key=lambda item: item["username"])


def admin_state_assignment_rows() -> list[dict[str, Any]]:
    diagnostics = admin_territory_diagnostics()
    by_state = {row["state"]: row for row in diagnostics["territory_rows"]}
    rows = []
    for state_code in sorted(territories.STATE_CODE_TO_NAME):
        row = by_state.get(state_code, {})
        rows.append(
            {
                "state": state_code,
                "state_name": territories.STATE_CODE_TO_NAME.get(state_code, state_code),
                "owner_username": str(row.get("owner_username") or "").split(", ")[0],
                "markets": row.get("markets", []),
                "prospect_count": int(row.get("prospect_count") or 0),
                "conflicts": row.get("conflicts", []),
            }
        )
    return rows


def normalize_admin_username(value: Any) -> str:
    username = str(value or "").strip().upper()
    if not ADMIN_USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Username must be 2-32 chars: A-Z, 0-9, underscore, starting with a letter.")
    return username


def default_password_hash_env(username: str) -> str:
    safe_username = re.sub(r"[^A-Z0-9_]+", "_", username.strip().upper()).strip("_")
    return f"AUTH_{safe_username}_PASSWORD_HASH"


def build_admin_user_details(
    username: str,
    *,
    display_name: Any,
    role: Any,
    allowed_states: Any,
    password_hash_env: Any,
    permissions: dict[str, bool] | None = None,
) -> dict[str, Any]:
    username = normalize_admin_username(username)
    display_name = str(display_name or username).strip() or username
    role = str(role or "user").strip().lower()
    if role not in {"admin", "user"}:
        raise ValueError("Role must be admin or user.")

    password_hash_env = str(password_hash_env or "").strip().upper()
    if not password_hash_env:
        password_hash_env = default_password_hash_env(username)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", password_hash_env):
        raise ValueError("Password hash env var must look like AUTH_USERNAME_PASSWORD_HASH.")

    if role == "admin":
        allowed_states = ["*"]
    else:
        allowed_states = [
            state_code
            for state_code in territories.normalize_state_list(allowed_states)
            if state_code != "*"
        ]

    normalized_permissions = dict(auth_service.DEFAULT_USER_PERMISSIONS)
    if permissions is not None:
        normalized_permissions.update(
            {
                key: bool(permissions.get(key))
                for key in auth_service.USER_PERMISSION_KEYS
            }
        )
    if role == "admin":
        normalized_permissions = dict(auth_service.DEFAULT_USER_PERMISSIONS)

    return {
        "role": role,
        "display_name": display_name,
        "allowed_states": allowed_states,
        "password_hash_env": password_hash_env,
        "permissions": normalized_permissions,
    }


def parse_admin_user_form(form: Any, *, existing_username: str | None = None) -> tuple[str, dict[str, Any]]:
    username = normalize_admin_username(existing_username or form.get("username"))
    return username, build_admin_user_details(
        username,
        display_name=form.get("display_name"),
        role=form.get("role"),
        allowed_states=form.get("allowed_states"),
        password_hash_env=form.get("password_hash_env"),
        permissions=admin_permissions_from_form(form, "permission", force=False),
    )


def admin_permissions_from_form(form: Any, prefix: str, *, force: bool = True) -> dict[str, bool] | None:
    if not force and not any(f"{prefix}_{key}" in form for key in auth_service.USER_PERMISSION_KEYS):
        return None
    return {
        key: str(form.get(f"{prefix}_{key}") or "").strip() == "1"
        for key in auth_service.USER_PERMISSION_KEYS
    }


def validate_admin_users_document(data: dict[str, Any]) -> dict[str, Any]:
    users = data.get("users")
    if not isinstance(users, dict):
        raise ValueError("Users document must contain a users mapping.")
    normalized = {"users": {}}
    admin_count = 0
    for raw_username, raw_user in users.items():
        username = normalize_admin_username(raw_username)
        if not isinstance(raw_user, dict):
            raise ValueError(f"User {username} must be a mapping.")
        role = str(raw_user.get("role") or "user").strip().lower()
        if role not in {"admin", "user"}:
            raise ValueError(f"User {username} has invalid role {role!r}.")
        if role == "admin":
            admin_count += 1
            allowed_states = ["*"]
        else:
            allowed_states = [
                state_code
                for state_code in territories.normalize_state_list(raw_user.get("allowed_states"))
                if state_code != "*"
            ]
        password_hash_env = str(raw_user.get("password_hash_env") or "").strip().upper()
        if not password_hash_env:
            password_hash_env = default_password_hash_env(username)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", password_hash_env):
            raise ValueError(f"User {username} has invalid password hash env var.")
        permissions = dict(auth_service.DEFAULT_USER_PERMISSIONS)
        raw_permissions = raw_user.get("permissions")
        if isinstance(raw_permissions, dict):
            for key in auth_service.USER_PERMISSION_KEYS:
                if key in raw_permissions:
                    permissions[key] = bool(raw_permissions.get(key))
        elif raw_permissions is not None:
            raise ValueError(f"User {username} permissions must be a mapping.")
        if role == "admin":
            permissions = dict(auth_service.DEFAULT_USER_PERMISSIONS)
        normalized["users"][username] = {
            "role": role,
            "display_name": str(raw_user.get("display_name") or username).strip() or username,
            "allowed_states": allowed_states,
            "password_hash_env": password_hash_env,
            "permissions": permissions,
        }
    if admin_count < 1:
        raise ValueError("At least one admin user is required.")
    conflicts = territories.validate_exclusive_territories(normalized)
    if conflicts:
        states = ", ".join(str(conflict.get("state")) for conflict in conflicts)
        raise ValueError(f"Territory conflict detected for: {states}")
    return normalized


def save_admin_users_document(data: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_admin_users_document(data)
    write_users_document(normalized)
    return normalized


def build_admin_save_all_document(form: Any) -> tuple[dict[str, Any], str | None]:
    current_config = load_users_document()
    current_users = current_config.get("users", {})
    if not isinstance(current_users, dict):
        raise ValueError("Users document must contain a users mapping.")

    updated: dict[str, Any] = {"users": {}}
    existing_usernames: list[str] = []
    for raw_username in form.getlist("existing_username"):
        username = normalize_admin_username(raw_username)
        if username not in current_users:
            raise ValueError(f"User {username} no longer exists.")
        if username in existing_usernames:
            continue
        existing_usernames.append(username)

    for username in existing_usernames:
        current_user = current_users.get(username) if isinstance(current_users.get(username), dict) else {}
        user_details = build_admin_user_details(
            username,
            display_name=form.get(f"user_{username}_display_name"),
            role=form.get(f"user_{username}_role"),
            allowed_states=current_user.get("allowed_states"),
            password_hash_env=form.get(f"user_{username}_password_hash_env"),
            permissions=admin_permissions_from_form(form, f"user_{username}_permission"),
        )
        if user_details["role"] != "admin":
            user_details["allowed_states"] = []
        updated["users"][username] = user_details

    new_username_raw = str(form.get("new_username") or "").strip()
    new_username: str | None = None
    if new_username_raw:
        new_username = normalize_admin_username(new_username_raw)
        if new_username in updated["users"]:
            raise ValueError(f"User {new_username} already exists.")
        new_details = build_admin_user_details(
            new_username,
            display_name=form.get("new_display_name"),
            role=form.get("new_role"),
            allowed_states=form.get("new_allowed_states"),
            password_hash_env=form.get("new_password_hash_env"),
            permissions=admin_permissions_from_form(form, "new_permission"),
        )
        updated["users"][new_username] = new_details

    for state_code in sorted(territories.STATE_CODE_TO_NAME):
        owner_raw = str(form.get(f"territory_owner_{state_code}") or "").strip()
        if not owner_raw:
            continue
        owner_username = normalize_admin_username(owner_raw)
        owner = updated["users"].get(owner_username)
        if not owner:
            raise ValueError(f"Territory owner {owner_username} does not exist.")
        if str(owner.get("role") or "").strip().lower() == "admin":
            raise ValueError("Admin already has all states; assign territories to non-admin users.")
        owner_states = territories.normalize_state_list(owner.get("allowed_states"))
        if state_code not in owner_states:
            owner_states.append(state_code)
        owner["allowed_states"] = sorted(owner_states)

    return updated, new_username


def owner_by_state_from_users(users_config: dict[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for username, user in users_config.get("users", {}).items():
        if str(user.get("role") or "").strip().lower() == "admin":
            continue
        for state_code in territories.normalize_state_list(user.get("allowed_states")):
            if state_code != "*":
                owners[state_code] = str(username)
    return owners


def table_column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
    except sqlite3.Error:
        return set()


def reconcile_owner_fields(
    connection: sqlite3.Connection,
    users_config: dict[str, Any],
    *,
    states: set[str] | None = None,
) -> dict[str, int]:
    owners_by_state = owner_by_state_from_users(users_config)
    stats = {
        "prospects": 0,
        "quotes": 0,
        "outreach_queue": 0,
        "dashboard_jobs": 0,
    }

    pipeline_db.ensure_territory_columns(connection)

    prospect_columns = table_column_names(connection, "prospects")
    if {"id", "market", "market_state", "owner_username"}.issubset(prospect_columns):
        rows = connection.execute(
            "SELECT id, market, market_state, owner_username, state, state_guess FROM prospects"
        ).fetchall()
        for row in rows:
            prospect = _row_to_dict(row)
            state_code = prospect_state_from_record(prospect)
            if not state_code or (states is not None and state_code not in states):
                continue
            owner_username = owners_by_state.get(state_code)
            if (
                territories.normalize_state(prospect.get("market_state")) != state_code
                or str(prospect.get("owner_username") or "") != str(owner_username or "")
            ):
                connection.execute(
                    """
                    UPDATE prospects
                    SET market_state = ?,
                        owner_username = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (state_code, owner_username, utc_now(), prospect["id"]),
                )
                stats["prospects"] += 1

    quote_columns = table_column_names(connection, "quotes")
    if {"id", "prospect_id", "owner_username", "market_state"}.issubset(quote_columns):
        rows = connection.execute(
            """
            SELECT q.id, q.owner_username, q.market_state,
                   p.owner_username AS prospect_owner_username,
                   p.market_state AS prospect_market_state
            FROM quotes q
            JOIN prospects p ON p.id = q.prospect_id
            """
        ).fetchall()
        for row in rows:
            state_code = territories.normalize_state(row["prospect_market_state"])
            if states is not None and state_code not in states:
                continue
            if row["owner_username"] != row["prospect_owner_username"] or row["market_state"] != row["prospect_market_state"]:
                connection.execute(
                    """
                    UPDATE quotes
                    SET owner_username = ?,
                        market_state = ?
                    WHERE id = ?
                    """,
                    (row["prospect_owner_username"], row["prospect_market_state"], row["id"]),
                )
                stats["quotes"] += 1

    queue_columns = table_column_names(connection, "outreach_queue")
    if {"id", "prospect_id", "owner_username", "market_state"}.issubset(queue_columns):
        rows = connection.execute(
            """
            SELECT q.id, q.owner_username, q.market_state,
                   p.owner_username AS prospect_owner_username,
                   p.market_state AS prospect_market_state
            FROM outreach_queue q
            JOIN prospects p ON p.id = q.prospect_id
            """
        ).fetchall()
        for row in rows:
            state_code = territories.normalize_state(row["prospect_market_state"])
            if states is not None and state_code not in states:
                continue
            if row["owner_username"] != row["prospect_owner_username"] or row["market_state"] != row["prospect_market_state"]:
                connection.execute(
                    """
                    UPDATE outreach_queue
                    SET owner_username = ?,
                        market_state = ?
                    WHERE id = ?
                    """,
                    (row["prospect_owner_username"], row["prospect_market_state"], row["id"]),
                )
                stats["outreach_queue"] += 1

    job_columns = table_column_names(connection, "dashboard_jobs")
    if {"id", "market", "market_state"}.issubset(job_columns):
        rows = connection.execute("SELECT id, market, market_state FROM dashboard_jobs").fetchall()
        for row in rows:
            state_code = configured_market_state(str(row["market"] or "").strip())
            if not state_code or (states is not None and state_code not in states):
                continue
            if territories.normalize_state(row["market_state"]) != state_code:
                connection.execute(
                    "UPDATE dashboard_jobs SET market_state = ?, updated_at = ? WHERE id = ?",
                    (state_code, utc_now(), row["id"]),
                )
                stats["dashboard_jobs"] += 1

    return stats


def admin_result_message() -> dict[str, str] | None:
    message = str(request.args.get("message") or "").strip()
    if not message:
        return None
    status = str(request.args.get("status") or "success").strip()
    return {"status": "error" if status == "error" else "success", "message": message}


def admin_redirect(message: str, *, status: str = "success", download_config: bool = False) -> Any:
    params: dict[str, str] = {"message": message, "status": status}
    if download_config and status != "error":
        params["download_config"] = "1"
    return redirect(url_for("admin_home", **params))


def admin_territory_diagnostics() -> dict[str, Any]:
    users_config = auth_service.load_user_config()
    markets = load_configured_markets()
    owners_by_state = admin_state_owners(users_config)
    conflict_rows = territories.validate_exclusive_territories(users_config)
    conflicts_by_state = admin_conflicts_by_state(conflict_rows)
    markets_by_state, missing_state_markets = admin_markets_by_state(markets)
    prospect_counts_by_state, unknown_prospect_count = admin_prospect_counts_by_state()

    state_codes = sorted(
        set(owners_by_state)
        | set(markets_by_state)
        | set(prospect_counts_by_state)
        | set(conflicts_by_state)
    )
    territory_rows = [
        {
            "state": state_code,
            "owner_username": ", ".join(owners_by_state.get(state_code, [])),
            "markets": markets_by_state.get(state_code, []),
            "prospect_count": prospect_counts_by_state.get(state_code, 0),
            "conflicts": conflicts_by_state.get(state_code, []),
        }
        for state_code in state_codes
    ]
    unassigned_states = [
        row
        for row in territory_rows
        if not row["owner_username"] and (row["markets"] or row["prospect_count"])
    ]
    return {
        "territory_rows": territory_rows,
        "conflicts": conflict_rows,
        "unassigned_states": unassigned_states,
        "missing_state_markets": missing_state_markets,
        "unknown_prospect_count": unknown_prospect_count,
    }


def admin_state_owners(users_config: dict[str, Any]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    users = users_config.get("users") if isinstance(users_config.get("users"), dict) else {}
    for username, raw_user in users.items():
        user = raw_user if isinstance(raw_user, dict) else {}
        if str(user.get("role") or "").strip().lower() == "admin":
            continue
        for state_code in territories.normalize_state_list(user.get("allowed_states")):
            if state_code == "*":
                continue
            owners.setdefault(state_code, []).append(str(username))
    return owners


def admin_conflicts_by_state(conflicts: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_state: dict[str, list[str]] = {}
    for conflict in conflicts:
        state_code = territories.normalize_state(conflict.get("state"))
        if not state_code:
            state_code = "Unknown"
        if conflict.get("type") == "duplicate_state":
            usernames = ", ".join(str(value) for value in conflict.get("usernames", []))
            message = f"Duplicate owner assignment: {usernames}"
        elif conflict.get("type") == "invalid_state":
            message = (
                f"Invalid state {conflict.get('state')!r} on "
                f"{conflict.get('username') or 'unknown user'}"
            )
        else:
            message = str(conflict)
        by_state.setdefault(state_code, []).append(message)
    return by_state


def admin_markets_by_state(
    markets: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    markets_by_state: dict[str, list[dict[str, str]]] = {}
    missing_state_markets: list[dict[str, str]] = []
    for market in markets:
        market_info = {
            "key": str(market.get("key") or ""),
            "label": str(market.get("label") or market.get("key") or ""),
        }
        state_code = territories.normalize_state(market.get("state"))
        if state_code:
            markets_by_state.setdefault(state_code, []).append(market_info)
        else:
            missing_state_markets.append(market_info)
    for state_markets in markets_by_state.values():
        state_markets.sort(key=lambda item: (item["label"].lower(), item["key"].lower()))
    missing_state_markets.sort(key=lambda item: (item["label"].lower(), item["key"].lower()))
    return markets_by_state, missing_state_markets


def admin_prospect_counts_by_state() -> tuple[dict[str, int], int]:
    rows = get_connection().execute(
        """
        SELECT market, market_state, state, state_guess
        FROM prospects
        """
    ).fetchall()
    counts: dict[str, int] = {}
    unknown_count = 0
    for row in rows:
        state_code = prospect_state_from_record(row)
        if state_code:
            counts[state_code] = counts.get(state_code, 0) + 1
        else:
            unknown_count += 1
    return counts, unknown_count


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


def append_active_prospect_filter(clauses: list[str], params: list[Any]) -> None:
    status_placeholders = ",".join("?" for _ in TRASH_STATUSES)
    qualification_placeholders = ",".join("?" for _ in TRASH_QUALIFICATION_STATUSES)
    clauses.extend(
        [
            f"UPPER(COALESCE(status, '')) NOT IN ({status_placeholders})",
            f"UPPER(COALESCE(qualification_status, '')) NOT IN ({qualification_placeholders})",
        ]
    )
    params.extend(TRASH_STATUSES)
    params.extend(TRASH_QUALIFICATION_STATUSES)


def append_trash_prospect_filter(clauses: list[str], params: list[Any]) -> None:
    status_placeholders = ",".join("?" for _ in TRASH_VISIBLE_STATUSES)
    qualification_placeholders = ",".join("?" for _ in TRASH_QUALIFICATION_STATUSES)
    clauses.append(
        "("
        f"UPPER(COALESCE(status, '')) IN ({status_placeholders}) "
        f"OR UPPER(COALESCE(qualification_status, '')) IN ({qualification_placeholders})"
        ")"
    )
    params.extend(TRASH_VISIBLE_STATUSES)
    params.extend(TRASH_QUALIFICATION_STATUSES)


def append_job_protected_filter(clauses: list[str], params: list[Any]) -> None:
    status_placeholders = ",".join("?" for _ in PROTECTED_JOB_STATUSES)
    next_action_placeholders = ",".join("?" for _ in PROTECTED_JOB_NEXT_ACTIONS)
    clauses.extend(
        [
            f"UPPER(COALESCE(status, '')) NOT IN ({status_placeholders})",
            f"UPPER(COALESCE(next_action, '')) NOT IN ({next_action_placeholders})",
        ]
    )
    params.extend(PROTECTED_JOB_STATUSES)
    params.extend(PROTECTED_JOB_NEXT_ACTIONS)


def _metadata_dict(value: Any) -> dict[str, Any]:
    metadata = parse_json_field(value)
    return metadata if isinstance(metadata, dict) else {}


def _parse_utc_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _trash_due_label(value: Any) -> str:
    due_at = _parse_utc_datetime(value)
    if due_at is None:
        return "Not scheduled"
    now = datetime.now(timezone.utc)
    if due_at <= now:
        return "Due now"
    delta = due_at - now
    days = delta.days
    if days >= 1:
        return f"{days + 1} days"
    hours = max(1, int(delta.total_seconds() // 3600))
    return f"{hours} hours"


def normalize_trash_category(value: Any) -> str:
    category = _normalize_token(value).lower()
    allowed = {key for key, _label in TRASH_CATEGORY_OPTIONS}
    return category if category in allowed else "all"


def _fallback_trash_reason(row: dict[str, Any]) -> str:
    status = _normalize_token(row.get("status"))
    qualification_status = _normalize_token(row.get("qualification_status"))
    if prospect_has_missing_website_signal(row):
        return "missing_website"
    if status == "CLOSED_LOST":
        return "closed_lost"
    if status == "INELIGIBLE" or qualification_status == "DISQUALIFIED":
        return "automated_eligibility_filter"
    if status == "REJECTED_REVIEW":
        return "manual_review_reject"
    if status == "DISCARDED":
        return "crm_stage_discarded"
    return "legacy_rejected_or_discarded"


def _trash_category_for_row(row: dict[str, Any], trash: dict[str, Any] | None = None) -> str:
    status = _normalize_token(row.get("status"))
    qualification_status = _normalize_token(row.get("qualification_status"))
    reason = _normalize_token((trash or {}).get("reason")).lower()
    if status == "CLOSED_LOST" or reason == "closed_lost":
        return "closed_lost"
    if prospect_has_missing_website_signal(row):
        return "system_deleted"
    if reason in {"manual_review_reject", "crm_stage_discarded", "quick_deleted"}:
        return "manual_deleted"
    if status in {"REJECTED_REVIEW", "DISCARDED"}:
        return "manual_deleted"
    if (
        reason in {"automated_eligibility_filter", "places_disqualification"}
        or reason.startswith("automated")
        or status == "INELIGIBLE"
        or qualification_status == "DISQUALIFIED"
    ):
        return "system_deleted"
    return "legacy"


def _trash_detail_lines(row: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        key = text.lower()
        if key not in seen:
            seen.add(key)
            lines.append(text)

    pre_audit = metadata.get("pre_audit_eligibility")
    pre_audit = pre_audit if isinstance(pre_audit, dict) else {}
    for reason in pre_audit.get("forced_reasons") or []:
        add(reason)

    reason_items = pre_audit.get("all_reasons") or pre_audit.get("top_reasons") or []
    if isinstance(reason_items, list):
        for item in reason_items:
            if isinstance(item, dict):
                reason = str(item.get("reason") or "").strip()
                points = int_value(item.get("points"), default=0)
                suffix = f" ({points:+})" if points else ""
                add(f"{reason}{suffix}")
            else:
                add(item)

    franchise = pre_audit.get("franchise_exclusion") or metadata.get("franchise_exclusion")
    if isinstance(franchise, dict) and franchise.get("is_excluded"):
        match = (
            franchise.get("matched_name")
            or franchise.get("matched_domain")
            or franchise.get("matched_regex")
            or "configured exclusion"
        )
        add(franchise.get("reason") or f"franchise/national-chain exclusion: {match}")

    add(metadata.get("disqualification_reason"))
    if not lines:
        status = _normalize_token(row.get("status"))
        qualification_status = _normalize_token(row.get("qualification_status"))
        if status == "CLOSED_LOST":
            add("closed_lost")
        elif status == "INELIGIBLE" or qualification_status == "DISQUALIFIED":
            add("automated eligibility filter")
    return lines


def mark_prospect_trashed(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    reason: str,
    category: str | None = None,
    previous: dict[str, Any] | sqlite3.Row | None = None,
) -> None:
    row = previous or connection.execute(
        "SELECT * FROM prospects WHERE id = ?",
        (prospect_id,),
    ).fetchone()
    current = _row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row or {})
    metadata = _metadata_dict(current.get("metadata_json"))
    trash = metadata.get("trash") if isinstance(metadata.get("trash"), dict) else {}
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()
    delete_after = (now_dt + timedelta(days=TRASH_MEDIA_RETENTION_DAYS)).isoformat()
    metadata["trash"] = {
        **trash,
        "is_trashed": True,
        "reason": reason,
        "category": category or trash.get("category") or _trash_category_for_row(current, {"reason": reason}),
        "trashed_at": trash.get("trashed_at") or now,
        "media_delete_after": trash.get("media_delete_after") or delete_after,
        "media_retention_days": TRASH_MEDIA_RETENTION_DAYS,
        "previous_status": trash.get("previous_status") or current.get("status"),
        "previous_next_action": trash.get("previous_next_action") or current.get("next_action"),
        "previous_human_review_status": trash.get("previous_human_review_status")
        or current.get("human_review_status"),
        "previous_human_review_decision": trash.get("previous_human_review_decision")
        or current.get("human_review_decision"),
    }
    connection.execute(
        """
        UPDATE prospects
        SET metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, sort_keys=True), now, prospect_id),
    )


def clear_trash_metadata(connection: sqlite3.Connection, prospect_id: int) -> None:
    row = connection.execute(
        "SELECT metadata_json FROM prospects WHERE id = ?",
        (prospect_id,),
    ).fetchone()
    if row is None:
        return
    metadata = _metadata_dict(row["metadata_json"])
    trash = metadata.get("trash")
    if isinstance(trash, dict):
        history = metadata.setdefault("trash_history", [])
        if isinstance(history, list):
            history.append({**trash, "restored_at": utc_now()})
        metadata.pop("trash", None)
    connection.execute(
        """
        UPDATE prospects
        SET metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, sort_keys=True), utc_now(), prospect_id),
    )


def restore_state_for_prospect(prospect: dict[str, Any], trash: dict[str, Any]) -> dict[str, Any]:
    previous_status = _normalize_token(trash.get("previous_status"))
    previous_next_action = _normalize_token(trash.get("previous_next_action"))
    previous_review_status = _normalize_token(trash.get("previous_human_review_status"))
    previous_review_decision = _normalize_token(trash.get("previous_human_review_decision"))
    if prospect_has_missing_website_signal(prospect):
        return {
            "status": "NO_WEBSITE",
            "qualification_status": "NO_WEBSITE",
            "next_action": "COLD_CALL_WEBSITE",
            "human_review_status": previous_review_status or prospect.get("human_review_status"),
            "human_review_decision": previous_review_decision or None,
        }

    restored_qualification = restore_qualification_status_for_prospect(prospect)
    if previous_status and previous_status not in TRASH_VISIBLE_STATUSES:
        return {
            "status": previous_status,
            "qualification_status": restored_qualification,
            "next_action": previous_next_action or CRM_NEXT_ACTIONS.get(previous_status),
            "human_review_status": previous_review_status or prospect.get("human_review_status"),
            "human_review_decision": previous_review_decision or prospect.get("human_review_decision"),
        }
    if _normalize_token(prospect.get("audit_data_status")) == "READY":
        return {
            "status": "PENDING_REVIEW",
            "qualification_status": restored_qualification,
            "next_action": "HUMAN_REVIEW",
            "human_review_status": "PENDING",
            "human_review_decision": None,
        }
    if _normalize_token(prospect.get("qualification_status")) == "QUALIFIED":
        return {
            "status": "ELIGIBLE_FOR_AUDIT",
            "qualification_status": "QUALIFIED",
            "next_action": "RUN_AUDIT",
            "human_review_status": prospect.get("human_review_status"),
            "human_review_decision": None,
        }
    return {
        "status": "NEW",
        "qualification_status": restored_qualification,
        "next_action": None,
        "human_review_status": prospect.get("human_review_status"),
        "human_review_decision": None,
    }


def restore_qualification_status_for_prospect(prospect: dict[str, Any]) -> str:
    qualification_status = _normalize_token(prospect.get("qualification_status"))
    if qualification_status in TRASH_QUALIFICATION_STATUSES:
        return "DISCOVERED"
    return qualification_status or "DISCOVERED"


def prospect_has_missing_website_signal(
    prospect: dict[str, Any] | sqlite3.Row,
    metadata: dict[str, Any] | None = None,
) -> bool:
    metadata = metadata if metadata is not None else _metadata_dict(record_value(prospect, "metadata_json"))
    status = _normalize_token(record_value(prospect, "status"))
    qualification_status = _normalize_token(record_value(prospect, "qualification_status"))
    next_action = _normalize_token(record_value(prospect, "next_action"))
    disqualification_reason = _normalize_token(metadata.get("disqualification_reason"))
    trash = metadata.get("trash") if isinstance(metadata.get("trash"), dict) else {}
    trash_reason = _normalize_token(trash.get("reason"))
    return (
        status == "NO_WEBSITE"
        or qualification_status == "NO_WEBSITE"
        or next_action == "COLD_CALL_WEBSITE"
        or metadata.get("no_website_bucket") is True
        or disqualification_reason == "MISSING_WEBSITE"
        or trash_reason == "MISSING_WEBSITE"
    )


def lead_category_label(
    prospect: dict[str, Any] | sqlite3.Row,
    metadata: dict[str, Any] | None = None,
) -> str:
    source = str(record_value(prospect, "source") or "").strip().lower()
    if prospect_has_missing_website_signal(prospect, metadata):
        return "Google Maps No Website" if source == "google_places" else "No Website Lead"
    return "Google Maps Lead" if source == "google_places" else ""


def restore_trashed_prospect(connection: sqlite3.Connection, prospect: dict[str, Any]) -> None:
    metadata = _metadata_dict(prospect.get("metadata_json"))
    trash = metadata.get("trash") if isinstance(metadata.get("trash"), dict) else {}
    restored_state = restore_state_for_prospect(prospect, trash)
    history = metadata.setdefault("trash_history", [])
    if isinstance(history, list):
        history.append({**trash, "restored_at": utc_now()})
    metadata.pop("trash", None)
    now = utc_now()
    connection.execute(
        """
        UPDATE prospects
        SET status = ?,
            qualification_status = ?,
            next_action = ?,
            human_review_status = ?,
            human_review_decision = ?,
            metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            restored_state["status"],
            restored_state["qualification_status"],
            restored_state["next_action"],
            restored_state["human_review_status"],
            restored_state["human_review_decision"],
            json.dumps(metadata, sort_keys=True),
            now,
            prospect["id"],
        ),
    )
    insert_crm_stage_event(
        connection,
        prospect_id=prospect["id"],
        old_status=prospect.get("status"),
        old_next_action=prospect.get("next_action"),
        new_status=restored_state["status"],
        next_action=restored_state["next_action"] or "",
        note="Restored from trash",
        metadata={"source": "trash_restore"},
    )


def purge_due_trash_media(
    connection: sqlite3.Connection,
    *,
    market: str = "",
    category: str = "all",
) -> dict[str, int]:
    clauses: list[str] = []
    params: list[Any] = []
    append_trash_prospect_filter(clauses, params)
    append_visible_market_scope(clauses, params, market)
    rows = [
        _row_to_dict(row)
        for row in connection.execute(
            f"SELECT * FROM prospects WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
    ]
    now_dt = datetime.now(timezone.utc)
    now = now_dt.replace(microsecond=0).isoformat()
    prospects_checked = 0
    prospects_purged = 0
    files_deleted = 0
    artifacts_marked = 0
    normalized_category = normalize_trash_category(category)
    for prospect in rows:
        metadata = _metadata_dict(prospect.get("metadata_json"))
        trash = metadata.get("trash") if isinstance(metadata.get("trash"), dict) else {}
        if not trash:
            trashed_at = prospect.get("human_reviewed_at") or prospect.get("updated_at")
            trashed_dt = _parse_utc_datetime(trashed_at)
            reason = _fallback_trash_reason(prospect)
            trash = {
                "is_trashed": True,
                "reason": reason,
                "category": _trash_category_for_row(prospect, {"reason": reason}),
                "trashed_at": trashed_at,
                "media_delete_after": (
                    (trashed_dt + timedelta(days=TRASH_MEDIA_RETENTION_DAYS))
                    .replace(microsecond=0)
                    .isoformat()
                    if trashed_dt
                    else None
                ),
                "media_retention_days": TRASH_MEDIA_RETENTION_DAYS,
            }
        if (
            normalized_category != "all"
            and _trash_category_for_row(prospect, trash) != normalized_category
        ):
            continue
        due_at = _parse_utc_datetime(trash.get("media_delete_after"))
        if due_at is None or due_at > now_dt or trash.get("media_purged_at"):
            continue
        prospects_checked += 1
        artifacts = connection.execute(
            f"""
            SELECT *
            FROM artifacts
            WHERE prospect_id = ?
              AND artifact_type IN ({','.join('?' for _ in TRASH_MEDIA_ARTIFACT_TYPES)})
            """,
            (prospect["id"], *TRASH_MEDIA_ARTIFACT_TYPES),
        ).fetchall()
        purged_for_prospect = False
        for artifact_row in artifacts:
            artifact = _row_to_dict(artifact_row)
            resolved_path = resolve_media_path(artifact.get("path"))
            if resolved_path and resolved_path.is_file():
                resolved_path.unlink()
                files_deleted += 1
            artifact_metadata = _metadata_dict(artifact.get("metadata_json"))
            artifact_metadata["trash_purged_at"] = now
            artifact_metadata["trash_purge_reason"] = "trash_retention_expired"
            connection.execute(
                """
                UPDATE artifacts
                SET status = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "purged",
                    json.dumps(artifact_metadata, sort_keys=True),
                    now,
                    artifact["id"],
                ),
            )
            artifacts_marked += 1
            purged_for_prospect = True
        if purged_for_prospect:
            trash["media_purged_at"] = now
            metadata["trash"] = trash
            connection.execute(
                """
                UPDATE prospects
                SET metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(metadata, sort_keys=True), now, prospect["id"]),
            )
            prospects_purged += 1
    return {
        "prospects_checked": prospects_checked,
        "prospects_purged": prospects_purged,
        "files_deleted": files_deleted,
        "artifacts_marked": artifacts_marked,
    }


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
    state = territories.normalize_state(form.get("state"))
    market_key = str(form.get("market_key") or "").strip()
    cities = parse_market_cities(form.get("included_cities"))
    notes = str(form.get("notes") or "").strip()

    if not label:
        raise ValueError("Label is required.")
    if state is None:
        raise ValueError("State must be a valid two-letter abbreviation.")
    if not current_user_can_access_state(state):
        raise ValueError(territory_denial_message())
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


def load_users_document() -> dict[str, Any]:
    return auth_service.load_user_config(users_config_path())


def write_users_document(data: dict[str, Any]) -> None:
    path = users_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
        shutil.copy2(path, backup_path)
    path.write_text(dump_yaml_text(data), encoding="utf-8")


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
                owner_username TEXT,
                market_state TEXT,
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
        resolved_path = resolve_media_path(artifact.get("path")) or resolve_project_path(artifact.get("path"))
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


def production_requires_app_secret() -> bool:
    """Return True for hosted/production environments where a real secret is required."""

    if any(os.environ.get(key) for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")):
        return True
    return any(
        str(os.environ.get(key) or "").strip().lower() == "production"
        for key in ("APP_ENV", "ENV", "FLASK_ENV")
    )


def load_app_secret_key() -> str:
    secret_key = str(os.environ.get("APP_SECRET_KEY") or "").strip()
    if secret_key:
        return secret_key

    if production_requires_app_secret():
        raise RuntimeError("APP_SECRET_KEY must be set in production/Railway environments.")

    logger.warning(
        "APP_SECRET_KEY is not set; generated a temporary local development secret. "
        "Set APP_SECRET_KEY for stable sessions."
    )
    return secrets.token_urlsafe(48)


def dashboard_auth_enabled() -> bool:
    return True


def dashboard_auth_configured() -> bool:
    try:
        return bool(auth_service.load_user_config().get("users"))
    except (FileNotFoundError, ValueError):
        return False


def dashboard_allowed_states_summary(user: auth_service.User | None) -> str:
    if user is None:
        return ""
    if auth_service.is_admin(user) or "*" in user.allowed_states:
        return "ALL STATES"
    return ", ".join(user.allowed_states)


def dashboard_db_import_enabled() -> bool:
    value = str(os.environ.get("DASHBOARD_DB_IMPORT_ENABLED") or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def dashboard_media_import_enabled() -> bool:
    value = str(os.environ.get("DASHBOARD_MEDIA_IMPORT_ENABLED") or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


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


def validate_uploaded_sqlite(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        required_tables = {"prospects", "website_audits", "artifacts"}
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
        tables = {str(row[0]) for row in rows}
        missing = sorted(required_tables - tables)
        if missing:
            raise ValueError(f"Uploaded DB is missing required table(s): {', '.join(missing)}")
        counts = {}
        for table in ("prospects", "website_audits", "artifacts", "contacts", "outreach_queue"):
            if table in tables:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if counts.get("prospects", 0) < 1:
            raise ValueError("Uploaded DB has no prospects.")
        return {"tables": sorted(tables), "counts": counts}
    finally:
        connection.close()


def backup_sqlite_database(source: Path) -> Path | None:
    if not source.is_file():
        return None
    backup_dir = project_path("backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{source.stem}-before-import-{stamp}.db"
    source_connection = sqlite3.connect(source)
    try:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    return target


def import_dashboard_database(uploaded_file: Any) -> dict[str, Any]:
    filename = str(getattr(uploaded_file, "filename", "") or "")
    if not filename.lower().endswith(".db"):
        raise ValueError("Upload a .db SQLite file.")
    db_path = resolve_project_path(current_app.config["DATABASE_PATH"])
    if db_path is None:
        raise ValueError("DATABASE_PATH is not configured.")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(f".{db_path.name}.upload-{secrets.token_hex(8)}.tmp")
    uploaded_file.save(tmp_path)
    try:
        validation = validate_uploaded_sqlite(tmp_path)
        old_connection = g.pop("dashboard_db", None)
        if old_connection is not None:
            old_connection.close()
        backup_path = backup_sqlite_database(db_path)
        os.replace(tmp_path, db_path)
        return {
            "database_path": str(db_path),
            "backup_path": str(backup_path) if backup_path else "",
            "counts": validation["counts"],
        }
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def import_media_archive(uploaded_file: Any) -> dict[str, Any]:
    filename = str(getattr(uploaded_file, "filename", "") or "")
    if not filename.lower().endswith(".zip"):
        raise ValueError("Upload a .zip file.")

    tmp_dir = project_path("runs/latest/import_uploads")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"media-{secrets.token_hex(8)}.zip"
    uploaded_file.save(tmp_path)

    extracted = 0
    skipped_dirs = 0
    bytes_written = 0
    roots_seen: set[str] = set()
    project_root = PROJECT_ROOT.resolve(strict=False)

    try:
        with zipfile.ZipFile(tmp_path) as archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/").lstrip("/")
                parts = [part for part in name.split("/") if part]
                if not parts or name.endswith("/"):
                    skipped_dirs += 1
                    continue
                if parts[0] not in MEDIA_IMPORT_ROOTS:
                    raise ValueError(
                        "Zip entries must start with one of: "
                        + ", ".join(MEDIA_IMPORT_ROOTS)
                    )
                if any(part in {"..", "."} for part in parts):
                    raise ValueError(f"Unsafe zip path: {member.filename}")

                target = project_path(Path(*parts)).resolve(strict=False)
                try:
                    target.relative_to(project_root)
                except ValueError as exc:
                    raise ValueError(f"Unsafe zip path: {member.filename}") from exc

                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                extracted += 1
                bytes_written += int(member.file_size or 0)
                roots_seen.add(parts[0])
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    if extracted < 1:
        raise ValueError("Zip did not contain any importable files.")

    return {
        "files": extracted,
        "bytes": bytes_written,
        "megabytes": round(bytes_written / 1024 / 1024, 2),
        "roots": sorted(roots_seen),
        "skipped_dirs": skipped_dirs,
    }


def create_app(db_path: str | Path | None = None) -> Flask:
    load_env()
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = load_app_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["DASHBOARD_AUTH_ENABLED"] = dashboard_auth_enabled()
    app.config["DASHBOARD_DB_IMPORT_ENABLED"] = dashboard_db_import_enabled()
    app.config["DASHBOARD_MEDIA_IMPORT_ENABLED"] = dashboard_media_import_enabled()
    app.config["DATABASE_PATH"] = str(resolve_project_path(db_path or get_database_path()))

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_login_user(user_id: str) -> auth_service.User | None:
        return auth_service.load_user(user_id)

    @login_manager.unauthorized_handler
    def redirect_to_login():
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    dashboard_jobs.ensure_schema(app.config["DATABASE_PATH"])
    ensure_outreach_queue_schema(app.config["DATABASE_PATH"])
    pipeline_db.ensure_quote_schema_for_path(app.config["DATABASE_PATH"])
    pipeline_db.ensure_contract_schema_for_path(app.config["DATABASE_PATH"])
    task_service.ensure_schema(app.config["DATABASE_PATH"])
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

    @app.template_filter("money")
    def money(value: Any) -> str:
        return quote_service.format_money(value)

    @app.context_processor
    def auth_template_context() -> dict[str, Any]:
        dashboard_current_user = auth_service.current_app_user()
        permissions = dashboard_permission_map(dashboard_current_user)
        return {
            "dashboard_auth_enabled": app.config["DASHBOARD_AUTH_ENABLED"],
            "dashboard_current_user": dashboard_current_user,
            "dashboard_user": dashboard_current_user.username if dashboard_current_user else "",
            "dashboard_role": dashboard_current_user.role if dashboard_current_user else "",
            "dashboard_allowed_states_summary": dashboard_allowed_states_summary(dashboard_current_user),
            "dashboard_permissions": permissions,
            "csrf_token": get_csrf_token,
            "csrf_input": csrf_input,
        }

    @app.before_request
    def validate_dashboard_csrf():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
            return None
        if validate_csrf_token(csrf_token_from_request()):
            return None
        return csrf_failure_response()

    @app.before_request
    def require_dashboard_login():
        if request.endpoint in PUBLIC_AUTH_ENDPOINTS:
            return None
        if bool(getattr(flask_current_user, "is_authenticated", False)):
            return None
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        next_url = safe_next_path(request.values.get("next"))
        if bool(getattr(flask_current_user, "is_authenticated", False)):
            return redirect(next_url)

        error = ""
        if request.method == "POST":
            username = str(request.form.get("username") or "").strip()
            password = str(request.form.get("password") or "")
            if auth_service.verify_password(username, password):
                user = auth_service.get_user(username)
                if user is not None:
                    session.clear()
                    session.permanent = True
                    generate_csrf_token()
                    flask_login_user(user)
                    session["user_id"] = user.username
                    session["role"] = user.role
                    session["states"] = list(user.allowed_states)
                    session["authenticated_at"] = utc_now()
                    return redirect(next_url)
            error = "Invalid username or password."
        return render_template(
            "dashboard/login.html",
            active_page="login",
            error=error,
            next_url=next_url,
        )

    @app.route("/logout", methods=["GET", "POST"])
    @login_required
    def logout():
        flask_logout_user()
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

    @app.get("/admin")
    @login_required
    @auth_service.admin_required
    def admin_home() -> str:
        diagnostics = admin_territory_diagnostics()
        return render_template(
            "dashboard/admin.html",
            active_page="admin",
            users=admin_user_config_rows(),
            territory_rows=admin_state_assignment_rows(),
            user_options=admin_non_admin_user_options(),
            missing_state_markets=diagnostics["missing_state_markets"],
            unknown_prospect_count=diagnostics["unknown_prospect_count"],
            conflicts=diagnostics["conflicts"],
            users_config_path=users_config_path(),
            hash_generator_path=PROJECT_ROOT / "generate_user_password_hash.bat",
            message=admin_result_message(),
            download_config_after_save=request.args.get("download_config") == "1",
            permission_options=[
                {"key": key, "label": auth_service.USER_PERMISSION_LABELS[key]}
                for key in auth_service.USER_PERMISSION_KEYS
            ],
        )

    @app.get("/admin/users-config/download")
    @login_required
    @auth_service.admin_required
    def admin_download_users_config():
        path = users_config_path()
        if not path.exists():
            abort(404)
        return send_file(
            path,
            as_attachment=True,
            download_name="users.yaml",
            mimetype="application/x-yaml",
        )

    @app.post("/admin/save-all")
    @login_required
    @auth_service.admin_required
    def admin_save_all():
        try:
            config, new_username = build_admin_save_all_document(request.form)
            normalized = save_admin_users_document(config)
            connection = get_connection()
            stats = reconcile_owner_fields(connection, normalized)
            connection.commit()
        except Exception as exc:
            return admin_redirect(str(exc), status="error")

        changed = sum(stats.values())
        user_count = len(normalized.get("users", {}))
        new_user_note = (
            f" Added {new_username}; set {normalized['users'][new_username]['password_hash_env']} in Railway before login."
            if new_username
            else ""
        )
        return admin_redirect(
            f"Saved {user_count} users and territory assignments. Synced {changed} ownership rows.{new_user_note}",
            download_config=True,
        )

    @app.post("/admin/users/add")
    @login_required
    @auth_service.admin_required
    def admin_add_user():
        try:
            username, user_details = parse_admin_user_form(request.form)
            config = load_users_document()
            if username in config.get("users", {}):
                raise ValueError(f"User {username} already exists.")
            config["users"][username] = user_details
            normalized = save_admin_users_document(config)
            connection = get_connection()
            stats = reconcile_owner_fields(connection, normalized)
            connection.commit()
        except Exception as exc:
            return admin_redirect(str(exc), status="error")
        changed = sum(stats.values())
        return admin_redirect(
            f"User {username} added. Set {user_details['password_hash_env']} in Railway before login. Synced {changed} rows."
        )

    @app.post("/admin/users/<username>/update")
    @login_required
    @auth_service.admin_required
    def admin_update_user(username: str):
        try:
            username = normalize_admin_username(username)
            config = load_users_document()
            if username not in config.get("users", {}):
                abort(404)
            _, user_details = parse_admin_user_form(request.form, existing_username=username)
            config["users"][username] = user_details
            normalized = save_admin_users_document(config)
            connection = get_connection()
            stats = reconcile_owner_fields(connection, normalized)
            connection.commit()
        except Exception as exc:
            return admin_redirect(str(exc), status="error")
        changed = sum(stats.values())
        return admin_redirect(f"User {username} updated. Synced {changed} ownership rows.")

    @app.post("/admin/territories/assign")
    @login_required
    @auth_service.admin_required
    def admin_assign_territory():
        try:
            state_code = territories.normalize_state(request.form.get("state"))
            if not state_code:
                raise ValueError("Choose a valid state.")
            owner_username_raw = str(request.form.get("owner_username") or "").strip()
            owner_username = normalize_admin_username(owner_username_raw) if owner_username_raw else ""
            config = load_users_document()
            users = config.get("users", {})
            if owner_username:
                owner = users.get(owner_username)
                if not owner:
                    raise ValueError(f"User {owner_username} does not exist.")
                if str(owner.get("role") or "").strip().lower() == "admin":
                    raise ValueError("Admin already has all states; assign territories to non-admin users.")

            for raw_user in users.values():
                if not isinstance(raw_user, dict):
                    continue
                if str(raw_user.get("role") or "").strip().lower() == "admin":
                    raw_user["allowed_states"] = ["*"]
                    continue
                raw_user["allowed_states"] = [
                    state
                    for state in territories.normalize_state_list(raw_user.get("allowed_states"))
                    if state != "*" and state != state_code
                ]
            if owner_username:
                owner_states = territories.normalize_state_list(users[owner_username].get("allowed_states"))
                if state_code not in owner_states:
                    owner_states.append(state_code)
                users[owner_username]["allowed_states"] = owner_states

            normalized = save_admin_users_document(config)
            connection = get_connection()
            stats = reconcile_owner_fields(connection, normalized, states={state_code})
            connection.commit()
        except Exception as exc:
            return admin_redirect(str(exc), status="error")

        changed = sum(stats.values())
        owner_label = owner_username or "unassigned"
        return admin_redirect(f"{state_code} assigned to {owner_label}. Synced {changed} ownership rows.")

    @app.get("/admin/database")
    @login_required
    @auth_service.admin_required
    def database_admin() -> str:
        return render_template(
            "dashboard/database_admin.html",
            active_page="admin",
            db_path=app.config["DATABASE_PATH"],
            import_enabled=app.config["DASHBOARD_DB_IMPORT_ENABLED"],
            result=None,
            error="",
        )

    @app.post("/admin/database/import")
    @login_required
    @auth_service.admin_required
    def database_import():
        if not app.config["DASHBOARD_DB_IMPORT_ENABLED"]:
            abort(403)
        if request.form.get("confirm_import") != "1":
            return render_template(
                "dashboard/database_admin.html",
                active_page="admin",
                db_path=app.config["DATABASE_PATH"],
                import_enabled=True,
                result=None,
                error="Check the confirmation box before importing.",
            )
        uploaded_file = request.files.get("database_file")
        if uploaded_file is None or not getattr(uploaded_file, "filename", ""):
            return render_template(
                "dashboard/database_admin.html",
                active_page="admin",
                db_path=app.config["DATABASE_PATH"],
                import_enabled=True,
                result=None,
                error="Choose a .db file first.",
            )
        try:
            result = import_dashboard_database(uploaded_file)
        except Exception as exc:
            return render_template(
                "dashboard/database_admin.html",
                active_page="admin",
                db_path=app.config["DATABASE_PATH"],
                import_enabled=True,
                result=None,
                error=str(exc),
            )
        return render_template(
            "dashboard/database_admin.html",
            active_page="admin",
            db_path=app.config["DATABASE_PATH"],
            import_enabled=True,
            result=result,
            error="",
        )

    @app.get("/admin/media")
    @login_required
    @auth_service.admin_required
    def media_admin() -> str:
        return render_template(
            "dashboard/media_admin.html",
            active_page="admin",
            import_enabled=app.config["DASHBOARD_MEDIA_IMPORT_ENABLED"],
            result=None,
            error="",
        )

    @app.post("/admin/media/import")
    @login_required
    @auth_service.admin_required
    def media_import():
        if not app.config["DASHBOARD_MEDIA_IMPORT_ENABLED"]:
            abort(403)
        if request.form.get("confirm_import") != "1":
            return render_template(
                "dashboard/media_admin.html",
                active_page="admin",
                import_enabled=True,
                result=None,
                error="Check the confirmation box before importing.",
            )
        uploaded_file = request.files.get("media_file")
        if uploaded_file is None or not getattr(uploaded_file, "filename", ""):
            return render_template(
                "dashboard/media_admin.html",
                active_page="admin",
                import_enabled=True,
                result=None,
                error="Choose a .zip file first.",
            )
        try:
            result = import_media_archive(uploaded_file)
        except Exception as exc:
            return render_template(
                "dashboard/media_admin.html",
                active_page="admin",
                import_enabled=True,
                result=None,
                error=str(exc),
            )
        return render_template(
            "dashboard/media_admin.html",
            active_page="admin",
            import_enabled=True,
            result=result,
            error="",
        )

    @app.get("/admin/users")
    @login_required
    @auth_service.admin_required
    def admin_users() -> str:
        return render_template(
            "dashboard/admin_users.html",
            active_page="admin",
            users=admin_user_diagnostics(),
        )

    @app.get("/admin/territories")
    @login_required
    @auth_service.admin_required
    def admin_territories() -> str:
        diagnostics = admin_territory_diagnostics()
        return render_template(
            "dashboard/admin_territories.html",
            active_page="admin",
            **diagnostics,
        )

    @app.get("/")
    def overview() -> str:
        selected_market, territory_error = normalize_selected_market_for_user(
            selected_market_from_request(),
            allow_unknown_for_admin=True,
        )
        lead_search_query = request.args.get("lead_q", "").strip()
        stage_counts = load_stage_counts(selected_market)
        trash_summary = load_trash_summary(selected_market)
        open_task_count = load_overview_open_task_count(selected_market)
        total_prospects = sum(item["count"] for item in stage_counts)
        top_markets = load_group_counts("market", market=selected_market)
        top_niches = load_group_counts("niche", market=selected_market)
        message = (
            {"status": "error", "message": territory_error}
            if territory_error
            else job_message_from_code(request.args.get("result"))
        )
        return render_template(
            "dashboard/overview.html",
            active_page="overview",
            db_path=app.config["DATABASE_PATH"],
            market_filter=market_filter_context(selected_market),
            lead_search=build_lead_search_context(
                "overview",
                query=lead_search_query,
                market=selected_market,
                hidden_fields=[("market", selected_market)],
                reset_args=overview_market_args(selected_market),
            ),
            message=message,
            stage_counts=stage_counts,
            overview_command_cards=overview_command_cards(
                stage_counts,
                trash_summary=trash_summary,
                selected_market=selected_market,
                open_task_count=open_task_count,
            ),
            overview_metric_groups=overview_metric_groups(
                stage_counts,
                trash_summary=trash_summary,
                selected_market=selected_market,
            ),
            total_prospects=total_prospects,
            top_markets=top_markets,
            top_niches=top_niches,
            market_summary=load_market_summary_rows(selected_market),
            trash_summary=trash_summary,
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
        prospect = require_prospect_access(prospect_id)
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
            lead_search=build_lead_search_context(
                "leads",
                query=filters["q"],
                param_name="q",
                hidden_fields=[
                    ("stage", filters["stage"]),
                    ("market", filters["market"]),
                    ("niche", filters["niche"]),
                    ("limit", filters["limit"]),
                ],
                reset_args={
                    key: value
                    for key, value in {
                        "stage": filters["stage"],
                        "market": filters["market"],
                        "niche": filters["niche"],
                        "limit": filters["limit"],
                    }.items()
                    if value
                },
                results=lead_rows[:8] if filters["q"] else None,
            ),
            leads=lead_rows,
            stage_options=PIPELINE_STAGE_BUCKETS,
            market_options=build_market_options(filters["market"]),
            niche_options=load_distinct_values("niche", market=filters["market"]),
        )

    @app.get("/trash")
    def trash_can() -> str:
        selected_market = selected_market_from_request()
        selected_category = normalize_trash_category(request.args.get("category"))
        summary = load_trash_summary(selected_market)
        rows = load_trash_rows(selected_market, selected_category)
        summary["selected_count"] = len(rows)
        summary["selected_media_due"] = sum(1 for row in rows if row.get("trash_can_purge"))
        return render_template(
            "dashboard/trash.html",
            active_page="trash",
            market_filter=market_filter_context(selected_market),
            category_filter={
                "selected": selected_category,
                "options": [
                    {
                        "value": key,
                        "label": label,
                        "count": summary.get("category_counts", {}).get(key, 0),
                    }
                    for key, label in TRASH_CATEGORY_OPTIONS
                ],
            },
            rows=rows,
            summary=summary,
            retention_days=TRASH_MEDIA_RETENTION_DAYS,
            message=trash_message_from_code(request.args.get("result")),
        )

    @app.post("/trash/<int:prospect_id>/restore")
    def restore_trash(prospect_id: int):
        prospect = require_prospect_access(prospect_id)
        selected_market = request.form.get("market", "").strip()
        selected_category = normalize_trash_category(request.form.get("category"))
        connection = get_connection()
        restore_trashed_prospect(connection, prospect)
        connection.commit()
        redirect_args = {"result": "restored"}
        if selected_market:
            redirect_args["market"] = selected_market
        if selected_category != "all":
            redirect_args["category"] = selected_category
        return redirect(url_for("trash_can", **redirect_args))

    @app.post("/trash/purge-media")
    def purge_trash_media():
        selected_market = request.form.get("market", "").strip()
        selected_category = normalize_trash_category(request.form.get("category"))
        connection = get_connection()
        result = purge_due_trash_media(
            connection,
            market=selected_market,
            category=selected_category,
        )
        connection.commit()
        code = f"purged:{result['prospects_purged']}:{result['files_deleted']}"
        redirect_args = {"result": code}
        if selected_market:
            redirect_args["market"] = selected_market
        if selected_category != "all":
            redirect_args["category"] = selected_category
        return redirect(url_for("trash_can", **redirect_args))

    @app.get("/crm")
    def crm() -> str:
        selected_market = selected_market_from_request()
        lead_search_query = request.args.get("lead_q", "").strip()
        groups = load_crm_groups(selected_market)
        return render_template(
            "dashboard/crm.html",
            active_page="crm",
            market_filter=market_filter_context(selected_market),
            lead_search=build_lead_search_context(
                "crm",
                query=lead_search_query,
                market=selected_market,
                hidden_fields=[("market", selected_market)],
                reset_args=overview_market_args(selected_market),
            ),
            groups=groups,
            stages=CRM_STAGES,
        )

    @app.get("/tasks")
    def tasks_board() -> str:
        filters = task_filters_from_request()
        task_rows = load_global_tasks(filters)
        summary_rows = load_global_tasks({**filters, "status": "", "due_bucket": ""})
        return render_template(
            "dashboard/tasks.html",
            active_page="tasks",
            filters=filters,
            market_options=build_market_options(filters["market"]),
            task_type_options=task_service.TASK_TYPE_OPTIONS,
            task_priority_options=task_service.TASK_PRIORITY_OPTIONS,
            task_status_options=task_service.TASK_STATUS_OPTIONS,
            due_bucket_options=task_due_bucket_options(),
            summary=task_summary(summary_rows),
            grouped_tasks=group_tasks_for_display(task_rows),
            message=task_message_from_code(request.args.get("result")),
        )

    @app.get("/tasks/<int:task_id>")
    def task_detail(task_id: int) -> str:
        task = require_task_access(task_id)
        return render_template(
            "dashboard/task_detail.html",
            active_page="tasks",
            task=task,
            contacts=load_contacts(int(task["prospect_id"])),
            task_type_options=task_service.TASK_TYPE_OPTIONS,
            task_priority_options=task_service.TASK_PRIORITY_OPTIONS,
            task_status_options=task_service.TASK_STATUS_OPTIONS,
            message=task_message_from_code(request.args.get("result")),
        )

    @app.post("/tasks/<int:task_id>/update")
    def update_task(task_id: int):
        task = require_task_access(task_id)
        try:
            contact_id = parse_optional_int(request.form.get("contact_id"))
            contact_snapshot = task_contact_snapshot(int(task["prospect_id"]), contact_id, request.form)
            task_service.update_task(
                get_connection(),
                task_id,
                task_type=request.form.get("task_type"),
                title=request.form.get("title"),
                priority=request.form.get("priority"),
                status=request.form.get("status"),
                due_date=request.form.get("due_date"),
                due_time=request.form.get("due_time"),
                assigned_to=request.form.get("assigned_to"),
                contact_id=contact_snapshot["contact_id"],
                contact_name=contact_snapshot["contact_name"],
                contact_email=contact_snapshot["contact_email"],
                contact_phone=contact_snapshot["contact_phone"],
                notes=request.form.get("notes"),
                outcome_notes=request.form.get("outcome_notes"),
            )
        except ValueError as exc:
            return redirect(url_for("task_detail", task_id=task_id, result=f"error:{exc}"))
        get_connection().commit()
        return redirect_after_task_action(task, "updated")

    @app.post("/tasks/<int:task_id>/complete")
    def complete_task(task_id: int):
        task = require_task_access(task_id)
        task_service.complete_task(
            get_connection(),
            task_id,
            outcome_notes=request.form.get("outcome_notes"),
        )
        get_connection().commit()
        return redirect_after_task_action(task, "completed")

    @app.post("/tasks/<int:task_id>/cancel")
    def cancel_task(task_id: int):
        task = require_task_access(task_id)
        task_service.cancel_task(
            get_connection(),
            task_id,
            outcome_notes=request.form.get("outcome_notes"),
        )
        get_connection().commit()
        return redirect_after_task_action(task, "cancelled")

    @app.post("/tasks/<int:task_id>/snooze")
    def snooze_task(task_id: int):
        task = require_task_access(task_id)
        snooze_until = request.form.get("snooze_until") or request.form.get("quick_snooze")
        try:
            task_service.snooze_task(get_connection(), task_id, snooze_until=snooze_until)
        except ValueError as exc:
            return redirect(url_for("task_detail", task_id=task_id, result=f"error:{exc}"))
        get_connection().commit()
        return redirect_after_task_action(task, "snoozed")

    @app.get("/outbound")
    @require_dashboard_permission("outbound")
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
    @require_dashboard_permission("outbound")
    def create_outbound_queue():
        filters = outbound_filters_from_request(source=request.form)
        limit = parse_outbound_queue_limit(request.form.get("queue_limit"))
        try:
            created, skipped = create_step_1_send_queue(filters, limit=limit)
        except ValueError as exc:
            return redirect(outbound_url(filters, result=f"error:{exc}"))
        return redirect(outbound_url(filters, result=f"queued:{created}:{skipped}"))

    @app.get("/send")
    @require_dashboard_permission("send")
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
    @login_required
    @auth_service.admin_required
    @require_dashboard_permission("send")
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
    @require_dashboard_permission("send")
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

    @app.get("/quotes")
    @require_dashboard_permission("quotes")
    def quotes_list() -> str:
        return render_template(
            "dashboard/quotes_list.html",
            active_page="quotes",
            quotes=list_quotes_for_current_user(),
            message=quote_message_from_code(request.args.get("result")),
        )

    @app.get("/quotes/new")
    @require_dashboard_permission("quotes")
    def new_quote() -> str:
        try:
            prospect_id = parse_optional_int(request.args.get("prospect_id"))
        except ValueError:
            abort(400)
        if prospect_id is None:
            abort(400)
        prospect = require_prospect_access(prospect_id)
        contacts = load_contacts(prospect_id)
        return render_template(
            "dashboard/quote_builder.html",
            active_page="quotes",
            mode="new",
            quote=None,
            prospect=prospect,
            primary_contact=primary_contact_from_contacts(contacts),
            catalog=quote_catalog_view(),
            form_state=quote_form_state(prospect, primary_contact_from_contacts(contacts)),
            form_action=url_for("create_quote"),
            error="",
        )

    @app.post("/quotes/new")
    @require_dashboard_permission("quotes")
    def create_quote():
        try:
            prospect_id = parse_optional_int(request.form.get("prospect_id"))
        except ValueError:
            abort(400)
        if prospect_id is None:
            abort(400)
        prospect = require_prospect_access(prospect_id)
        contacts = load_contacts(prospect_id)
        primary_contact = primary_contact_from_contacts(contacts)
        catalog = quote_service.load_quote_catalog()
        try:
            payload = parse_quote_builder_form(request.form, prospect=prospect, catalog=catalog)
            connection = get_connection()
            quote = quote_service.create_quote_for_prospect(
                connection,
                prospect_id,
                payload["package_key"],
            )
            quote_service.update_quote_header(connection, int(quote["id"]), **payload["header"])
            quote_service.replace_quote_line_items(connection, int(quote["id"]), payload["line_items"])
            sync_quote_territory_fields(connection, int(quote["id"]), prospect)
            connection.commit()
        except ValueError as exc:
            if "connection" in locals():
                connection.rollback()
            return (
                render_template(
                    "dashboard/quote_builder.html",
                    active_page="quotes",
                    mode="new",
                    quote=None,
                    prospect=prospect,
                    primary_contact=primary_contact,
                    catalog=quote_catalog_view(catalog),
                    form_state=quote_form_state(prospect, primary_contact, form=request.form),
                    form_action=url_for("create_quote"),
                    error=str(exc),
                ),
                400,
            )
        return redirect(url_for("quote_detail", quote_id=quote["id"]))

    @app.get("/quotes/<int:quote_id>")
    @require_dashboard_permission("quotes")
    def quote_detail(quote_id: int) -> str:
        quote, prospect = require_quote_access(quote_id)
        contract_rows = contract_service.list_contracts_for_quote(get_connection(), quote_id)
        for contract in contract_rows:
            contract["generated_files"] = contract_generated_file_info(contract)
        return render_template(
            "dashboard/quote_detail.html",
            active_page="quotes",
            quote=quote,
            prospect=prospect,
            contracts=contract_rows,
            latest_contract=contract_rows[0] if contract_rows else None,
            message=quote_message_from_code(request.args.get("result")),
        )

    @app.get("/quotes/<int:quote_id>/edit")
    @require_dashboard_permission("quotes")
    def edit_quote(quote_id: int) -> str:
        quote, prospect = require_quote_access(quote_id)
        contacts = load_contacts(int(prospect["id"]))
        primary_contact = primary_contact_from_contacts(contacts)
        return render_template(
            "dashboard/quote_builder.html",
            active_page="quotes",
            mode="edit",
            quote=quote,
            prospect=prospect,
            primary_contact=primary_contact,
            catalog=quote_catalog_view(),
            form_state=quote_form_state(prospect, primary_contact, quote=quote),
            form_action=url_for("update_quote", quote_id=quote_id),
            error="",
        )

    @app.post("/quotes/<int:quote_id>/edit")
    @require_dashboard_permission("quotes")
    def update_quote(quote_id: int):
        connection = get_connection()
        quote, prospect = require_quote_access(quote_id)
        contacts = load_contacts(int(prospect["id"]))
        primary_contact = primary_contact_from_contacts(contacts)
        catalog = quote_service.load_quote_catalog()
        try:
            payload = parse_quote_builder_form(request.form, prospect=prospect, catalog=catalog)
            quote_service.update_quote_header(connection, quote_id, **payload["header"])
            quote_service.replace_quote_line_items(connection, quote_id, payload["line_items"])
            quote_service.log_quote_event(
                connection,
                quote_id,
                int(prospect["id"]),
                "quote_updated",
                metadata={"package_key": payload["package_key"]},
            )
            connection.commit()
        except ValueError as exc:
            connection.rollback()
            return (
                render_template(
                    "dashboard/quote_builder.html",
                    active_page="quotes",
                    mode="edit",
                    quote=quote,
                    prospect=prospect,
                    primary_contact=primary_contact,
                    catalog=quote_catalog_view(catalog),
                    form_state=quote_form_state(prospect, primary_contact, quote=quote, form=request.form),
                    form_action=url_for("update_quote", quote_id=quote_id),
                    error=str(exc),
                ),
                400,
            )
        return redirect(url_for("quote_detail", quote_id=quote_id, result="saved"))

    @app.get("/quotes/<int:quote_id>/export/text")
    @require_dashboard_permission("quotes")
    def quote_export_text(quote_id: int):
        connection = get_connection()
        quote, prospect = require_quote_access(quote_id)
        text = quote_exports.render_email_text(quote, prospect)
        export_file = quote_exports.write_text_export(connection, quote, text)
        quote_exports.log_export_event(
            connection,
            quote,
            event_type="quote_exported_text",
            export_file=export_file,
        )
        connection.commit()
        return current_app.response_class(text, content_type="text/plain; charset=utf-8")

    @app.get("/quotes/<int:quote_id>/export.txt")
    @require_dashboard_permission("quotes")
    def quote_export_text_legacy(quote_id: int):
        return redirect(url_for("quote_export_text", quote_id=quote_id))

    @app.get("/quotes/<int:quote_id>/export/html")
    @require_dashboard_permission("quotes")
    def quote_export_html(quote_id: int):
        connection = get_connection()
        quote, prospect = require_quote_access(quote_id)
        context = quote_exports.build_export_context(quote, prospect)
        html = render_template("dashboard/quote_printable.html", **context)
        export_file = quote_exports.write_html_export(connection, quote, html)
        quote_exports.log_export_event(
            connection,
            quote,
            event_type="quote_exported_html",
            export_file=export_file,
        )
        connection.commit()
        return current_app.response_class(html, content_type="text/html; charset=utf-8")

    @app.get("/quotes/<int:quote_id>/print")
    @require_dashboard_permission("quotes")
    def quote_printable(quote_id: int):
        return redirect(url_for("quote_export_html", quote_id=quote_id))

    @app.post("/quotes/<int:quote_id>/status")
    @require_dashboard_permission("quotes")
    def update_quote_status(quote_id: int):
        require_quote_access(quote_id)
        status = str(request.form.get("status") or "").strip().lower()
        if status not in {"sent", "accepted", "declined"}:
            abort(400)
        note = str(request.form.get("note") or "").strip() or None
        try:
            handle_quote_lifecycle_action(
                get_connection(),
                quote_id,
                status,
                note=note,
                close_lost=request.form.get("confirm_close_lost") == "1",
            )
        except ValueError:
            abort(404)
        return redirect(url_for("quote_detail", quote_id=quote_id, result=f"status:{status}"))

    @app.post("/quotes/<int:quote_id>/mark-sent")
    @require_dashboard_permission("quotes")
    def mark_quote_sent(quote_id: int):
        require_quote_access(quote_id)
        try:
            handle_quote_lifecycle_action(
                get_connection(),
                quote_id,
                "sent",
                note=str(request.form.get("note") or "").strip() or None,
            )
        except ValueError:
            abort(404)
        return redirect(url_for("quote_detail", quote_id=quote_id, result="status:sent"))

    @app.post("/quotes/<int:quote_id>/mark-accepted")
    @require_dashboard_permission("quotes")
    def mark_quote_accepted(quote_id: int):
        require_quote_access(quote_id)
        try:
            handle_quote_lifecycle_action(
                get_connection(),
                quote_id,
                "accepted",
                note=str(request.form.get("note") or "").strip() or None,
            )
        except ValueError:
            abort(404)
        return redirect(url_for("quote_detail", quote_id=quote_id, result="status:accepted"))

    @app.post("/quotes/<int:quote_id>/mark-declined")
    @require_dashboard_permission("quotes")
    def mark_quote_declined(quote_id: int):
        require_quote_access(quote_id)
        try:
            handle_quote_lifecycle_action(
                get_connection(),
                quote_id,
                "declined",
                note=str(request.form.get("note") or "").strip() or None,
                close_lost=request.form.get("confirm_close_lost") == "1",
            )
        except ValueError:
            abort(404)
        result = "status:declined_closed_lost" if request.form.get("confirm_close_lost") == "1" else "status:declined"
        return redirect(url_for("quote_detail", quote_id=quote_id, result=result))

    @app.post("/quotes/<int:quote_id>/create-revision")
    @app.post("/quotes/<int:quote_id>/revision")
    @require_dashboard_permission("quotes")
    def create_quote_revision(quote_id: int):
        connection = get_connection()
        require_quote_access(quote_id)
        try:
            quote = quote_service.create_quote_revision(connection, quote_id)
            prospect = require_prospect_access(int(quote["prospect_id"]))
            sync_quote_territory_fields(connection, int(quote["id"]), prospect)
            insert_quote_lifecycle_outreach_event(
                connection,
                quote=quote,
                lifecycle_event="quote_revision_created",
            )
            connection.commit()
        except ValueError:
            connection.rollback()
            abort(404)
        return redirect(url_for("quote_detail", quote_id=quote["id"], result="revision_created"))

    @app.post("/quotes/<int:quote_id>/delete")
    @require_dashboard_permission("quotes")
    def delete_quote(quote_id: int):
        if request.form.get("confirm_delete") != "1":
            abort(400)
        connection = get_connection()
        require_quote_access(quote_id)
        try:
            quote = quote_service.delete_quote(
                connection,
                quote_id,
                note=str(request.form.get("note") or "").strip() or None,
            )
            connection.commit()
        except ValueError:
            connection.rollback()
            abort(404)
        return_to = str(request.form.get("return_to") or "").strip().lower()
        if return_to == "case":
            return redirect(url_for("case_file", prospect_id=int(quote["prospect_id"]), quote_result="deleted"))
        return redirect(url_for("quotes_list", result="deleted"))

    @app.get("/contracts")
    @require_dashboard_permission("quotes")
    def contracts_list() -> str:
        selected_filter = normalize_contract_list_filter(request.args.get("filter"))
        all_contracts = list_contracts_for_current_user(limit=1000)
        visible_contracts = [
            contract
            for contract in all_contracts
            if contract_status_filter_bucket(contract.get("status")) == selected_filter
        ]
        return render_template(
            "dashboard/contracts_list.html",
            active_page="contracts",
            contracts=visible_contracts,
            contract_filter=selected_filter,
            contract_filter_options=contract_filter_options(all_contracts, selected_filter),
            message=contract_message_from_code(request.args.get("result")),
        )

    @app.get("/quotes/<int:quote_id>/contract/new")
    @require_dashboard_permission("quotes")
    def new_contract_from_quote(quote_id: int) -> str:
        quote, prospect = require_quote_access(quote_id)
        contract_rows = contract_service.list_contracts_for_quote(get_connection(), quote_id)
        return render_template(
            "dashboard/contract_builder.html",
            active_page="contracts",
            mode="new",
            contract=None,
            quote=quote,
            prospect=prospect,
            existing_contracts=contract_rows,
            form_state=contract_form_state(prospect, quote),
            form_action=url_for("create_contract_from_quote", quote_id=quote_id),
            error="",
        )

    @app.post("/quotes/<int:quote_id>/contract")
    @require_dashboard_permission("quotes")
    def create_contract_from_quote(quote_id: int):
        quote, prospect = require_quote_access(quote_id)
        contract_rows = contract_service.list_contracts_for_quote(get_connection(), quote_id)
        try:
            payload = parse_contract_builder_form(request.form)
            connection = get_connection()
            user = current_dashboard_user()
            contract = contract_service.create_contract_from_quote(
                connection,
                quote_id,
                created_by=user.username if user else None,
            )
            sync_contract_territory_fields(connection, int(contract["id"]), prospect)
            contract_service.update_contract_header(connection, int(contract["id"]), payload["header"])
            contract_service.update_contract_signers(connection, int(contract["id"]), payload["signers"])
            contract_service.update_contract_sections(connection, int(contract["id"]), payload["sections"])
            connection.commit()
        except ValueError as exc:
            if "connection" in locals():
                connection.rollback()
            return (
                render_template(
                    "dashboard/contract_builder.html",
                    active_page="contracts",
                    mode="new",
                    contract=None,
                    quote=quote,
                    prospect=prospect,
                    existing_contracts=contract_rows,
                    form_state=contract_form_state(prospect, quote, form=request.form),
                    form_action=url_for("create_contract_from_quote", quote_id=quote_id),
                    error=str(exc),
                ),
                400,
            )
        return redirect(url_for("contract_detail", contract_id=contract["id"], result="created"))

    @app.get("/contracts/<int:contract_id>")
    @require_dashboard_permission("quotes")
    def contract_detail(contract_id: int) -> str:
        contract, quote, prospect = require_contract_access(contract_id)
        contract["generated_files"] = contract_generated_file_info(contract)
        render_context = contract_exports.build_contract_render_context(get_connection(), contract_id)
        docusign = contract_docusign_view(contract, render_context=render_context)
        return render_template(
            "dashboard/contract_detail.html",
            active_page="contracts",
            contract=contract,
            quote=quote,
            prospect=prospect,
            render_context=render_context,
            docusign=docusign,
            message=contract_message_from_code(request.args.get("result")),
        )

    @app.get("/contracts/<int:contract_id>/edit")
    @require_dashboard_permission("quotes")
    def edit_contract(contract_id: int) -> str:
        contract, quote, prospect = require_contract_access(contract_id)
        return render_template(
            "dashboard/contract_builder.html",
            active_page="contracts",
            mode="edit",
            contract=contract,
            quote=quote,
            prospect=prospect,
            existing_contracts=[],
            form_state=contract_form_state(prospect, quote, contract=contract),
            form_action=url_for("update_contract", contract_id=contract_id),
            error="",
        )

    @app.post("/contracts/<int:contract_id>/edit")
    @require_dashboard_permission("quotes")
    def update_contract(contract_id: int):
        contract, quote, prospect = require_contract_access(contract_id)
        connection = get_connection()
        try:
            payload = parse_contract_builder_form(request.form)
            contract_service.update_contract_header(connection, contract_id, payload["header"])
            contract_service.update_contract_signers(connection, contract_id, payload["signers"])
            contract_service.update_contract_sections(connection, contract_id, payload["sections"])
            connection.commit()
        except ValueError as exc:
            connection.rollback()
            return (
                render_template(
                    "dashboard/contract_builder.html",
                    active_page="contracts",
                    mode="edit",
                    contract=contract,
                    quote=quote,
                    prospect=prospect,
                    existing_contracts=[],
                    form_state=contract_form_state(prospect, quote, contract=contract, form=request.form),
                    form_action=url_for("update_contract", contract_id=contract_id),
                    error=str(exc),
                ),
                400,
            )
        return redirect(url_for("contract_detail", contract_id=contract_id, result="saved"))

    @app.post("/contracts/<int:contract_id>/generate")
    @require_dashboard_permission("quotes")
    def generate_contract(contract_id: int):
        require_contract_access(contract_id)
        connection = get_connection()
        try:
            contract_exports.generate_contract_artifacts(connection, contract_id)
            connection.commit()
        except ValueError as exc:
            connection.rollback()
            return redirect(url_for("contract_detail", contract_id=contract_id, result=f"error:{exc}"))
        except Exception:
            connection.rollback()
            logger.exception("Contract generation failed for contract %s", contract_id)
            return redirect(url_for("contract_detail", contract_id=contract_id, result="error:Contract generation failed."))
        return redirect(url_for("contract_detail", contract_id=contract_id, result="generated"))

    @app.post("/contracts/<int:contract_id>/send-docusign")
    @require_dashboard_permission("quotes")
    def send_contract_docusign(contract_id: int):
        contract, _quote, _prospect = require_contract_access(contract_id)
        if request.form.get("confirm_docusign_send") != "1":
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result="error:Confirm the DocuSign send first. No envelope was created.",
                )
            )

        config_status = docusign_config_status()
        allow_active_envelope = request.form.get("resend_supersede") == "1"
        generated_docx_path = resolve_contract_generated_docx_path(contract)
        if generated_docx_path is None and request.form.get("generate_before_send") == "1":
            connection = get_connection()
            try:
                contract_exports.generate_contract_artifacts(connection, contract_id)
                connection.commit()
                contract = contract_service.load_contract(connection, contract_id) or contract
                generated_docx_path = resolve_contract_generated_docx_path(contract)
            except ValueError as exc:
                connection.rollback()
                return redirect(url_for("contract_detail", contract_id=contract_id, result=f"error:{exc}"))
            except Exception:
                connection.rollback()
                logger.exception("Contract generation failed before DocuSign send for contract %s", contract_id)
                return redirect(
                    url_for(
                        "contract_detail",
                        contract_id=contract_id,
                        result="error:Contract generation failed before DocuSign send.",
                    )
                )

        render_context = contract_exports.build_contract_render_context(get_connection(), contract_id)
        preflight = build_contract_docusign_preflight(
            contract,
            config_status=config_status,
            allow_active_envelope=allow_active_envelope,
            render_context=render_context,
        )
        if preflight["errors"]:
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result="error:" + " ".join(preflight["errors"]),
                )
            )

        generated_docx_path = resolve_contract_generated_docx_path(contract)
        if generated_docx_path is None:
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result="error:Generated DOCX file is missing. No envelope was created.",
                )
            )
        signers = contract_required_docusign_signers(contract)
        envelope_status = "created" if request.form.get("draft_only") == "1" else "sent"
        old_envelope_id = str(contract.get("docusign_envelope_id") or "").strip()
        try:
            send_result = docusign_client.send_envelope_from_document(
                contract,
                generated_docx_path,
                signers,
                status=envelope_status,
            )
            envelope_id = str(send_result.get("envelope_id") or "").strip()
            if not envelope_id:
                raise ValueError("DocuSign did not return an envelope ID.")
            returned_status = normalize_docusign_status(send_result.get("status")) or envelope_status
        except Exception as exc:
            logger.warning(
                "DocuSign send failed for contract %s with %s.",
                contract_id,
                type(exc).__name__,
            )
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result=f"error:{contract_docusign_error_message(exc)}",
                )
            )

        connection = get_connection()
        try:
            contract_service.update_docusign_status(
                connection,
                contract_id,
                envelope_id=envelope_id,
                status=returned_status,
                metadata={
                    "source": "dashboard_send_docusign",
                    "environment": config_status["environment"],
                    "requested_status": envelope_status,
                    "status_date_time": send_result.get("status_date_time"),
                    "uri": send_result.get("uri"),
                    "old_envelope_id": old_envelope_id if allow_active_envelope else None,
                },
            )
            contract_service.update_contract_status(
                connection,
                contract_id,
                "sent",
                note="DocuSign envelope created from generated contract.",
                metadata={
                    "docusign_envelope_id": envelope_id,
                    "docusign_status": returned_status,
                    "docusign_environment": config_status["environment"],
                    "draft_only": envelope_status == "created",
                },
            )
            contract_service.log_contract_event(
                connection,
                contract_id,
                event_type="contract_docusign_sent",
                status=returned_status,
                note="DocuSign envelope created from generated contract.",
                metadata={
                    "envelope_id": envelope_id,
                    "requested_status": envelope_status,
                    "environment": config_status["environment"],
                    "signer_count": len(signers),
                    "resend_supersede": allow_active_envelope,
                },
            )
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("Failed to store DocuSign envelope details for contract %s", contract_id)
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result="error:DocuSign envelope was created, but local CRM status could not be saved. Refresh status before retrying.",
                )
            )

        result_code = "docusign_draft_created" if envelope_status == "created" else "docusign_sent"
        return redirect(url_for("contract_detail", contract_id=contract_id, result=result_code))

    @app.post("/contracts/<int:contract_id>/refresh-docusign-status")
    @require_dashboard_permission("quotes")
    def refresh_contract_docusign_status(contract_id: int):
        contract, _quote, _prospect = require_contract_access(contract_id)
        envelope_id = str(contract.get("docusign_envelope_id") or "").strip()
        if not envelope_id:
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result="error:No DocuSign envelope ID is stored for this contract.",
                )
            )

        config_status = docusign_config_status()
        if not config_status["configured"]:
            config_errors = "; ".join(config_status.get("errors") or [])
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result=f"error:DocuSign config is incomplete. {config_errors}",
                )
            )

        try:
            status_result = docusign_client.get_envelope_status(envelope_id)
            returned_status = normalize_docusign_status(status_result.get("status")) or "unknown"
        except Exception as exc:
            logger.warning(
                "DocuSign status refresh failed for contract %s with %s.",
                contract_id,
                type(exc).__name__,
            )
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result=f"error:{contract_docusign_error_message(exc)}",
                )
            )

        connection = get_connection()
        try:
            contract_service.update_docusign_status(
                connection,
                contract_id,
                envelope_id=str(status_result.get("envelope_id") or envelope_id),
                status=returned_status,
                metadata={
                    "source": "dashboard_refresh_docusign_status",
                    "environment": config_status["environment"],
                    "status_changed_date_time": status_result.get("status_changed_date_time"),
                    "sent_date_time": status_result.get("sent_date_time"),
                    "completed_date_time": status_result.get("completed_date_time"),
                },
            )
            lifecycle = DOCUSIGN_REFRESH_STATUS_EVENTS.get(returned_status)
            if lifecycle:
                contract_status, event_type = lifecycle
                contract_service.update_contract_status(
                    connection,
                    contract_id,
                    contract_status,
                    note=f"DocuSign envelope status refreshed as {returned_status}.",
                    metadata={
                        "docusign_envelope_id": envelope_id,
                        "docusign_status": returned_status,
                        "docusign_environment": config_status["environment"],
                    },
                )
                contract_service.log_contract_event(
                    connection,
                    contract_id,
                    event_type=event_type,
                    status=returned_status,
                    note=f"DocuSign envelope status refreshed as {returned_status}.",
                    metadata={
                        "envelope_id": envelope_id,
                        "environment": config_status["environment"],
                    },
                )
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("Failed to store DocuSign status for contract %s", contract_id)
            return redirect(
                url_for(
                    "contract_detail",
                    contract_id=contract_id,
                    result="error:DocuSign status was fetched, but local CRM status could not be saved.",
                )
            )

        return redirect(url_for("contract_detail", contract_id=contract_id, result="docusign_refreshed"))

    @app.post("/webhooks/docusign")
    def docusign_webhook():
        if not docusign_webhook_enabled():
            abort(404)
        if not docusign_webhook_secret_configured():
            return jsonify({"ok": False, "error": "webhook_not_configured"}), 403
        if not validate_docusign_webhook_request():
            logger.warning("Rejected DocuSign webhook with invalid shared secret.")
            return jsonify({"ok": False, "error": "forbidden"}), 403

        payload = parse_docusign_webhook_payload(request)
        envelope_id = str(payload.get("envelope_id") or "").strip()
        envelope_status = normalize_docusign_status(payload.get("status"))
        event_name = str(payload.get("event") or "").strip()
        if not envelope_id or not envelope_status:
            logger.warning("Rejected DocuSign webhook with missing envelope id or status.")
            return jsonify({"ok": False, "error": "missing_envelope_or_status"}), 400

        connection = get_connection()
        contract = find_contract_by_docusign_envelope_id(connection, envelope_id)
        if contract is None:
            contract_service.log_contract_event(
                connection,
                None,
                event_type="docusign_webhook_contract_not_found",
                status=envelope_status,
                note="DocuSign webhook did not match a stored contract envelope.",
                metadata={
                    "envelope_id": mask_docusign_envelope_id(envelope_id),
                    "event": event_name,
                    "source": "docusign_webhook",
                },
            )
            connection.commit()
            return jsonify({"ok": True, "matched": False}), 202

        try:
            contract_service.update_docusign_status(
                connection,
                int(contract["id"]),
                envelope_id=envelope_id,
                status=envelope_status,
                metadata={
                    "source": "docusign_webhook",
                    "event": event_name,
                    "received_at": pipeline_db.utc_now(),
                },
            )
            lifecycle = DOCUSIGN_REFRESH_STATUS_EVENTS.get(envelope_status)
            if lifecycle:
                contract_status, event_type = lifecycle
                contract_service.update_contract_status(
                    connection,
                    int(contract["id"]),
                    contract_status,
                    note=f"DocuSign webhook status received as {envelope_status}.",
                    metadata={
                        "docusign_envelope_id": envelope_id,
                        "docusign_status": envelope_status,
                        "docusign_event": event_name,
                        "source": "docusign_webhook",
                    },
                )
                contract_service.log_contract_event(
                    connection,
                    int(contract["id"]),
                    event_type=event_type,
                    status=envelope_status,
                    note=f"DocuSign webhook status received as {envelope_status}.",
                    metadata={
                        "envelope_id": envelope_id,
                        "event": event_name,
                        "source": "docusign_webhook",
                    },
                )
            else:
                contract_service.log_contract_event(
                    connection,
                    int(contract["id"]),
                    event_type="contract_docusign_webhook_status",
                    status=envelope_status,
                    note=f"DocuSign webhook status received as {envelope_status}.",
                    metadata={
                        "envelope_id": envelope_id,
                        "event": event_name,
                        "source": "docusign_webhook",
                    },
                )
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception(
                "Failed to apply DocuSign webhook status for contract %s.",
                contract.get("id"),
            )
            return jsonify({"ok": False, "error": "status_update_failed"}), 500

        return jsonify({"ok": True, "matched": True, "status": envelope_status})

    @app.get("/contracts/<int:contract_id>/preview")
    @require_dashboard_permission("quotes")
    def contract_preview(contract_id: int):
        require_contract_access(contract_id)
        html = contract_exports.render_contract_html(get_connection(), contract_id)
        return current_app.response_class(html, content_type="text/html; charset=utf-8")

    @app.get("/contracts/<int:contract_id>/download/docx")
    @require_dashboard_permission("quotes")
    def download_contract_docx(contract_id: int):
        contract, _quote, _prospect = require_contract_access(contract_id)
        filename = f"{contract.get('contract_key') or 'contract'}.docx"
        return contract_file_response(contract, "generated_docx_path", filename)

    @app.get("/contracts/<int:contract_id>/download/html")
    @require_dashboard_permission("quotes")
    def download_contract_html(contract_id: int):
        contract, _quote, _prospect = require_contract_access(contract_id)
        filename = f"{contract.get('contract_key') or 'contract'}.html"
        return contract_file_response(contract, "generated_html_path", filename)

    @app.post("/contracts/<int:contract_id>/void")
    @require_dashboard_permission("quotes")
    def void_contract(contract_id: int):
        require_contract_access(contract_id)
        connection = get_connection()
        contract_service.update_contract_status(
            connection,
            contract_id,
            "voided",
            note=str(request.form.get("note") or "").strip() or "Voided from dashboard.",
            metadata={"source": "dashboard_contract_void"},
        )
        connection.commit()
        return redirect(url_for("contract_detail", contract_id=contract_id, result="voided"))

    @app.post("/contracts/<int:contract_id>/create-revision")
    @require_dashboard_permission("quotes")
    def create_contract_revision(contract_id: int):
        _contract, _quote, prospect = require_contract_access(contract_id)
        connection = get_connection()
        try:
            revision = contract_service.create_contract_revision(connection, contract_id)
            sync_contract_territory_fields(connection, int(revision["id"]), prospect)
            connection.commit()
        except ValueError:
            connection.rollback()
            abort(404)
        return redirect(url_for("contract_detail", contract_id=revision["id"], result="revision_created"))

    @app.get("/crm/stage/<stage>")
    def crm_stage(stage: str) -> str:
        normalized_stage = _normalize_token(stage)
        if normalized_stage not in CRM_BOARD_STAGE_LABELS:
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
            stage_label=CRM_BOARD_STAGE_LABELS[normalized_stage],
            market_filter=market_filter_context(selected_market),
            prospects=prospects,
            stages=CRM_STAGES,
        )

    @app.get("/sales-packet/<int:prospect_id>")
    def sales_packet(prospect_id: int) -> str:
        prospect = require_prospect_access(prospect_id)
        if not sales_packet_available(prospect):
            abort(404)

        artifacts = load_artifacts(prospect_id)
        audits = load_audits(prospect_id)
        contacts = load_contacts(prospect_id)
        case_tasks = load_case_tasks(prospect_id)
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
        prospect = require_prospect_access(prospect_id)
        if not sales_packet_available(prospect):
            abort(404)
        notes = str(request.form.get("sales_notes") or "").strip()
        connection = get_connection()
        save_sales_notes(connection, prospect=prospect, notes=notes)
        connection.commit()
        return redirect(url_for("sales_packet", prospect_id=prospect_id, result="notes_saved"))

    @app.get("/pipeline")
    @require_dashboard_permission("run_jobs")
    def pipeline() -> str:
        selected_market = selected_market_from_request()
        if selected_market:
            return redirect(url_for("run_controls", market=selected_market))
        return redirect(url_for("run_controls"))

    @app.get("/run")
    @require_dashboard_permission("run_jobs")
    def run_controls() -> str:
        selected_market, territory_error = normalize_selected_market_for_user(
            selected_market_from_request(),
            allow_unknown_for_admin=True,
        )
        selected_niches = selected_niches_from_request()
        market_options = build_market_options(selected_market)
        message = (
            {"status": "error", "message": territory_error}
            if territory_error
            else job_message_from_code(request.args.get("result"))
        )
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
            recent_jobs=list_dashboard_jobs_for_current_user(limit=10),
            message=message,
            places_limit_default=50,
            places_limit_max=PLACES_JOB_LIMIT,
            audit_limit_default=20,
            audit_limit_max=AUDIT_JOB_LIMIT,
            selected_audit_mode=selected_audit_mode_from_request(),
            artifact_limit_default=25,
        )

    @app.post("/run/full-pipeline")
    @require_dashboard_permission("run_jobs")
    def start_full_market_pipeline():
        try:
            job_key = start_full_pipeline_from_form(request.form)
        except ValueError as exc:
            return redirect(run_controls_url_from_form(request.form, result=f"error:{exc}"))
        return redirect(url_for("job_detail", job_key=job_key))

    @app.get("/jobs")
    @require_dashboard_permission("run_jobs")
    def jobs() -> str:
        selected_market, territory_error = normalize_selected_market_for_user(
            selected_market_from_request(),
            allow_unknown_for_admin=True,
        )
        market_options = build_market_options(selected_market)
        message = (
            {"status": "error", "message": territory_error}
            if territory_error
            else job_message_from_code(request.args.get("result"))
        )
        return render_template(
            "dashboard/jobs.html",
            active_page="jobs",
            jobs=list_dashboard_jobs_for_current_user(),
            job_types={
                key: job
                for key, job in dashboard_jobs.ALLOWED_JOBS.items()
                if key != "full_pipeline"
                and (dashboard_user_is_admin() or key != "reconcile_statuses")
            },
            market_filter=market_filter_context(selected_market),
            job_market_options=[option for option in market_options if option.get("can_run")],
            niche_options=load_distinct_values("niche"),
            message=message,
        )

    @app.get("/jobs/<job_key>")
    @require_dashboard_permission("run_jobs")
    def job_detail(job_key: str) -> str:
        job = dashboard_jobs.get_job(job_key, db_path=app.config["DATABASE_PATH"])
        if job is None:
            abort(404)
        if not current_user_can_access_job(job):
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
    @require_dashboard_permission("run_jobs")
    def start_dashboard_job():
        try:
            job_key = start_job_from_form(request.form)
        except ValueError as exc:
            if request.form.get("source") == "run":
                return redirect(run_controls_url_from_form(request.form, result=f"error:{exc}"))
            return redirect(url_for("jobs", result=f"error:{exc}"))
        return redirect(url_for("job_detail", job_key=job_key))

    @app.get("/jobs/<job_key>/status")
    @require_dashboard_permission("run_jobs")
    def job_status(job_key: str):
        job = dashboard_jobs.get_job(job_key, db_path=app.config["DATABASE_PATH"])
        if job is None:
            abort(404)
        if not current_user_can_access_job(job):
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
    @require_dashboard_permission("run_jobs")
    def run_pipeline_job():
        return redirect(
            run_controls_url_from_form(
                request.form,
                result="error:The old Pipeline runner has been retired. Use the Run tab.",
            )
        )

    @app.get("/markets")
    @require_dashboard_permission("markets")
    def markets() -> str:
        return render_template(
            "dashboard/markets.html",
            active_page="markets",
            markets=load_market_manager_rows(),
            message=market_message_from_query(),
        )

    @app.post("/markets/add")
    @require_dashboard_permission("markets")
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
        prospect = require_prospect_access(prospect_id)
        artifacts = load_artifacts(prospect_id)
        audits = load_audits(prospect_id)
        contacts = load_contacts(prospect_id)
        case_tasks = load_case_tasks(prospect_id)
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
        quote_rows = quote_service.list_quotes_for_prospect(get_connection(), prospect_id)
        contract_rows = (
            contract_service.list_contracts_for_prospect(get_connection(), prospect_id)
            if dashboard_user_has_permission("quotes")
            else []
        )
        for contract in contract_rows:
            contract["generated_files"] = contract_generated_file_info(contract)
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
            task_context=case_tasks,
            task_type_options=task_service.TASK_TYPE_OPTIONS,
            task_priority_options=task_service.TASK_PRIORITY_OPTIONS,
            highlight_task_id=str(request.args.get("task") or ""),
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
            quotes=quote_rows,
            latest_quote=quote_rows[0] if quote_rows else None,
            contracts=contract_rows,
            latest_contract=contract_rows[0] if contract_rows else None,
            review_message=review_message_from_code(request.args.get("review")),
            quote_message=quote_message_from_code(request.args.get("quote_result")),
            contract_message=contract_message_from_code(request.args.get("contract_result")),
        )

    @app.post("/case/<int:prospect_id>/review")
    def record_case_review(prospect_id: int):
        prospect = require_prospect_access(prospect_id)

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
        prospect = require_prospect_access(prospect_id)
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
        prospect = require_prospect_access(prospect_id)
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

    @app.post("/case/<int:prospect_id>/tasks/create")
    def create_case_task(prospect_id: int):
        prospect = require_prospect_access(prospect_id)
        actor = current_dashboard_user()
        try:
            contact_id = parse_optional_int(request.form.get("contact_id"))
            quote_id = parse_optional_int(request.form.get("quote_id"))
            contact_snapshot = task_contact_snapshot(prospect_id, contact_id, request.form)
            connection = get_connection()
            quote_id = task_quote_id_for_prospect(connection, prospect_id, quote_id)
            task_id = task_service.create_task(
                connection,
                prospect_id=prospect_id,
                quote_id=quote_id,
                task_type=request.form.get("task_type"),
                title=request.form.get("title"),
                priority=request.form.get("priority"),
                due_date=request.form.get("due_date"),
                due_time=request.form.get("due_time"),
                assigned_to=request.form.get("assigned_to"),
                contact_id=contact_snapshot["contact_id"],
                contact_name=contact_snapshot["contact_name"],
                contact_email=contact_snapshot["contact_email"],
                contact_phone=contact_snapshot["contact_phone"],
                notes=request.form.get("notes"),
                created_by_user=actor.username if actor else None,
                owner_username=prospect.get("owner_username") or (actor.username if actor else None),
                market_state=prospect_state_from_record(prospect),
            )
        except ValueError as exc:
            return redirect(
                url_for(
                    "case_file",
                    prospect_id=prospect_id,
                    review=f"task_error:{exc}",
                )
            )
        connection.commit()
        return redirect(url_for("case_file", prospect_id=prospect_id, review="task_created", task=task_id))

    @app.post("/case/<int:prospect_id>/visual-review")
    def record_visual_review(prospect_id: int):
        prospect = require_prospect_access(prospect_id)

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
        require_prospect_access(prospect_id)
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
        require_prospect_access(prospect_id)
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
        prospect = require_prospect_access(prospect_id)
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
        require_artifact_path_access(relative_path)
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
        require_project_file_access(file_path)
        return send_file(resolved)

    @app.get("/health")
    def health() -> tuple[str, int, dict[str, str]]:
        return "OK\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    return app


def load_stage_counts(market: str = "") -> list[dict[str, Any]]:
    counts = {stage: 0 for stage in PIPELINE_STAGE_BUCKETS}
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_visible_market_scope(clauses, params, market)
    append_active_prospect_filter(clauses, params)
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


def overview_stage_count_map(stage_counts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        str(item.get("stage") or ""): int(item.get("count") or 0)
        for item in stage_counts
    }


def overview_stage_total(counts: dict[str, int], *stages: str) -> int:
    return sum(counts.get(stage, 0) for stage in stages)


def overview_market_args(selected_market: str = "") -> dict[str, str]:
    return {"market": selected_market} if selected_market else {}


def overview_leads_url(stage: str, selected_market: str = "") -> str:
    return url_for("leads", stage=stage, **overview_market_args(selected_market))


def overview_command_cards(
    stage_counts: list[dict[str, Any]],
    *,
    trash_summary: dict[str, Any],
    selected_market: str,
    open_task_count: int | None,
) -> list[dict[str, Any]]:
    counts = overview_stage_count_map(stage_counts)
    cards = [
        {
            "label": "Eligible for Audit",
            "count": counts.get("ELIGIBLE_FOR_AUDIT", 0),
            "href": overview_leads_url("ELIGIBLE_FOR_AUDIT", selected_market),
        },
        {
            "label": "Pending Review",
            "count": counts.get("PENDING_REVIEW", 0),
            "href": url_for("review_queue", **overview_market_args(selected_market)),
        },
        {
            "label": "Outbound Ready",
            "count": counts.get("OUTREACH_DRAFTED", 0),
            "href": overview_leads_url("OUTREACH_DRAFTED", selected_market),
        },
        {
            "label": "Contact Made / Active Sales",
            "count": overview_stage_total(
                counts,
                "CONTACT_MADE",
                "CALL_BOOKED",
                "PROPOSAL_SENT",
                "CLOSED_WON",
                "PROJECT_ACTIVE",
            ),
            "href": url_for("crm", **overview_market_args(selected_market)),
        },
    ]
    if open_task_count is not None:
        cards.append(
            {
                "label": "Open Tasks",
                "count": open_task_count,
                "href": url_for("tasks_board", **overview_market_args(selected_market)),
            }
        )
    cards.append(
        {
            "label": "Trash",
            "count": int(trash_summary.get("count") or 0),
            "href": url_for("trash_can", **overview_market_args(selected_market)),
        }
    )
    return cards


def overview_metric_groups(
    stage_counts: list[dict[str, Any]],
    *,
    trash_summary: dict[str, Any],
    selected_market: str,
) -> list[dict[str, Any]]:
    counts = overview_stage_count_map(stage_counts)
    grouped_stages: set[str] = set()

    def stage_item(stage: str, label: str) -> dict[str, Any]:
        grouped_stages.add(stage)
        return {
            "key": stage.lower(),
            "label": label,
            "count": counts.get(stage, 0),
            "href": overview_leads_url(stage, selected_market),
        }

    groups = [
        {
            "key": "acquisition",
            "label": "Acquisition",
            "items": [
                stage_item("NEW", "New"),
                stage_item("NO_WEBSITE", "No Website"),
                stage_item("ELIGIBLE_FOR_AUDIT", "Eligible for Audit"),
                stage_item("INELIGIBLE", "Ineligible"),
                stage_item("DISCARDED", "Discarded"),
            ],
        },
        {
            "key": "audit_review",
            "label": "Audit + Review",
            "items": [
                stage_item("AUDIT_READY", "Audit Ready"),
                stage_item("PENDING_REVIEW", "Pending Review"),
                stage_item("REJECTED_REVIEW", "Rejected Review"),
            ],
        },
        {
            "key": "outreach",
            "label": "Outreach",
            "items": [
                stage_item("APPROVED_FOR_OUTREACH", "Approved for Outreach"),
                stage_item("OUTREACH_DRAFTED", "Outreach Drafted"),
                stage_item("OUTREACH_SENT", "Outreach Sent"),
                stage_item("CONTACT_MADE", "Contact Made"),
            ],
        },
        {
            "key": "sales_projects",
            "label": "Sales + Projects",
            "items": [
                stage_item("CALL_BOOKED", "Call Booked"),
                stage_item("PROPOSAL_SENT", "Proposal Sent"),
                stage_item("CLOSED_WON", "Closed Won"),
                stage_item("CLOSED_LOST", "Closed Lost"),
                stage_item("PROJECT_ACTIVE", "Project Active"),
                stage_item("PROJECT_COMPLETE", "Project Complete"),
            ],
        },
        {
            "key": "trash_cleanup",
            "label": "Trash / Cleanup",
            "items": [
                {
                    "key": "trashed",
                    "label": "Trashed",
                    "count": int(trash_summary.get("count") or 0),
                    "href": url_for("trash_can", **overview_market_args(selected_market)),
                },
                {
                    "key": "media_due",
                    "label": "Media Due",
                    "count": int(trash_summary.get("media_due") or 0),
                    "href": url_for("trash_can", **overview_market_args(selected_market)),
                },
                {
                    "key": "media_purged",
                    "label": "Media Purged",
                    "count": int(trash_summary.get("media_purged") or 0),
                    "href": url_for("trash_can", **overview_market_args(selected_market)),
                },
            ],
        },
    ]
    extra_items = [
        stage_item(item["stage"], item["stage"].replace("_", " ").title())
        for item in stage_counts
        if item["stage"] not in grouped_stages
    ]
    if extra_items:
        groups.append({"key": "other", "label": "Other", "items": extra_items})
    for group in groups:
        group["total"] = sum(int(item.get("count") or 0) for item in group["items"])
        group["open"] = group["total"] > 0
    return groups


def load_overview_open_task_count(market: str = "") -> int | None:
    try:
        tasks = load_global_tasks(
            {
                "status": "",
                "task_type": "",
                "priority": "",
                "market": market,
                "assigned_to": "",
                "due_bucket": "",
                "q": "",
            }
        )
    except sqlite3.OperationalError as exc:
        if "crm_tasks" in str(exc).lower():
            return None
        raise
    return sum(1 for task in tasks if task.get("status") in task_service.OPEN_STATUSES)


def load_pipeline_counts(market: str = "") -> list[dict[str, Any]]:
    connection = get_connection()
    count_queries = {
        "DISCOVERED": "qualification_status = 'DISCOVERED'",
        "NO_WEBSITE": (
            "qualification_status = 'NO_WEBSITE' "
            "OR status = 'NO_WEBSITE' "
            "OR next_action = 'COLD_CALL_WEBSITE'"
        ),
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
    market_clauses: list[str] = []
    market_params: list[Any] = []
    append_visible_market_scope(market_clauses, market_params, market)
    for bucket in PIPELINE_COUNT_BUCKETS:
        clauses = [f"({count_queries[bucket]})"]
        params: list[Any] = []
        clauses.extend(market_clauses)
        params.extend(market_params)
        append_active_prospect_filter(clauses, params)
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
        ("no_website", "No Website"),
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
    append_visible_market_scope(clauses, params, market)
    append_active_prospect_filter(clauses, params)
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
        if stage == "NO_WEBSITE":
            bucket = "no_website"
        elif stage in {"DISCARDED", "REJECTED_REVIEW", "CLOSED_LOST"}:
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
    append_visible_market_scope(base_clauses, base_params, market)
    if niches:
        placeholders = ", ".join("?" for _ in niches)
        base_clauses.append(f"niche IN ({placeholders})")
        base_params.extend(niches)
    append_job_protected_filter(base_clauses, base_params)

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
    append_visible_market_scope(clauses, params, market)
    append_active_prospect_filter(clauses, params)
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
    append_visible_market_scope(clauses, params, market)
    append_active_prospect_filter(clauses, params)
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
        append_visible_market_scope(clauses, params, market)
    else:
        append_visible_market_scope(clauses, params, "")
    append_active_prospect_filter(clauses, params)
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
        "no_website": 0,
        "eligible": 0,
        "pending_review": 0,
        "approved_for_outreach": 0,
        "outreach_sent": 0,
        "contact_made": 0,
        "discarded_ineligible": 0,
    }


def add_stage_to_market_summary(counts: dict[str, int], stage: str) -> None:
    counts["total"] += 1
    if stage == "NO_WEBSITE":
        counts["no_website"] += 1
    elif stage == "ELIGIBLE_FOR_AUDIT":
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
    append_visible_market_scope(clauses, params, market)
    append_active_prospect_filter(clauses, params)
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
    configured = visible_configured_markets()
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
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_visible_market_scope(clauses, params, "")
    append_active_prospect_filter(clauses, params)
    rows = get_connection().execute(
        """
        SELECT market, status, qualification_status, audit_data_status,
               human_review_status, human_review_decision, next_action
        FROM prospects
        WHERE {where_clause}
        """.format(where_clause=" AND ".join(clauses)),
        params,
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
    for market in visible_configured_markets():
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
    append_active_prospect_filter(clauses, params)

    append_visible_market_scope(clauses, params, filters.get("market", ""))
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
        row["lead_category_label"] = lead_category_label(row, metadata)

    if selected_stage:
        rows = [row for row in rows if row["pipeline_stage"] == selected_stage]

    return rows[: filters.get("limit", DEFAULT_LIMIT)]


def load_lead_search_results(
    query: str,
    *,
    market: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []

    normalized_limit = max(1, min(int(limit or 8), 20))
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_active_prospect_filter(clauses, params)
    append_visible_market_scope(clauses, params, market)

    pattern = f"%{normalized_query}%"
    clauses.append(
        """
        (
            CAST(id AS TEXT) LIKE ?
            OR business_name LIKE ?
            OR website_url LIKE ?
            OR phone LIKE ?
            OR market LIKE ?
            OR niche LIKE ?
            OR status LIKE ?
            OR qualification_status LIKE ?
            OR next_action LIKE ?
        )
        """
    )
    params.extend([pattern] * 9)
    params.extend([normalized_query, normalized_query, f"{normalized_query}%", normalized_limit])

    rows = get_connection().execute(
        f"""
        SELECT {", ".join(LEADS_COLUMNS)}
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY
            CASE
                WHEN CAST(id AS TEXT) = ? THEN 0
                WHEN business_name = ? THEN 1
                WHEN business_name LIKE ? THEN 2
                ELSE 3
            END,
            expected_close_score DESC,
            website_pain_score DESC,
            id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    results = [_row_to_dict(row) for row in rows]
    for result in results:
        result["pipeline_stage"] = compute_pipeline_stage(result)
        metadata = parse_json_field(result.get("metadata_json"))
        result["metadata"] = metadata if isinstance(metadata, dict) else {}
        result["lead_category_label"] = lead_category_label(result, result["metadata"])
    return results


def build_lead_search_context(
    endpoint: str,
    *,
    query: str = "",
    market: str = "",
    param_name: str = "lead_q",
    hidden_fields: list[tuple[str, Any]] | None = None,
    reset_args: dict[str, Any] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_query = str(query or "").strip()
    clean_hidden_fields = [
        {"name": str(name), "value": str(value)}
        for name, value in (hidden_fields or [])
        if value not in (None, "")
    ]
    clean_reset_args = {
        key: value
        for key, value in (reset_args or {}).items()
        if value not in (None, "")
    }
    return {
        "action": url_for(endpoint),
        "reset_href": url_for(endpoint, **clean_reset_args),
        "param_name": param_name,
        "query": normalized_query,
        "hidden_fields": clean_hidden_fields,
        "results": (
            results
            if results is not None
            else load_lead_search_results(normalized_query, market=market)
        ),
        "submitted": bool(normalized_query),
    }


def _enrich_trash_row(row: dict[str, Any]) -> dict[str, Any]:
    row["pipeline_stage"] = compute_pipeline_stage(row)
    metadata = _metadata_dict(row.get("metadata_json"))
    if prospect_has_missing_website_signal(row, metadata):
        row["pipeline_stage"] = "NO_WEBSITE"
    trash = metadata.get("trash") if isinstance(metadata.get("trash"), dict) else {}
    if not trash:
        reason = _fallback_trash_reason(row)
        trash = {
            "is_trashed": True,
            "reason": reason,
            "category": _trash_category_for_row(row, {"reason": reason}),
            "trashed_at": row.get("human_reviewed_at") or row.get("updated_at"),
            "media_retention_days": TRASH_MEDIA_RETENTION_DAYS,
        }
        trashed_dt = _parse_utc_datetime(trash.get("trashed_at"))
        trash["media_delete_after"] = (
            (trashed_dt + timedelta(days=TRASH_MEDIA_RETENTION_DAYS)).replace(microsecond=0).isoformat()
            if trashed_dt
            else None
        )
    row["metadata"] = metadata
    row["lead_category_label"] = lead_category_label(row, metadata)
    row["trash"] = trash
    row["trash_reason"] = trash.get("reason") or "rejected_or_discarded"
    stored_category = normalize_trash_category(trash.get("category"))
    row["trash_category"] = (
        stored_category if stored_category != "all" else _trash_category_for_row(row, trash)
    )
    row["trash_detail_lines"] = _trash_detail_lines(row, metadata)
    row["trash_trashed_at"] = trash.get("trashed_at") or row.get("updated_at")
    row["trash_media_delete_after"] = trash.get("media_delete_after")
    row["trash_media_due_label"] = _trash_due_label(trash.get("media_delete_after"))
    row["trash_media_purged_at"] = trash.get("media_purged_at")
    row["trash_can_purge"] = bool(
        _parse_utc_datetime(trash.get("media_delete_after"))
        and not trash.get("media_purged_at")
        and (_parse_utc_datetime(trash.get("media_delete_after")) or datetime.max.replace(tzinfo=timezone.utc))
        <= datetime.now(timezone.utc)
    )
    return row


def load_trash_rows(market: str = "", category: str = "all") -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_trash_prospect_filter(clauses, params)
    append_visible_market_scope(clauses, params, market)
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY updated_at DESC, id DESC
        """,
        params,
    ).fetchall()
    enriched = [_enrich_trash_row(_row_to_dict(row)) for row in rows]
    normalized_category = normalize_trash_category(category)
    if normalized_category != "all":
        enriched = [
            row for row in enriched
            if row.get("trash_category") == normalized_category
        ]
    return enriched


def load_trash_summary(market: str = "") -> dict[str, Any]:
    rows = load_trash_rows(market)
    category_counts = {key: 0 for key, _label in TRASH_CATEGORY_OPTIONS}
    category_counts["all"] = len(rows)
    for row in rows:
        category = row.get("trash_category") or "legacy"
        category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "count": len(rows),
        "media_due": sum(1 for row in rows if row.get("trash_can_purge")),
        "media_purged": sum(1 for row in rows if row.get("trash_media_purged_at")),
        "category_counts": category_counts,
    }


def load_crm_groups(market: str = "", recent_limit: int = 8) -> list[dict[str, Any]]:
    columns = load_crm_columns(market, recent_limit=recent_limit, include_inactive=True)
    columns_by_stage = {column["stage"]: column for column in columns}
    grouped_stages: set[str] = set()
    groups = []
    for key, label, stages in CRM_BOARD_GROUPS:
        stage_columns = []
        for stage in stages:
            grouped_stages.add(stage)
            stage_columns.append(
                columns_by_stage.get(stage)
                or empty_crm_column(
                    stage,
                    CRM_BOARD_STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
                    market=market,
                )
            )
        groups.append(
            {
                "key": key,
                "label": label,
                "count": sum(column["count"] for column in stage_columns),
                "columns": stage_columns,
            }
        )

    other_columns = [
        column
        for column in columns
        if column["stage"] not in grouped_stages and column["count"] > 0
    ]
    if other_columns:
        groups.append(
            {
                "key": "other",
                "label": "Other CRM",
                "count": sum(column["count"] for column in other_columns),
                "columns": other_columns,
            }
        )
    return groups


def empty_crm_column(stage: str, label: str, *, market: str = "") -> dict[str, Any]:
    return {
        "stage": stage,
        "label": label,
        "count": 0,
        "prospects": [],
        "href": url_for("crm_stage", stage=stage, **overview_market_args(market)),
    }


def load_crm_columns(
    market: str = "",
    recent_limit: int = 8,
    *,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    prospects = load_crm_prospects(market, include_inactive=include_inactive)
    columns = []
    stages = list(CRM_STAGES)
    if include_inactive and "REJECTED_REVIEW" not in dict(stages):
        stages.append(("REJECTED_REVIEW", "Rejected Review"))
    for stage, label in stages:
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
                "href": url_for("crm_stage", stage=stage, **overview_market_args(market)),
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
        prospect for prospect in load_crm_prospects(
            market,
            include_inactive=stage in {"DISCARDED", "REJECTED_REVIEW"},
        )
        if prospect["pipeline_stage"] == stage
    ]
    return prospects[:limit]


def load_crm_prospects(market: str = "", *, include_inactive: bool = False) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_visible_market_scope(clauses, params, market)
    if not include_inactive:
        append_active_prospect_filter(clauses, params)
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
    attach_quote_summaries_to_prospects(prospects)
    attach_task_summaries_to_prospects(prospects)
    return prospects


def attach_quote_summaries_to_prospects(prospects: list[dict[str, Any]]) -> None:
    prospect_ids = [int(prospect["id"]) for prospect in prospects if prospect.get("id") is not None]
    if not prospect_ids:
        return
    rows = []
    for start in range(0, len(prospect_ids), 500):
        chunk = prospect_ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            get_connection().execute(
                f"""
                SELECT id, quote_key, prospect_id, status, package_name,
                       one_time_total_cents, recurring_monthly_total_cents,
                       valid_until, updated_at
                FROM quotes
                WHERE prospect_id IN ({placeholders})
                ORDER BY prospect_id, updated_at DESC, id DESC
                """,
                chunk,
            ).fetchall()
        )
    quote_counts: dict[int, int] = {}
    latest_quotes: dict[int, dict[str, Any]] = {}
    for row in rows:
        quote = _row_to_dict(row)
        prospect_id = int(quote["prospect_id"])
        quote_counts[prospect_id] = quote_counts.get(prospect_id, 0) + 1
        latest_quotes.setdefault(prospect_id, quote)

    for prospect in prospects:
        prospect_id = int(prospect["id"])
        latest_quote = latest_quotes.get(prospect_id)
        prospect["quote_count"] = quote_counts.get(prospect_id, 0)
        prospect["latest_quote"] = latest_quote
        if latest_quote:
            prospect["latest_quote_status"] = latest_quote.get("status")
            prospect["latest_quote_amount_cents"] = latest_quote.get("one_time_total_cents")
            prospect["latest_quote_monthly_cents"] = latest_quote.get("recurring_monthly_total_cents")


def attach_task_summaries_to_prospects(prospects: list[dict[str, Any]]) -> None:
    prospects_by_id = {
        int(prospect["id"]): prospect
        for prospect in prospects
        if prospect.get("id") is not None
    }
    for prospect in prospects_by_id.values():
        prospect["open_task_count"] = 0
        prospect["next_task"] = None
        prospect["has_overdue_task"] = False
    if not prospects_by_id:
        return

    prospect_ids = list(prospects_by_id)
    status_values = sorted(task_service.OPEN_STATUSES)
    id_placeholders = ",".join("?" for _ in prospect_ids)
    status_placeholders = ",".join("?" for _ in status_values)
    today = datetime.now().date().isoformat()
    rows = get_connection().execute(
        f"""
        SELECT *
        FROM crm_tasks
        WHERE prospect_id IN ({id_placeholders})
          AND status IN ({status_placeholders})
        ORDER BY
            CASE
                WHEN status IN ('open', 'in_progress') AND due_date IS NOT NULL AND due_date < ? THEN 0
                WHEN status IN ('open', 'in_progress') AND due_date = ? THEN 1
                WHEN status IN ('open', 'in_progress') THEN 2
                WHEN status = 'waiting' THEN 3
                ELSE 4
            END,
            COALESCE(due_at, '9999-12-31T23:59:59'),
            updated_at DESC,
            id DESC
        """,
        [*prospect_ids, *status_values, today, today],
    ).fetchall()
    for row in rows:
        task = enrich_task_row(_row_to_dict(row))
        prospect = prospects_by_id.get(int(task["prospect_id"]))
        if prospect is None:
            continue
        prospect["open_task_count"] += 1
        prospect["has_overdue_task"] = bool(prospect["has_overdue_task"] or task["is_overdue"])
        if prospect["next_task"] is None:
            prospect["next_task"] = task


def list_quotes_for_current_user(limit: int = 200) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(int(limit or 200), 1000))
    clauses = ["1 = 1"]
    params: list[Any] = []
    apply_prospect_scope(clauses, params, "p")
    params.append(normalized_limit)
    rows = get_connection().execute(
        f"""
        SELECT q.id, p.business_name AS prospect_business_name,
               p.market AS prospect_market, p.niche AS prospect_niche
        FROM quotes q
        LEFT JOIN prospects p ON p.id = q.prospect_id
        WHERE {" AND ".join(clauses)}
        ORDER BY q.updated_at DESC, q.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    quotes: list[dict[str, Any]] = []
    connection = get_connection()
    for row in rows:
        quote = quote_service.get_quote(connection, int(row["id"]))
        if quote is None:
            continue
        quote["prospect_business_name"] = row["prospect_business_name"]
        quote["prospect_market"] = row["prospect_market"]
        quote["prospect_niche"] = row["prospect_niche"]
        quotes.append(quote)
    return quotes


def sync_quote_territory_fields(
    connection: sqlite3.Connection,
    quote_id: int,
    prospect: dict[str, Any],
) -> None:
    user = current_dashboard_user()
    owner_username = str(prospect.get("owner_username") or "").strip()
    if not owner_username and user is not None:
        owner_username = user.username
    connection.execute(
        """
        UPDATE quotes
        SET owner_username = ?,
            market_state = ?
        WHERE id = ?
        """,
        (owner_username or None, prospect_state_from_record(prospect), quote_id),
    )


def list_contracts_for_current_user(limit: int = 200) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(int(limit or 200), 1000))
    clauses = ["1 = 1"]
    params: list[Any] = []
    apply_prospect_scope(clauses, params, "p")
    params.append(normalized_limit)
    rows = get_connection().execute(
        f"""
        SELECT c.id, p.business_name AS prospect_business_name,
               p.market AS prospect_market, p.niche AS prospect_niche,
               q.quote_key AS quote_key, q.status AS quote_status
        FROM contracts c
        JOIN prospects p ON p.id = c.prospect_id
        LEFT JOIN quotes q ON q.id = c.quote_id
        WHERE {" AND ".join(clauses)}
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    contracts: list[dict[str, Any]] = []
    connection = get_connection()
    for row in rows:
        contract = contract_service.load_contract(connection, int(row["id"]))
        if contract is None:
            continue
        contract["prospect_business_name"] = row["prospect_business_name"]
        contract["prospect_market"] = row["prospect_market"]
        contract["prospect_niche"] = row["prospect_niche"]
        contract["quote_key"] = row["quote_key"]
        contract["quote_status"] = row["quote_status"]
        contract["generated_files"] = contract_generated_file_info(contract)
        contracts.append(contract)
    return contracts


CONTRACT_ACTIVE_STATUSES = {"draft", "generated", "sent", "delivered"}
CONTRACT_SIGNED_STATUSES = {"completed"}
CONTRACT_FILTER_KEYS = ("active", "signed", "other")


def normalize_contract_list_filter(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in CONTRACT_FILTER_KEYS else "active"


def contract_status_filter_bucket(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in CONTRACT_ACTIVE_STATUSES:
        return "active"
    if normalized in CONTRACT_SIGNED_STATUSES:
        return "signed"
    return "other"


def contract_filter_options(
    contracts: list[dict[str, Any]],
    selected_filter: str,
) -> list[dict[str, Any]]:
    counts = {key: 0 for key in CONTRACT_FILTER_KEYS}
    for contract in contracts:
        bucket = contract_status_filter_bucket(contract.get("status"))
        counts[bucket] = counts.get(bucket, 0) + 1
    return [
        {
            "key": key,
            "label": label,
            "count": counts.get(key, 0),
            "active": key == selected_filter,
            "href": url_for("contracts_list", filter=key),
        }
        for key, label in (
            ("active", "Active"),
            ("signed", "Signed"),
            ("other", "Other"),
        )
    ]


def sync_contract_territory_fields(
    connection: sqlite3.Connection,
    contract_id: int,
    prospect: dict[str, Any],
) -> None:
    user = current_dashboard_user()
    owner_username = str(prospect.get("owner_username") or "").strip()
    if not owner_username and user is not None:
        owner_username = user.username
    connection.execute(
        """
        UPDATE contracts
        SET owner_username = ?,
            market_state = ?
        WHERE id = ?
        """,
        (owner_username or None, prospect_state_from_record(prospect), contract_id),
    )


def contract_generated_file_info(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = {}
    for key, label in (
        ("generated_docx_path", "DOCX"),
        ("generated_html_path", "HTML"),
        ("generated_pdf_path", "PDF"),
    ):
        path = str(contract.get(key) or "").strip()
        resolved = resolve_media_path(path) if path else None
        files[key] = {
            "label": label,
            "path": path,
            "exists": bool(resolved and resolved.exists() and resolved.is_file()),
        }
    return files


DOCUSIGN_FINAL_ENVELOPE_STATUSES = {"completed", "declined", "voided", "deleted"}
DOCUSIGN_REFRESH_STATUS_EVENTS = {
    "sent": ("sent", "contract_sent"),
    "delivered": ("delivered", "contract_delivered"),
    "completed": ("completed", "contract_completed"),
    "declined": ("declined", "contract_declined"),
    "voided": ("voided", "contract_voided"),
}
DOCUSIGN_WEBHOOK_SECRET_HEADER_NAMES = (
    "X-DocuSign-Webhook-Secret",
    "X-DocuSign-Connect-Secret",
    "X-Webhook-Secret",
)
DOCUSIGN_WEBHOOK_EVENT_PREFIX = "envelope-"
DOCUSIGN_WEBHOOK_STATUS_VALUES = {
    "created",
    "sent",
    "delivered",
    "completed",
    "declined",
    "voided",
}


def docusign_config_status() -> dict[str, Any]:
    try:
        config = docusign_client.load_docusign_config()
        errors = docusign_client.validate_docusign_config(
            config,
            check_private_key_file=True,
        )
    except Exception:
        return {
            "configured": False,
            "environment": "unknown",
            "environment_label": "Unknown",
            "is_production": False,
            "base_path": "",
            "auth_server": "",
            "private_key_configured": False,
            "private_key_exists": False,
            "errors": ["DocuSign configuration could not be loaded."],
        }
    environment = str(config.environment or "demo").strip().lower()
    return {
        "configured": not errors,
        "environment": environment,
        "environment_label": "Production" if environment == "production" else "Demo",
        "is_production": environment == "production",
        "base_path": config.base_path,
        "auth_server": config.auth_server,
        "private_key_configured": bool(config.rsa_private_key_pem or config.rsa_private_key_path),
        "private_key_exists": bool(config.rsa_private_key_pem) or bool(config.rsa_private_key_path.exists()),
        "errors": errors,
    }


def docusign_webhook_enabled() -> bool:
    value = str(os.environ.get("DOCUSIGN_WEBHOOK_ENABLED") or "false").strip().lower()
    return value in {"1", "true", "yes", "y", "on", "enabled"}


def docusign_webhook_secret() -> str:
    return str(os.environ.get("DOCUSIGN_WEBHOOK_SECRET") or "").strip()


def docusign_webhook_secret_configured() -> bool:
    return bool(docusign_webhook_secret())


def validate_docusign_webhook_request() -> bool:
    configured_secret = docusign_webhook_secret()
    if not configured_secret:
        return False

    candidates = []
    for header_name in DOCUSIGN_WEBHOOK_SECRET_HEADER_NAMES:
        value = str(request.headers.get(header_name) or "").strip()
        if value:
            candidates.append(value)
    authorization = str(request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:].strip())

    return any(secrets.compare_digest(configured_secret, candidate) for candidate in candidates)


def parse_docusign_webhook_payload(webhook_request: Any) -> dict[str, str]:
    payload: Any = None
    if webhook_request.is_json:
        payload = webhook_request.get_json(silent=True)
    raw_body = webhook_request.get_data(cache=True, as_text=False) or b""
    text_body = raw_body.decode("utf-8", errors="ignore").strip()

    if payload is None and text_body:
        if text_body.startswith("{") or text_body.startswith("["):
            try:
                payload = json.loads(text_body)
            except json.JSONDecodeError:
                payload = None
        elif text_body.startswith("<"):
            return parse_docusign_webhook_xml(text_body)

    if isinstance(payload, (dict, list)):
        event_name = extract_json_value(payload, {"event", "eventname"}) or ""
        status = extract_json_value(payload, {"status", "envelopestatus"}) or status_from_docusign_event(event_name)
        return {
            "envelope_id": extract_json_value(payload, {"envelopeid"}) or "",
            "status": normalize_docusign_status(status),
            "event": event_name,
        }
    return {"envelope_id": "", "status": "", "event": ""}


def parse_docusign_webhook_xml(text_body: str) -> dict[str, str]:
    try:
        root = ET.fromstring(text_body)
    except ET.ParseError:
        return {"envelope_id": "", "status": "", "event": ""}
    event_name = extract_xml_text(root, {"event", "eventname"}) or ""
    status = extract_xml_text(root, {"status", "envelopestatus"}) or status_from_docusign_event(event_name)
    return {
        "envelope_id": extract_xml_text(root, {"envelopeid"}) or "",
        "status": normalize_docusign_status(status),
        "event": event_name,
    }


def extract_json_value(payload: Any, normalized_keys: set[str]) -> str:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if normalize_payload_key(key) in normalized_keys and value not in (None, ""):
                if isinstance(value, (str, int, float)):
                    return str(value).strip()
        for value in payload.values():
            found = extract_json_value(value, normalized_keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = extract_json_value(value, normalized_keys)
            if found:
                return found
    return ""


def extract_xml_text(root: ET.Element, normalized_tags: set[str]) -> str:
    for element in root.iter():
        if normalize_payload_key(xml_local_name(element.tag)) not in normalized_tags:
            continue
        text = str(element.text or "").strip()
        if text:
            return text
    return ""


def status_from_docusign_event(event_name: str) -> str:
    event = str(event_name or "").strip().lower()
    if not event.startswith(DOCUSIGN_WEBHOOK_EVENT_PREFIX):
        return ""
    candidate = event.removeprefix(DOCUSIGN_WEBHOOK_EVENT_PREFIX).strip()
    return candidate if candidate in DOCUSIGN_WEBHOOK_STATUS_VALUES else ""


def normalize_payload_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def xml_local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def find_contract_by_docusign_envelope_id(
    connection: sqlite3.Connection,
    envelope_id: str,
) -> dict[str, Any] | None:
    rows = contract_service.list_contracts(
        connection,
        {"docusign_envelope_id": envelope_id, "limit": 1},
    )
    if not rows:
        return None
    return contract_service.load_contract(connection, int(rows[0]["id"]))


def contract_docusign_view(
    contract: dict[str, Any],
    render_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = docusign_config_status()
    generated_files = contract.get("generated_files")
    if not isinstance(generated_files, dict):
        generated_files = contract_generated_file_info(contract)
    docx_file = generated_files.get("generated_docx_path", {})
    preflight = build_contract_docusign_preflight(
        contract,
        config_status=config,
        allow_active_envelope=False,
        render_context=render_context,
    )
    return {
        "config": config,
        "masked_envelope_id": mask_docusign_envelope_id(contract.get("docusign_envelope_id")),
        "has_envelope": bool(str(contract.get("docusign_envelope_id") or "").strip()),
        "active_envelope": contract_has_active_docusign_envelope(contract),
        "status": str(contract.get("docusign_status") or "").strip(),
        "status_updated_at": contract.get("docusign_status_updated_at"),
        "docx_exists": bool(docx_file.get("exists")),
        "required_signers": contract_required_docusign_signers(contract),
        "preflight": preflight,
        "preflight_errors": preflight["errors"],
        "preflight_warnings": preflight["warnings"],
        "preflight_items": preflight["items"],
        "ready_to_send": preflight["ready"],
        "send_disabled": not config["configured"],
    }


def build_contract_docusign_preflight(
    contract: dict[str, Any],
    *,
    config_status: dict[str, Any] | None = None,
    allow_active_envelope: bool,
    render_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return DocuSign readiness details without calling DocuSign."""

    errors: list[str] = []
    warnings: list[str] = []
    config = config_status or docusign_config_status()
    docx_path = resolve_contract_generated_docx_path(contract)
    docx_path_value = str(contract.get("generated_docx_path") or "").strip()
    active_envelope = contract_has_active_docusign_envelope(contract)
    status = normalize_docusign_status(contract.get("status")) or "draft"
    primary_signer = contract_primary_signer_record(contract)
    business_context = docusign_context_section(render_context, "business")
    quote_context = docusign_context_section(render_context, "quote")
    contract_context = docusign_context_section(render_context, "contract")

    if not config.get("configured"):
        config_errors = "; ".join(config.get("errors") or [])
        errors.append(
            "DocuSign config is incomplete."
            + (f" {config_errors}" if config_errors else "")
        )

    if status in {"completed", "declined", "voided", "superseded"}:
        errors.append("Create a revision before sending this contract status to DocuSign.")
    elif active_envelope and not allow_active_envelope:
        errors.append(
            "This contract already has an active DocuSign envelope. Check resend/supersede to create another envelope."
        )
    elif status not in {"generated", "draft", "sent", "delivered", "error"}:
        errors.append("Generate the contract before sending it to DocuSign.")
    elif status == "draft" and docx_path is None:
        errors.append("Draft contracts need an existing generated DOCX before DocuSign send.")

    if not docx_path_value:
        errors.append("Generated DOCX path is required before DocuSign send.")
    elif docx_path is None:
        errors.append("Generated DOCX file does not exist at the stored path.")

    if not docusign_first_value(contract.get("legal_business_name"), business_context.get("legal_name")):
        errors.append("Legal business name must be confirmed before DocuSign send.")
    if not docusign_first_value(contract.get("business_entity_type"), business_context.get("entity_type")):
        errors.append("Business entity type must be confirmed before DocuSign send.")
    if not docusign_first_value(primary_signer.get("name"), contract.get("signer_name")):
        errors.append("Primary signer name must be confirmed before DocuSign send.")
    if not docusign_first_value(primary_signer.get("title"), contract.get("signer_title")):
        errors.append("Primary signer authority/title must be confirmed before DocuSign send.")
    if not docusign_first_value(primary_signer.get("email"), contract.get("signer_email")):
        errors.append("Primary signer email must be confirmed before DocuSign send.")
    if not docusign_first_value(contract.get("effective_date"), contract_context.get("effective_date")):
        errors.append("Contract effective date must be confirmed before DocuSign send.")
    if not docusign_first_value(quote_context.get("package_name")):
        errors.append("Quote package name is required before DocuSign send.")
    if not contract_has_billable_amount(contract, quote_context):
        errors.append("A one-time total or recurring monthly amount is required before DocuSign send.")

    signer_errors, signer_warnings = contract_docusign_signer_preflight_messages(contract)
    errors.extend(signer_errors)
    warnings.extend(signer_warnings)

    anchor_check = contract_docusign_anchor_check(docx_path)
    if anchor_check["missing"]:
        errors.append("Missing DocuSign anchors: " + ", ".join(anchor_check["missing"]))
    if anchor_check["error"]:
        errors.append(anchor_check["error"])

    errors = dedupe_strings(errors)
    warnings = dedupe_strings(warnings)
    ready = not errors
    return {
        "ready": ready,
        "errors": errors,
        "warnings": warnings,
        "items": contract_docusign_preflight_items(
            ready=ready,
            config=config,
            contract=contract,
            docx_path=docx_path,
            primary_signer=primary_signer,
            anchor_check=anchor_check,
            optional_signer_warnings=signer_warnings,
        ),
        "anchors": anchor_check,
    }


def validate_contract_docusign_send_preconditions(
    contract: dict[str, Any],
    *,
    config_status: dict[str, Any] | None = None,
    allow_active_envelope: bool,
    render_context: dict[str, Any] | None = None,
) -> list[str]:
    return build_contract_docusign_preflight(
        contract,
        config_status=config_status,
        allow_active_envelope=allow_active_envelope,
        render_context=render_context,
    )["errors"]


def contract_required_docusign_signers(contract: dict[str, Any]) -> list[dict[str, Any]]:
    signers = []
    for index, signer in enumerate(contract_signer_records(contract), start=1):
        if not contract_signer_is_required(signer, index):
            continue
        name = str(signer.get("name") or "").strip()
        title = str(signer.get("title") or "").strip()
        email = str(signer.get("email") or "").strip()
        if not name or not title or not email:
            continue
        signers.append(
            {
                "role": "client",
                "name": name,
                "title": title,
                "email": email,
                "phone": str(signer.get("phone") or "").strip(),
                "required": True,
                "_anchor_index": index,
                "routing_order": parse_safe_int(signer.get("routing_order"), default=index),
            }
        )
    return signers[:3]


def contract_required_docusign_signer_errors(contract: dict[str, Any]) -> list[str]:
    errors, _warnings = contract_docusign_signer_preflight_messages(contract)
    return errors


def contract_docusign_signer_preflight_messages(contract: dict[str, Any]) -> tuple[list[str], list[str]]:
    records = contract_signer_records(contract)
    errors: list[str] = []
    warnings: list[str] = []
    required_count = 0
    complete_count = 0
    for index, signer in enumerate(records, start=1):
        name = str(signer.get("name") or "").strip()
        title = str(signer.get("title") or "").strip()
        email = str(signer.get("email") or "").strip()
        required = contract_signer_is_required(signer, index)
        if not required:
            if index > 1 and any((name, title, email)) and not all((name, title, email)):
                warnings.append(
                    f"Optional signer {index} is incomplete; complete name, title, and email or leave it optional/incomplete."
                )
            continue
        required_count += 1
        missing = []
        if not name:
            missing.append("name")
        if not title:
            missing.append("title")
        if not email:
            missing.append("email")
        if not missing:
            complete_count += 1
        else:
            errors.append(
                f"Signer {index} is marked required but is missing " + ", ".join(missing) + "."
            )
    if required_count == 0:
        errors.append("At least one signer must be marked required before DocuSign send.")
    elif complete_count == 0:
        errors.append("At least one required signer needs name, title, and email before DocuSign send.")
    return errors, warnings


def contract_primary_signer_record(contract: dict[str, Any]) -> dict[str, Any]:
    records = contract_signer_records(contract)
    if records:
        return records[0]
    return {
        "name": str(contract.get("signer_name") or "").strip(),
        "title": str(contract.get("signer_title") or "").strip(),
        "email": str(contract.get("signer_email") or "").strip(),
        "phone": str(contract.get("signer_phone") or "").strip(),
        "required": True,
        "routing_order": 1,
    }


def contract_signer_is_required(signer: dict[str, Any], index: int) -> bool:
    return index == 1 or truthy_checkbox(signer.get("required"), default=index == 1)


def docusign_context_section(
    render_context: dict[str, Any] | None,
    section: str,
) -> dict[str, Any]:
    if not isinstance(render_context, dict):
        return {}
    value = render_context.get(section)
    return dict(value) if isinstance(value, dict) else {}


def docusign_first_value(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def contract_has_billable_amount(
    contract: dict[str, Any],
    quote_context: dict[str, Any],
) -> bool:
    if parse_safe_int(contract.get("one_time_total_cents"), default=0) > 0:
        return True
    if parse_safe_int(contract.get("recurring_monthly_total_cents"), default=0) > 0:
        return True
    if parse_safe_int(quote_context.get("one_time_total_cents"), default=0) > 0:
        return True
    if parse_safe_int(quote_context.get("recurring_monthly_total_cents"), default=0) > 0:
        return True
    return False


def contract_docusign_preflight_items(
    *,
    ready: bool,
    config: dict[str, Any],
    contract: dict[str, Any],
    docx_path: Path | None,
    primary_signer: dict[str, Any],
    anchor_check: dict[str, Any],
    optional_signer_warnings: list[str],
) -> list[dict[str, str]]:
    return [
        {
            "label": "Ready to send",
            "status": "ok" if ready else "error",
            "detail": "All required preflight checks passed." if ready else "Resolve the required items before sending.",
        },
        {
            "label": "Missing legal business name",
            "status": "ok" if str(contract.get("legal_business_name") or "").strip() else "error",
            "detail": "Confirmed." if str(contract.get("legal_business_name") or "").strip() else "Add the client's legal business name.",
        },
        {
            "label": "Missing signer title",
            "status": "ok" if str(primary_signer.get("title") or contract.get("signer_title") or "").strip() else "error",
            "detail": "Confirmed." if str(primary_signer.get("title") or contract.get("signer_title") or "").strip() else "Add the primary signer's title or authority.",
        },
        {
            "label": "Missing generated DOCX",
            "status": "ok" if docx_path is not None else "error",
            "detail": str(docx_path) if docx_path is not None else "Generate the contract before sending.",
        },
        {
            "label": "Missing DocuSign config",
            "status": "ok" if config.get("configured") else "error",
            "detail": "Configured." if config.get("configured") else "Complete the DocuSign environment settings.",
        },
        {
            "label": "Missing anchors",
            "status": "ok" if not anchor_check.get("missing") and not anchor_check.get("error") else "error",
            "detail": (
                f"All anchors present in {anchor_check.get('source_label')}."
                if not anchor_check.get("missing") and not anchor_check.get("error")
                else anchor_check.get("error") or ", ".join(anchor_check.get("missing") or [])
            ),
        },
        {
            "label": "Optional signers incomplete",
            "status": "warn" if optional_signer_warnings else "ok",
            "detail": " ".join(optional_signer_warnings) if optional_signer_warnings else "No optional signer blocker.",
        },
    ]


def contract_docusign_anchor_check(generated_docx_path: Path | None) -> dict[str, Any]:
    source_path = generated_docx_path or contract_docusign_template_path()
    required = required_docusign_anchor_strings()
    if source_path is None:
        return {
            "source_path": "",
            "source_label": "missing template",
            "required": required,
            "missing": required,
            "error": "No generated DOCX or contract template is available for anchor validation.",
        }
    text, error = read_docx_text_for_anchor_check(source_path)
    missing = [anchor for anchor in required if anchor not in text]
    return {
        "source_path": str(source_path),
        "source_label": "generated DOCX" if generated_docx_path else "contract template",
        "required": required,
        "missing": missing,
        "error": error,
    }


def contract_docusign_template_path() -> Path | None:
    for path in (contract_exports.PRIMARY_DOCX_TEMPLATE, contract_exports.FALLBACK_DOCX_TEMPLATE):
        if path.exists() and path.is_file():
            return path
    return None


def required_docusign_anchor_strings() -> list[str]:
    anchors = [
        docusign_client.PROVIDER_ANCHORS["sign"],
        docusign_client.PROVIDER_ANCHORS["date"],
    ]
    for index in sorted(docusign_client.CLIENT_ANCHORS):
        anchors.append(docusign_client.CLIENT_ANCHORS[index]["sign"])
        anchors.append(docusign_client.CLIENT_ANCHORS[index]["date"])
    return anchors


def read_docx_text_for_anchor_check(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            parts = []
            for name in archive.namelist():
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue
                if name != "word/document.xml" and not re.match(r"word/(header|footer)\d*\.xml$", name):
                    continue
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                parts.append("".join(root.itertext()))
    except (OSError, zipfile.BadZipFile) as exc:
        return "", f"Could not inspect DOCX anchors in {path}: {exc}"
    return "\n".join(parts), ""


def contract_signer_records(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw_signers = contract.get("signers")
    records: list[dict[str, Any]] = []
    if isinstance(raw_signers, list):
        for signer in raw_signers[:3]:
            if isinstance(signer, dict):
                records.append(dict(signer))
    if not records:
        records.append(
            {
                "role": "client",
                "name": str(contract.get("signer_name") or "").strip(),
                "title": str(contract.get("signer_title") or "").strip(),
                "email": str(contract.get("signer_email") or "").strip(),
                "phone": str(contract.get("signer_phone") or "").strip(),
                "required": True,
                "routing_order": 1,
            }
        )
    return records[:3]


def resolve_contract_generated_docx_path(contract: dict[str, Any]) -> Path | None:
    path = str(contract.get("generated_docx_path") or "").strip()
    resolved = resolve_media_path(path) if path else None
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return resolved


def contract_has_active_docusign_envelope(contract: dict[str, Any]) -> bool:
    envelope_id = str(contract.get("docusign_envelope_id") or "").strip()
    if not envelope_id:
        return False
    status = normalize_docusign_status(contract.get("docusign_status"))
    return not status or status not in DOCUSIGN_FINAL_ENVELOPE_STATUSES


def mask_docusign_envelope_id(envelope_id: Any) -> str:
    clean = str(envelope_id or "").strip()
    if not clean:
        return ""
    if len(clean) <= 12:
        return clean[:4] + "..." if len(clean) > 4 else clean
    return f"{clean[:8]}...{clean[-4:]}"


def normalize_docusign_status(value: Any) -> str:
    return str(value or "").strip().lower()


def truthy_checkbox(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "required"}


def parse_safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            output.append(clean)
    return output


def contract_docusign_error_message(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            docusign_client.DocusignConfigurationError,
            docusign_client.DocusignDependencyError,
            ValueError,
            FileNotFoundError,
        ),
    ):
        return str(exc)
    return "DocuSign request failed. Check configuration, JWT consent, and the configured DocuSign environment."


def contract_form_state(
    prospect: dict[str, Any],
    quote: dict[str, Any] | None = None,
    *,
    contract: dict[str, Any] | None = None,
    form: Any | None = None,
) -> dict[str, Any]:
    contact_name = str((quote or {}).get("client_contact_name") or "").strip()
    contact_email = str((quote or {}).get("client_email") or "").strip()
    contact_phone = str((quote or {}).get("client_phone") or "").strip()
    business_name = str(
        (contract or {}).get("client_business_name")
        or (quote or {}).get("client_business_name")
        or prospect.get("business_name")
        or ""
    ).strip()
    state = {
        "title": str(
            (contract or {}).get("title")
            or f"Service Agreement for {business_name or 'Client'}"
        ).strip(),
        "client_business_name": business_name,
        "client_contact_name": str((contract or {}).get("client_contact_name") or contact_name).strip(),
        "client_email": str((contract or {}).get("client_email") or contact_email).strip(),
        "client_phone": str((contract or {}).get("client_phone") or contact_phone).strip(),
        "website_url": str(
            (contract or {}).get("website_url")
            or (quote or {}).get("website_url")
            or prospect.get("website_url")
            or ""
        ).strip(),
        "legal_business_name": str((contract or {}).get("legal_business_name") or "").strip(),
        "business_entity_type": str((contract or {}).get("business_entity_type") or "").strip(),
        "billing_address": str(
            (contract or {}).get("billing_address")
            or prospect.get("formatted_address")
            or prospect.get("address")
            or ""
        ).strip(),
        "signer_name": str((contract or {}).get("signer_name") or contact_name).strip(),
        "signer_title": str((contract or {}).get("signer_title") or "").strip(),
        "signer_email": str((contract or {}).get("signer_email") or contact_email).strip(),
        "signer_phone": str((contract or {}).get("signer_phone") or contact_phone).strip(),
        "effective_date": str((contract or {}).get("effective_date") or "").strip(),
        "start_date": str((contract or {}).get("start_date") or "").strip(),
        "term_months": str(
            (contract or {}).get("term_months")
            if (contract or {}).get("term_months") not in (None, "")
            else (quote or {}).get("term_months") or 0
        ),
        "quote_summary": {
            "package_name": (quote or {}).get("package_name") or "Custom",
            "one_time_total_cents": (contract or {}).get("one_time_total_cents")
            if (contract or {}).get("one_time_total_cents") is not None
            else (quote or {}).get("one_time_total_cents"),
            "recurring_monthly_total_cents": (contract or {}).get("recurring_monthly_total_cents")
            if (contract or {}).get("recurring_monthly_total_cents") is not None
            else (quote or {}).get("recurring_monthly_total_cents"),
            "deposit_due_cents": (contract or {}).get("deposit_due_cents")
            if (contract or {}).get("deposit_due_cents") is not None
            else (quote or {}).get("deposit_due_cents"),
            "balance_due_cents": (contract or {}).get("balance_due_cents")
            if (contract or {}).get("balance_due_cents") is not None
            else (quote or {}).get("balance_due_cents"),
        },
        "signers": contract_signer_form_state(contract),
        "sections": contract_section_form_state(contract),
    }
    if contract is None and state["signers"]:
        state["signers"][0].update(
            {
                "name": state["signer_name"],
                "title": state["signer_title"],
                "email": state["signer_email"],
                "phone": state["signer_phone"],
                "required": True,
                "routing_order": 1,
            }
        )
    if form is not None:
        for key in (
            "title",
            "client_business_name",
            "client_contact_name",
            "client_email",
            "client_phone",
            "website_url",
            "legal_business_name",
            "business_entity_type",
            "billing_address",
            "signer_name",
            "signer_title",
            "signer_email",
            "signer_phone",
            "effective_date",
            "start_date",
            "term_months",
        ):
            state[key] = str(form.get(key) or "").strip()
        state["signers"] = contract_signers_from_form_state(form)
        state["sections"] = contract_sections_from_form_state(form)
    return state


def contract_signer_form_state(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    signers = []
    if contract and isinstance(contract.get("signers"), list):
        signers = [dict(signer) for signer in contract["signers"][:3] if isinstance(signer, dict)]
    if not signers and contract:
        signers = [
            {
                "name": contract.get("signer_name") or "",
                "title": contract.get("signer_title") or "",
                "email": contract.get("signer_email") or "",
                "phone": contract.get("signer_phone") or "",
                "required": True,
                "routing_order": 1,
            }
        ]
    while len(signers) < 3:
        signers.append(
            {
                "name": "",
                "title": "",
                "email": "",
                "phone": "",
                "required": len(signers) == 0,
                "routing_order": len(signers) + 1,
            }
        )
    return signers[:3]


def contract_section_form_state(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    sections = []
    if contract and isinstance(contract.get("sections"), list):
        for section in contract["sections"][:5]:
            if not isinstance(section, dict):
                continue
            metadata = section.get("metadata") if isinstance(section.get("metadata"), dict) else {}
            sections.append(
                {
                    "title": str(section.get("title") or "").strip(),
                    "body": str(section.get("body") or "").strip(),
                    "client_visible": metadata.get("client_visible") is not False,
                    "requires_signature": bool(section.get("requires_signature")),
                    "signer_index": str(section.get("signer_index") or ""),
                }
            )
    while len(sections) < 5:
        sections.append(
            {
                "title": "",
                "body": "",
                "client_visible": True,
                "requires_signature": False,
                "signer_index": "",
            }
        )
    return sections[:5]


def contract_signers_from_form_state(form: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": str(form.get(f"signer_name_{index}") or "").strip(),
            "title": str(form.get(f"signer_title_{index}") or "").strip(),
            "email": str(form.get(f"signer_email_{index}") or "").strip(),
            "phone": str(form.get(f"signer_phone_{index}") or "").strip(),
            "required": form.get(f"signer_required_{index}") == "1",
            "routing_order": str(form.get(f"signer_routing_order_{index}") or index).strip(),
        }
        for index in range(1, 4)
    ]


def contract_sections_from_form_state(form: Any) -> list[dict[str, Any]]:
    return [
        {
            "title": str(form.get(f"section_title_{index}") or "").strip(),
            "body": str(form.get(f"section_body_{index}") or "").strip(),
            "client_visible": form.get(f"section_client_visible_{index}") == "1",
            "requires_signature": form.get(f"section_requires_signature_{index}") == "1",
            "signer_index": str(form.get(f"section_signer_index_{index}") or "").strip(),
        }
        for index in range(1, 6)
    ]


def parse_contract_builder_form(form: Any) -> dict[str, Any]:
    header = {
        "title": str(form.get("title") or "").strip(),
        "client_business_name": str(form.get("client_business_name") or "").strip(),
        "client_contact_name": str(form.get("client_contact_name") or "").strip(),
        "client_email": str(form.get("client_email") or "").strip(),
        "client_phone": str(form.get("client_phone") or "").strip(),
        "website_url": str(form.get("website_url") or "").strip(),
        "legal_business_name": str(form.get("legal_business_name") or "").strip(),
        "business_entity_type": str(form.get("business_entity_type") or "").strip(),
        "billing_address": str(form.get("billing_address") or "").strip(),
        "signer_name": str(form.get("signer_name_1") or form.get("signer_name") or "").strip(),
        "signer_title": str(form.get("signer_title_1") or form.get("signer_title") or "").strip(),
        "signer_email": str(form.get("signer_email_1") or form.get("signer_email") or "").strip(),
        "signer_phone": str(form.get("signer_phone_1") or form.get("signer_phone") or "").strip(),
        "effective_date": str(form.get("effective_date") or "").strip(),
        "start_date": str(form.get("start_date") or "").strip(),
        "term_months": parse_nonnegative_int(form.get("term_months") or "0", "Term months"),
    }
    required_fields = {
        "Legal business name": header["legal_business_name"],
        "Business entity type": header["business_entity_type"],
        "Billing address": header["billing_address"],
        "Primary signer name": header["signer_name"],
        "Primary signer title": header["signer_title"],
        "Primary signer email": header["signer_email"],
        "Effective date": header["effective_date"],
        "Start date": header["start_date"],
    }
    missing = [label for label, value in required_fields.items() if not value]
    if missing:
        raise ValueError("Confirm required contract fields: " + ", ".join(missing) + ".")

    signers: list[dict[str, Any]] = []
    for index in range(1, 4):
        name = str(form.get(f"signer_name_{index}") or "").strip()
        title = str(form.get(f"signer_title_{index}") or "").strip()
        email = str(form.get(f"signer_email_{index}") or "").strip()
        phone = str(form.get(f"signer_phone_{index}") or "").strip()
        has_values = any((name, title, email, phone))
        if not has_values and index > 1:
            continue
        if index == 1 and not has_values:
            name = header["signer_name"]
            title = header["signer_title"]
            email = header["signer_email"]
            phone = header["signer_phone"]
        if has_values and (not name or not email):
            raise ValueError(f"Signer {index} needs at least a name and email.")
        signers.append(
            {
                "role": "client",
                "name": name,
                "title": title,
                "email": email,
                "phone": phone,
                "required": form.get(f"signer_required_{index}") == "1" or index == 1,
                "routing_order": parse_optional_contract_int(
                    form.get(f"signer_routing_order_{index}"),
                    default=index,
                ),
            }
        )

    sections: list[dict[str, Any]] = []
    for index in range(1, 6):
        title = str(form.get(f"section_title_{index}") or "").strip()
        body = str(form.get(f"section_body_{index}") or "").strip()
        if not title and not body:
            continue
        if not title or not body:
            raise ValueError(f"Additional section {index} needs both a title and body.")
        sections.append(
            {
                "section_key": f"custom_section_{index}",
                "title": title,
                "body": body,
                "requires_signature": form.get(f"section_requires_signature_{index}") == "1",
                "signer_index": parse_optional_contract_int(
                    form.get(f"section_signer_index_{index}"),
                    default=0,
                ),
                "metadata": {
                    "client_visible": form.get(f"section_client_visible_{index}") == "1",
                },
            }
        )
    return {"header": header, "signers": signers, "sections": sections}


def parse_optional_contract_int(value: Any, *, default: int = 0) -> int:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError("Expected a numeric value.") from exc
    return max(0, parsed)


def contract_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    if code == "created":
        return {"status": "success", "message": "Contract created."}
    if code == "saved":
        return {"status": "success", "message": "Contract saved."}
    if code == "generated":
        return {"status": "success", "message": "Contract artifacts generated locally."}
    if code == "docusign_sent":
        return {"status": "success", "message": "DocuSign envelope sent."}
    if code == "docusign_draft_created":
        return {"status": "success", "message": "DocuSign draft envelope created."}
    if code == "docusign_refreshed":
        return {"status": "success", "message": "DocuSign envelope status refreshed."}
    if code == "voided":
        return {"status": "success", "message": "Contract voided."}
    if code == "revision_created":
        return {"status": "success", "message": "Contract revision created."}
    if code.startswith("error:"):
        return {"status": "error", "message": code.split(":", 1)[1]}
    return {"status": "success", "message": str(code)}


def contract_file_response(contract: dict[str, Any], path_key: str, download_name: str):
    path = str(contract.get(path_key) or "").strip()
    resolved = resolve_media_path(path) if path else None
    if resolved is None or not resolved.exists() or not resolved.is_file():
        abort(404)
    return send_file(resolved, as_attachment=True, download_name=download_name)


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


def task_filters_from_request(source: Any | None = None) -> dict[str, str]:
    data = source or request.args
    status = str(data.get("status") or "").strip().lower()
    raw_task_type = str(data.get("task_type") or "").strip()
    priority = str(data.get("priority") or "").strip().lower()
    due_bucket = str(data.get("due_bucket") or "").strip().lower()
    try:
        task_type = task_service.normalize_task_type(raw_task_type) if raw_task_type else ""
    except ValueError:
        task_type = ""
    return {
        "status": status if status in task_service.TASK_STATUS_LABELS else "",
        "task_type": task_type,
        "priority": priority if priority in task_service.TASK_PRIORITY_LABELS else "",
        "market": str(data.get("market") or "").strip(),
        "assigned_to": str(data.get("assigned_to") or "").strip(),
        "due_bucket": due_bucket if due_bucket in {"overdue", "today", "upcoming", "waiting", "completed"} else "",
        "q": str(data.get("q") or "").strip(),
    }


def task_due_bucket_options() -> list[dict[str, str]]:
    return [
        {"value": "overdue", "label": "Overdue"},
        {"value": "today", "label": "Today"},
        {"value": "upcoming", "label": "Upcoming"},
        {"value": "waiting", "label": "Waiting"},
        {"value": "completed", "label": "Completed"},
    ]


def load_global_tasks(filters: dict[str, str]) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    append_visible_market_scope(clauses, params, filters.get("market", ""))
    if filters.get("status"):
        clauses.append("t.status = ?")
        params.append(filters["status"])
    if filters.get("task_type"):
        clauses.append("t.task_type = ?")
        params.append(filters["task_type"])
    if filters.get("priority"):
        clauses.append("t.priority = ?")
        params.append(filters["priority"])
    if filters.get("assigned_to"):
        clauses.append("LOWER(COALESCE(t.assigned_to, '')) = ?")
        params.append(filters["assigned_to"].lower())
    append_task_due_bucket_filter(clauses, params, filters.get("due_bucket", ""))
    q = filters.get("q", "").strip().lower()
    if q:
        like = f"%{q}%"
        clauses.append(
            "("
            "LOWER(COALESCE(t.title, '')) LIKE ? OR "
            "LOWER(COALESCE(t.notes, '')) LIKE ? OR "
            "LOWER(COALESCE(t.contact_name, '')) LIKE ? OR "
            "LOWER(COALESCE(t.contact_email, '')) LIKE ? OR "
            "LOWER(COALESCE(t.contact_phone, '')) LIKE ? OR "
            "LOWER(COALESCE(prospects.business_name, '')) LIKE ? OR "
            "LOWER(COALESCE(prospects.market, '')) LIKE ? OR "
            "LOWER(COALESCE(prospects.niche, '')) LIKE ?"
            ")"
        )
        params.extend([like] * 8)
    return query_task_rows(clauses, params)


def append_task_due_bucket_filter(
    clauses: list[str],
    params: list[Any],
    due_bucket: str,
) -> None:
    today = datetime.now().date().isoformat()
    if due_bucket == "overdue":
        clauses.append("t.status IN ('open', 'in_progress') AND t.due_date IS NOT NULL AND t.due_date < ?")
        params.append(today)
    elif due_bucket == "today":
        clauses.append("t.status IN ('open', 'in_progress') AND t.due_date = ?")
        params.append(today)
    elif due_bucket == "upcoming":
        clauses.append("t.status IN ('open', 'in_progress') AND (t.due_date IS NULL OR t.due_date > ?)")
        params.append(today)
    elif due_bucket == "waiting":
        clauses.append("t.status = 'waiting'")
    elif due_bucket == "completed":
        clauses.append("t.status IN ('done', 'cancelled')")


def query_task_rows(clauses: list[str], params: list[Any]) -> list[dict[str, Any]]:
    today = datetime.now().date().isoformat()
    rows = get_connection().execute(
        f"""
        SELECT t.*,
               prospects.business_name AS prospect_business_name,
               prospects.market AS prospect_market,
               prospects.niche AS prospect_niche,
               prospects.website_url AS prospect_website_url,
               quotes.quote_key AS quote_key,
               quotes.status AS quote_status
        FROM crm_tasks t
        JOIN prospects ON prospects.id = t.prospect_id
        LEFT JOIN quotes ON quotes.id = t.quote_id
        WHERE {" AND ".join(clauses)}
        ORDER BY
            CASE
                WHEN t.status IN ('open', 'in_progress') AND t.due_date IS NOT NULL AND t.due_date < ? THEN 0
                WHEN t.status IN ('open', 'in_progress') AND t.due_date = ? THEN 1
                WHEN t.status IN ('open', 'in_progress') THEN 2
                WHEN t.status = 'waiting' THEN 3
                ELSE 4
            END,
            COALESCE(t.due_at, '9999-12-31T23:59:59'),
            t.updated_at DESC,
            t.id DESC
        """,
        [*params, today, today],
    ).fetchall()
    return [enrich_task_row(_row_to_dict(row)) for row in rows]


def load_case_tasks(prospect_id: int) -> dict[str, Any]:
    rows = get_connection().execute(
        """
        SELECT t.*,
               p.business_name AS prospect_business_name,
               p.market AS prospect_market,
               p.niche AS prospect_niche,
               q.quote_key AS quote_key,
               q.status AS quote_status
        FROM crm_tasks t
        LEFT JOIN prospects p ON p.id = t.prospect_id
        LEFT JOIN quotes q ON q.id = t.quote_id
        WHERE t.prospect_id = ?
        ORDER BY
            CASE
                WHEN t.status IN ('open', 'in_progress') THEN 0
                WHEN t.status = 'waiting' THEN 1
                ELSE 2
            END,
            COALESCE(t.due_at, '9999-12-31T23:59:59'),
            t.updated_at DESC,
            t.id DESC
        """,
        (prospect_id,),
    ).fetchall()
    tasks = [enrich_task_row(_row_to_dict(row)) for row in rows]
    open_tasks = [task for task in tasks if task["status"] in {"open", "in_progress"}]
    waiting_tasks = [task for task in tasks if task["status"] == "waiting"]
    completed_tasks = [task for task in tasks if task["status"] in task_service.CLOSED_STATUSES]
    return {
        "all": tasks,
        "open": open_tasks,
        "waiting": waiting_tasks,
        "completed": completed_tasks,
        "next": next_task_for_case(open_tasks),
    }


def load_task_with_prospect(task_id: int) -> dict[str, Any] | None:
    rows = query_task_rows(["t.id = ?"], [task_id])
    return rows[0] if rows else None


def require_task_access(task_id: int) -> dict[str, Any]:
    task = load_task_with_prospect(task_id)
    if task is None:
        abort(404)
    require_prospect_access(int(task["prospect_id"]))
    return task


def task_contact_snapshot(
    prospect_id: int,
    contact_id: int | None,
    form: Any,
) -> dict[str, Any]:
    selected_contact = None
    if contact_id is not None:
        selected_contact = next(
            (contact for contact in load_contacts(prospect_id) if int(contact["id"]) == contact_id),
            None,
        )
    if selected_contact is None:
        contact_id = None
    manual_name = str(form.get("contact_name") or "").strip()
    manual_email = str(form.get("contact_email") or "").strip()
    manual_phone = str(form.get("contact_phone") or "").strip()
    return {
        "contact_id": contact_id,
        "contact_name": manual_name or (selected_contact or {}).get("name"),
        "contact_email": manual_email or (selected_contact or {}).get("email"),
        "contact_phone": manual_phone or (selected_contact or {}).get("phone"),
    }


def task_quote_id_for_prospect(
    connection: sqlite3.Connection,
    prospect_id: int,
    quote_id: int | None,
) -> int | None:
    if quote_id is None:
        return None
    row = connection.execute(
        "SELECT id FROM quotes WHERE id = ? AND prospect_id = ?",
        (quote_id, prospect_id),
    ).fetchone()
    if row is None:
        raise ValueError("Quote does not belong to this case.")
    return int(row["id"])


def next_task_for_case(open_tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for bucket in ("overdue", "today", "upcoming"):
        task = next((item for item in open_tasks if item.get("due_bucket") == bucket), None)
        if task:
            return task
    return open_tasks[0] if open_tasks else None


def group_tasks_for_display(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"key": "overdue", "label": "Overdue", "tasks": [task for task in tasks if task["due_bucket"] == "overdue"]},
        {"key": "today", "label": "Due Today", "tasks": [task for task in tasks if task["due_bucket"] == "today"]},
        {"key": "upcoming", "label": "Upcoming", "tasks": [task for task in tasks if task["due_bucket"] == "upcoming"]},
        {"key": "waiting", "label": "Waiting", "tasks": [task for task in tasks if task["due_bucket"] == "waiting"]},
        {"key": "completed", "label": "Completed", "tasks": [task for task in tasks if task["due_bucket"] == "completed"]},
    ]


def task_summary(tasks: list[dict[str, Any]]) -> dict[str, int]:
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    summary = {
        "overdue": 0,
        "today": 0,
        "upcoming": 0,
        "waiting": 0,
        "done_this_week": 0,
    }
    for task in tasks:
        bucket = task.get("due_bucket")
        if bucket in {"overdue", "today", "upcoming", "waiting"}:
            summary[bucket] += 1
        if task.get("status") == "done":
            completed_date = task_date(task.get("completed_at"))
            if completed_date and completed_date >= week_start:
                summary["done_this_week"] += 1
    return summary


def enrich_task_row(task: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_json_field(task.get("metadata_json"))
    task["metadata"] = metadata if isinstance(metadata, dict) else {}
    task["auto_task_key"] = str(task.get("auto_task_key") or task["metadata"].get("auto_task_key") or "").strip()
    task["is_auto"] = bool(task["auto_task_key"])
    task["task_type"] = task_service.normalize_task_type(task.get("task_type"))
    task["priority"] = task_service.normalize_priority(task.get("priority"))
    task["status"] = task_service.normalize_status(task.get("status"))
    task["type_label"] = task_service.TASK_TYPE_LABELS[task["task_type"]]
    task["priority_label"] = task_service.TASK_PRIORITY_LABELS[task["priority"]]
    task["status_label"] = task_service.TASK_STATUS_LABELS[task["status"]]
    task["due_bucket"] = task_due_bucket(task)
    task["due_label"] = task_due_label(task)
    task["is_overdue"] = task["due_bucket"] == "overdue"
    task["is_due_today"] = task["due_bucket"] == "today"
    task["is_closed"] = task["status"] in task_service.CLOSED_STATUSES
    return task


def create_auto_task_once(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    auto_task_key: str,
    task_type: str,
    title: str,
    priority: str,
    due_days: int,
    notes: str,
    quote_id: int | None = None,
    source: str = "crm_automation",
) -> int | None:
    prospect_id = int(prospect["id"])
    if existing_auto_task_id(connection, prospect_id, auto_task_key) is not None:
        return None
    actor = current_dashboard_user()
    due_date = (datetime.now().date() + timedelta(days=due_days)).isoformat()
    return task_service.create_task(
        connection,
        prospect_id=prospect_id,
        quote_id=quote_id,
        task_type=task_type,
        title=title,
        priority=priority,
        due_date=due_date,
        due_time=None,
        assigned_to=prospect.get("owner_username") or (actor.username if actor else None),
        created_by_user=actor.username if actor else None,
        owner_username=prospect.get("owner_username") or (actor.username if actor else None),
        market_state=prospect_state_from_record(prospect),
        notes=notes,
        auto_task_key=auto_task_key,
        metadata={
            "auto_created": True,
            "auto_task_key": auto_task_key,
            "source": source,
        },
    )


def existing_auto_task_id(
    connection: sqlite3.Connection,
    prospect_id: int,
    auto_task_key: str,
) -> int | None:
    row = connection.execute(
        """
        SELECT id
        FROM crm_tasks
        WHERE auto_task_key = ?
          AND status IN ('open', 'in_progress', 'waiting')
        ORDER BY id DESC
        LIMIT 1
        """,
        (auto_task_key,),
    ).fetchone()
    if row is not None:
        return int(row["id"])

    rows = connection.execute(
        """
        SELECT id, metadata_json
        FROM crm_tasks
        WHERE prospect_id = ?
          AND status IN ('open', 'in_progress', 'waiting')
        ORDER BY id DESC
        """,
        (prospect_id,),
    ).fetchall()
    for row in rows:
        metadata = parse_json_field(row["metadata_json"])
        if isinstance(metadata, dict) and metadata.get("auto_task_key") == auto_task_key:
            return int(row["id"])
    return None


def create_stage_auto_tasks(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    new_status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    business_name = str(prospect.get("business_name") or "this business")
    prospect_id = int(prospect["id"])
    source = str((metadata or {}).get("source") or "crm_stage_change")
    if new_status == "CONTACT_MADE":
        create_auto_task_once(
            connection,
            prospect=prospect,
            auto_task_key=f"contact_made_schedule_call:{prospect_id}",
            task_type="call_scheduled",
            title=f"Schedule call with {business_name}",
            priority="high",
            due_days=0,
            notes="Prospect made contact. Schedule or confirm call window.",
            source=source,
        )
    elif new_status == "CALL_BOOKED":
        create_auto_task_once(
            connection,
            prospect=prospect,
            auto_task_key=f"call_booked_prepare_call:{prospect_id}",
            task_type="call_scheduled",
            title=f"Prepare for call with {business_name}",
            priority="high",
            due_days=0,
            notes="Review case file, screenshots, visual critique, public packet, and quote options.",
            source=source,
        )
    elif new_status == "CLOSED_WON":
        create_closed_won_auto_tasks(connection, prospect=prospect, source=source)
    elif new_status == "PROJECT_ACTIVE":
        create_auto_task_once(
            connection,
            prospect=prospect,
            auto_task_key=f"project_active_access:{prospect_id}",
            task_type="client_access_needed",
            title=f"Collect access for {business_name}",
            priority="high",
            due_days=1,
            notes="Collect the logins and platform access needed to start implementation.",
            source=source,
        )


def create_closed_won_auto_tasks(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    source: str,
) -> None:
    business_name = str(prospect.get("business_name") or "this business")
    prospect_id = int(prospect["id"])
    create_auto_task_once(
        connection,
        prospect=prospect,
        auto_task_key=f"closed_won_project_handoff:{prospect_id}",
        task_type="contract_deposit",
        title="Send contract and deposit next step",
        priority="urgent",
        due_days=0,
        notes="Send SOW/payment instructions and collect launch intake.",
        source=source,
    )
    create_auto_task_once(
        connection,
        prospect=prospect,
        auto_task_key=f"closed_won_collect_assets:{prospect_id}",
        task_type="collect_assets",
        title=f"Collect website assets from {business_name}",
        priority="high",
        due_days=3,
        notes="Collect launch assets, brand files, photos, copy, access, and any existing site materials.",
        source=source,
    )


def create_quote_sent_auto_task(
    connection: sqlite3.Connection,
    *,
    prospect: dict[str, Any],
    quote: dict[str, Any],
) -> None:
    business_name = str(prospect.get("business_name") or "this business")
    create_auto_task_once(
        connection,
        prospect=prospect,
        quote_id=int(quote["id"]),
        auto_task_key=f"proposal_sent_followup:{int(prospect['id'])}:{int(quote['id'])}",
        task_type="proposal_follow_up",
        title=f"Follow up on quote for {business_name}",
        priority="high",
        due_days=2,
        notes="Ask whether they want to move forward, revise scope, or close it out.",
        source="quote_mark_sent",
    )


def create_outreach_sent_auto_task(
    connection: sqlite3.Connection,
    row: dict[str, Any],
) -> None:
    prospect = {
        "id": int(row["prospect_id"]),
        "business_name": row.get("business_name") or "this business",
        "owner_username": row.get("owner_username"),
        "market": row.get("market"),
        "market_state": row.get("market_state"),
        "state": row.get("state"),
        "state_guess": row.get("state_guess"),
    }
    business_name = str(prospect["business_name"])
    step = int(row.get("step") or OUTBOUND_DEFAULT_STEP)
    create_auto_task_once(
        connection,
        prospect=prospect,
        auto_task_key=f"outreach_sent_followup:{int(prospect['id'])}:{step}",
        task_type="follow_up",
        title=f"Check reply / follow up with {business_name}",
        priority="normal",
        due_days=3,
        notes="Do not follow up if they replied, bounced, or unsubscribed.",
        source="dashboard_send",
    )


def task_due_bucket(task: dict[str, Any]) -> str:
    status = task_service.normalize_status(task.get("status"))
    if status == "waiting":
        return "waiting"
    if status in task_service.CLOSED_STATUSES:
        return "completed"
    due_date = task_date(task.get("due_date"))
    if due_date is None:
        return "upcoming"
    today = datetime.now().date()
    if due_date < today:
        return "overdue"
    if due_date == today:
        return "today"
    return "upcoming"


def task_due_label(task: dict[str, Any]) -> str:
    if task.get("status") == "waiting" and task.get("snooze_until"):
        snooze_label = task_datetime_label(task.get("snooze_until"))
        return f"Waiting until {snooze_label}" if snooze_label else "Waiting"
    due_date = task_date(task.get("due_date"))
    if due_date is None:
        return "No due date"
    today = datetime.now().date()
    if due_date == today:
        label = "Today"
    elif due_date == today + timedelta(days=1):
        label = "Tomorrow"
    else:
        label = due_date.isoformat()
    due_time = str(task.get("due_time") or "").strip()
    return f"{label} {due_time}" if due_time else label


def task_datetime_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    due_date = task_date(text)
    if due_date is None:
        return text
    time_match = re.search(r"T(\d{2}:\d{2})", text)
    time_label = f" {time_match.group(1)}" if time_match else ""
    today = datetime.now().date()
    if due_date == today:
        return f"Today{time_label}"
    if due_date == today + timedelta(days=1):
        return f"Tomorrow{time_label}"
    return f"{due_date.isoformat()}{time_label}"


def task_date(value: Any) -> datetime.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def redirect_after_task_action(task: dict[str, Any], result: str):
    return_to = str(request.form.get("return_to") or "").strip().lower()
    if return_to == "case":
        return redirect(
            url_for(
                "case_file",
                prospect_id=int(task["prospect_id"]),
                review=f"task_{result}",
                task=int(task["id"]),
            )
            + f"#task-{int(task['id'])}"
        )
    if return_to == "tasks":
        return redirect(url_for("tasks_board", result=result))
    return redirect(url_for("task_detail", task_id=int(task["id"]), result=result))


def task_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    text = str(code)
    if text.startswith("error:"):
        return {"status": "error", "message": text.split(":", 1)[1]}
    messages = {
        "updated": "Task updated.",
        "completed": "Task marked done.",
        "cancelled": "Task cancelled.",
        "snoozed": "Task snoozed.",
    }
    message = messages.get(text)
    return {"status": "success", "message": message} if message else None


def quote_catalog_view(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = catalog or quote_service.load_quote_catalog()
    packages = []
    for key, package in (raw.get("base_packages") or {}).items():
        if not isinstance(package, dict):
            continue
        packages.append(
            {
                "key": key,
                "name": package.get("name") or key,
                "display_price": package.get("display_price") or "",
                "default_price_cents": int(package.get("default_price_cents") or 0),
                "default_price_input": cents_to_dollar_input(package.get("default_price_cents")),
                "description": package.get("description") or "",
                "best_for": package.get("best_for") or "",
                "included": package.get("included") if isinstance(package.get("included"), list) else [],
                "exclusions": package.get("exclusions") if isinstance(package.get("exclusions"), list) else [],
                "requires_custom_quote": bool(package.get("requires_custom_quote")),
                "requires_discovery": bool(package.get("requires_discovery")),
            }
        )

    addon_groups: dict[str, list[dict[str, Any]]] = {}
    for key, addon in (raw.get("addons") or {}).items():
        if not isinstance(addon, dict):
            continue
        category = str(addon.get("category") or "other")
        addon_groups.setdefault(category, []).append(
            {
                "key": key,
                "name": addon.get("name") or key,
                "description": addon.get("description") or "",
                "category": category,
                "default_price_cents": int(addon.get("default_price_cents") or 0),
                "recurring_interval": addon.get("recurring_interval"),
                "client_visible": addon.get("client_visible", True),
                "requires_discovery": bool(addon.get("requires_discovery")),
                "notes": addon.get("notes") or "",
            }
        )

    recurring = []
    for key, item in (raw.get("recurring_retainers") or {}).items():
        if not isinstance(item, dict):
            continue
        recurring.append(
            {
                "key": key,
                "name": item.get("name") or key,
                "display_price": item.get("display_price") or "",
                "description": item.get("description") or "",
                "default_price_cents": int(item.get("default_price_cents") or 0),
                "recurring_interval": item.get("recurring_interval") or "monthly",
                "client_visible": item.get("client_visible", True),
                "requires_discovery": bool(item.get("requires_discovery")),
                "notes": item.get("notes") or "",
            }
        )
    return {
        "base_packages": packages,
        "addon_groups": [
            {"category": category, "items": items}
            for category, items in sorted(addon_groups.items())
        ],
        "recurring_retainers": recurring,
    }


def quote_form_state(
    prospect: dict[str, Any],
    primary_contact: dict[str, Any] | None,
    *,
    quote: dict[str, Any] | None = None,
    form: Any | None = None,
) -> dict[str, Any]:
    catalog = quote_service.load_quote_catalog()
    valid_until_default = (datetime.now(timezone.utc) + timedelta(days=14)).date().isoformat()
    contact_metadata = (
        primary_contact.get("metadata")
        if primary_contact and isinstance(primary_contact.get("metadata"), dict)
        else {}
    )
    state: dict[str, Any] = {
        "title": f"Quote for {prospect.get('business_name') or ''}".strip(),
        "client_business_name": prospect.get("business_name") or "",
        "client_contact_name": primary_contact.get("name") if primary_contact else "",
        "client_email": primary_contact.get("email") if primary_contact else "",
        "client_phone": (primary_contact or {}).get("phone") or prospect.get("phone") or "",
        "website_url": prospect.get("website_url") or "",
        "package_key": "site_refresh",
        "package_price": "3000",
        "term_months": "0",
        "deposit_percent": "50",
        "valid_until": valid_until_default,
        "client_visible_notes": "",
        "internal_notes": "",
        "assumptions_text": "",
        "discount_amount": "",
        "discount_note": "",
        "addons": {},
        "recurring": {},
        "custom_items": [],
        "line_items": [],
    }
    if contact_metadata.get("selected_primary_email") and not state["client_email"]:
        state["client_email"] = contact_metadata["selected_primary_email"]

    for key, addon in (catalog.get("addons") or {}).items():
        state["addons"][key] = {
            "selected": False,
            "quantity": "1",
            "unit_price": cents_to_dollar_input(addon.get("default_price_cents")),
            "optional": False,
            "client_visible": bool(addon.get("client_visible", True)),
        }
    for key, item in (catalog.get("recurring_retainers") or {}).items():
        state["recurring"][key] = {
            "selected": False,
            "unit_price": cents_to_dollar_input(item.get("default_price_cents")),
            "optional": False,
            "client_visible": bool(item.get("client_visible", True)),
        }

    custom_items = []
    line_items = []
    if quote:
        state.update(
            {
                "title": quote.get("title") or state["title"],
                "client_business_name": quote.get("client_business_name") or state["client_business_name"],
                "client_contact_name": quote.get("client_contact_name") or state["client_contact_name"],
                "client_email": quote.get("client_email") or state["client_email"],
                "client_phone": quote.get("client_phone") or state["client_phone"],
                "website_url": quote.get("website_url") or state["website_url"],
                "package_key": quote.get("package_key") or state["package_key"],
                "term_months": str(quote.get("term_months") or 0),
                "deposit_percent": str(quote.get("deposit_percent") or 50),
                "valid_until": quote.get("valid_until") or state["valid_until"],
                "client_visible_notes": quote.get("client_visible_notes") or "",
                "internal_notes": quote.get("internal_notes") or "",
            }
        )
        assumptions = quote.get("assumptions") if isinstance(quote.get("assumptions"), dict) else {}
        state["assumptions_text"] = "\n".join(
            str(item) for item in assumptions.get("items", []) if str(item).strip()
        )
        for item in quote.get("line_items", []):
            item_key = str(item.get("item_key") or "")
            if item.get("item_type") == "package":
                state["package_price"] = cents_to_dollar_input(item.get("unit_price_cents"))
            elif item.get("item_type") == "addon" and item_key in state["addons"]:
                state["addons"][item_key].update(
                    {
                        "selected": True,
                        "quantity": str(item.get("quantity") or 1),
                        "unit_price": cents_to_dollar_input(item.get("unit_price_cents")),
                        "optional": bool(item.get("is_optional")),
                        "client_visible": bool(item.get("metadata", {}).get("client_visible", True)),
                    }
                )
            elif item.get("item_type") == "recurring" and item_key in state["recurring"]:
                state["recurring"][item_key].update(
                    {
                        "selected": True,
                        "unit_price": cents_to_dollar_input(item.get("unit_price_cents")),
                        "optional": bool(item.get("is_optional")),
                        "client_visible": bool(item.get("metadata", {}).get("client_visible", True)),
                    }
                )
            elif item.get("item_type") == "discount":
                state["discount_amount"] = cents_to_dollar_input(abs(int(item.get("unit_price_cents") or 0)))
                state["discount_note"] = str(item.get("metadata", {}).get("note") or "")
            elif item.get("item_type") == "custom":
                custom_items.append(
                    {
                        "name": item.get("name") or "",
                        "description": item.get("description") or "",
                        "quantity": str(item.get("quantity") or 1),
                        "unit_price": cents_to_dollar_input(item.get("unit_price_cents")),
                        "recurring_interval": item.get("recurring_interval") or "",
                        "optional": bool(item.get("is_optional")),
                        "client_visible": bool(item.get("metadata", {}).get("client_visible", True)),
                    }
                )
            if item.get("item_type") != "discount":
                line_items.append(quote_line_item_form_state(item))

    if form is not None:
        submitted_package_key = str(form.get("package_key") or "")
        state.update(
            {
                "title": str(form.get("title") or state["title"]),
                "client_business_name": str(form.get("client_business_name") or ""),
                "client_contact_name": str(form.get("client_contact_name") or ""),
                "client_email": str(form.get("client_email") or ""),
                "client_phone": str(form.get("client_phone") or ""),
                "website_url": str(form.get("website_url") or ""),
                "package_key": submitted_package_key,
                "package_price": str(
                    form.get(f"package_price_{submitted_package_key}")
                    or form.get("package_price")
                    or ""
                ),
                "term_months": str(form.get("term_months") or "0"),
                "deposit_percent": str(form.get("deposit_percent") or "50"),
                "valid_until": str(form.get("valid_until") or ""),
                "client_visible_notes": str(form.get("client_visible_notes") or ""),
                "internal_notes": str(form.get("internal_notes") or ""),
                "assumptions_text": str(form.get("assumptions_text") or ""),
                "discount_amount": str(form.get("discount_amount") or ""),
                "discount_note": str(form.get("discount_note") or ""),
            }
        )
        selected_addons = set(form.getlist("addons"))
        for key, values in state["addons"].items():
            values.update(
                {
                    "selected": key in selected_addons,
                    "quantity": str(form.get(f"addon_quantity_{key}") or "1"),
                    "unit_price": str(form.get(f"addon_price_{key}") or values["unit_price"]),
                    "optional": form.get(f"addon_optional_{key}") == "1",
                    "client_visible": form.get(f"addon_client_visible_{key}") == "1",
                }
            )
        selected_recurring = set(form.getlist("recurring"))
        for key, values in state["recurring"].items():
            values.update(
                {
                    "selected": key in selected_recurring,
                    "unit_price": str(form.get(f"recurring_price_{key}") or values["unit_price"]),
                    "optional": form.get(f"recurring_optional_{key}") == "1",
                    "client_visible": form.get(f"recurring_client_visible_{key}") == "1",
                }
            )
        custom_items = [
            {
                "name": str(form.get(f"custom_name_{index}") or ""),
                "description": str(form.get(f"custom_description_{index}") or ""),
                "quantity": str(form.get(f"custom_quantity_{index}") or "1"),
                "unit_price": str(form.get(f"custom_price_{index}") or ""),
                "recurring_interval": str(form.get(f"custom_recurring_interval_{index}") or ""),
                "optional": form.get(f"custom_optional_{index}") == "1",
                "client_visible": form.get(f"custom_client_visible_{index}") == "1",
            }
            for index in range(5)
        ]
        line_items = quote_line_items_from_form_state(form)

    while len(custom_items) < 5:
        custom_items.append(
            {
                "name": "",
                "description": "",
                "quantity": "1",
                "unit_price": "",
                "recurring_interval": "",
                "optional": False,
                "client_visible": True,
            }
        )
    state["custom_items"] = custom_items[:5]
    if not line_items:
        line_items = default_quote_line_items_for_state(state, catalog)
    state["line_items"] = line_items
    return state


def quote_line_item_form_state(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "item_key": str(item.get("item_key") or ""),
        "item_type": str(item.get("item_type") or "custom"),
        "category": str(item.get("category") or ""),
        "name": str(item.get("name") or ""),
        "description": str(item.get("description") or ""),
        "quantity": str(item.get("quantity") or 1),
        "unit_label": str(metadata.get("unit_label") or "each"),
        "unit_price": cents_to_dollar_input(item.get("unit_price_cents")),
        "recurring_interval": str(item.get("recurring_interval") or ""),
        "optional": bool(item.get("is_optional")),
        "client_visible": bool(metadata.get("client_visible", True)),
        "salesman_notes": str(metadata.get("salesman_notes") or ""),
    }


def default_quote_line_items_for_state(
    form_state: dict[str, Any],
    catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    package_key = str(form_state.get("package_key") or "").strip()
    package = (catalog.get("base_packages") or {}).get(package_key)
    if not isinstance(package, dict):
        return []
    return [
        {
            "item_key": package_key,
            "item_type": "package",
            "category": "base_package",
            "name": str(package.get("name") or package_key),
            "description": str(package.get("description") or ""),
            "quantity": "1",
            "unit_label": "each",
            "unit_price": str(form_state.get("package_price") or cents_to_dollar_input(package.get("default_price_cents"))),
            "recurring_interval": "",
            "optional": False,
            "client_visible": True,
            "salesman_notes": "",
        }
    ]


def quote_line_items_from_form_state(form: Any) -> list[dict[str, Any]]:
    try:
        count = int(str(form.get("line_item_count") or "0"))
    except ValueError:
        count = 0
    line_items = []
    for index in range(max(0, min(count, 100))):
        name = str(form.get(f"line_item_name_{index}") or "").strip()
        if not name:
            continue
        line_items.append(
            {
                "item_key": str(form.get(f"line_item_key_{index}") or ""),
                "item_type": str(form.get(f"line_item_type_{index}") or "custom"),
                "category": str(form.get(f"line_item_category_{index}") or ""),
                "name": name,
                "description": str(form.get(f"line_item_description_{index}") or ""),
                "quantity": str(form.get(f"line_item_quantity_{index}") or "1"),
                "unit_label": str(form.get(f"line_item_unit_label_{index}") or "each"),
                "unit_price": str(form.get(f"line_item_price_{index}") or ""),
                "recurring_interval": str(form.get(f"line_item_recurring_interval_{index}") or ""),
                "optional": form.get(f"line_item_optional_{index}") == "1",
                "client_visible": form.get(f"line_item_client_visible_{index}") == "1",
                "salesman_notes": str(form.get(f"line_item_salesman_notes_{index}") or ""),
            }
        )
    return line_items


def parse_quote_builder_form(
    form: Any,
    *,
    prospect: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    package_key = str(form.get("package_key") or "").strip()
    packages = catalog.get("base_packages") if isinstance(catalog.get("base_packages"), dict) else {}
    if package_key not in packages:
        raise ValueError("Choose a base package before saving the quote.")
    package = packages[package_key]
    package_price_value = (
        form.get(f"package_price_{package_key}")
        or form.get("package_price")
        or cents_to_dollar_input(package.get("default_price_cents"))
    )
    package_price = parse_quote_price(package_price_value, "Package price")
    if package_price < 0:
        raise ValueError("Package price cannot be negative.")

    internal_notes = str(form.get("internal_notes") or "").strip()
    discount_amount = parse_quote_price(form.get("discount_amount"), "Discount")
    if discount_amount < 0:
        raise ValueError("Discount cannot be negative.")
    if discount_amount and not internal_notes:
        raise ValueError("Internal notes are required when a discount is applied.")

    line_items: list[dict[str, Any]] = [
        {
            "item_key": package_key,
            "item_type": "package",
            "category": "base_package",
            "name": package.get("name") or package_key,
            "description": package.get("description") or "",
            "quantity": 1,
            "unit_price_cents": package_price,
            "sort_order": 0,
            "metadata": {
                "display_price": package.get("display_price"),
                "requires_discovery": bool(package.get("requires_discovery")),
                "requires_custom_quote": bool(package.get("requires_custom_quote")),
                "client_visible": True,
                "unit_label": "each",
                "salesman_notes": "",
            },
        }
    ]
    one_time_subtotal = package_price
    sort_order = 10

    addons = catalog.get("addons") if isinstance(catalog.get("addons"), dict) else {}
    for key in form.getlist("addons"):
        addon = addons.get(key)
        if not isinstance(addon, dict):
            continue
        quantity = parse_quote_quantity(form.get(f"addon_quantity_{key}"), addon.get("name") or key)
        unit_price = parse_quote_price(form.get(f"addon_price_{key}"), addon.get("name") or key)
        if unit_price < 0:
            raise ValueError(f"{addon.get('name') or key} price cannot be negative.")
        optional = form.get(f"addon_optional_{key}") == "1"
        line_total = quote_line_total(unit_price, quantity)
        if not optional:
            one_time_subtotal += line_total
        line_items.append(
            {
                "item_key": key,
                "item_type": "addon",
                "category": addon.get("category") or "addon",
                "name": addon.get("name") or key,
                "description": addon.get("description") or "",
                "quantity": quantity,
                "unit_price_cents": unit_price,
                "is_optional": optional,
                "sort_order": sort_order,
                "metadata": {
                    "client_visible": form.get(f"addon_client_visible_{key}") == "1",
                    "requires_discovery": bool(addon.get("requires_discovery")),
                    "notes": addon.get("notes") or "",
                    "unit_label": "each",
                    "salesman_notes": "",
                },
            }
        )
        sort_order += 10

    recurring = catalog.get("recurring_retainers") if isinstance(catalog.get("recurring_retainers"), dict) else {}
    for key in form.getlist("recurring"):
        item = recurring.get(key)
        if not isinstance(item, dict):
            continue
        unit_price = parse_quote_price(form.get(f"recurring_price_{key}"), item.get("name") or key)
        if unit_price < 0:
            raise ValueError(f"{item.get('name') or key} price cannot be negative.")
        line_items.append(
            {
                "item_key": key,
                "item_type": "recurring",
                "category": "recurring",
                "name": item.get("name") or key,
                "description": item.get("description") or "",
                "quantity": 1,
                "unit_price_cents": unit_price,
                "recurring_interval": "monthly",
                "is_optional": form.get(f"recurring_optional_{key}") == "1",
                "sort_order": sort_order,
                "metadata": {
                    "client_visible": form.get(f"recurring_client_visible_{key}") == "1",
                    "requires_discovery": bool(item.get("requires_discovery")),
                    "notes": item.get("notes") or "",
                    "unit_label": "each",
                    "salesman_notes": "",
                },
            }
        )
        sort_order += 10

    for index in range(5):
        name = str(form.get(f"custom_name_{index}") or "").strip()
        if not name:
            continue
        quantity = parse_quote_quantity(form.get(f"custom_quantity_{index}"), name)
        unit_price = parse_quote_price(form.get(f"custom_price_{index}"), name)
        if unit_price < 0:
            raise ValueError(f"{name} price cannot be negative.")
        recurring_interval = str(form.get(f"custom_recurring_interval_{index}") or "").strip() or None
        optional = form.get(f"custom_optional_{index}") == "1"
        if recurring_interval != "monthly" and not optional:
            one_time_subtotal += quote_line_total(unit_price, quantity)
        line_items.append(
            {
                "item_type": "custom",
                "category": "custom",
                "name": name,
                "description": str(form.get(f"custom_description_{index}") or "").strip(),
                "quantity": quantity,
                "unit_price_cents": unit_price,
                "recurring_interval": recurring_interval,
                "is_optional": optional,
                "sort_order": sort_order,
                "metadata": {
                    "client_visible": form.get(f"custom_client_visible_{index}") == "1",
                    "unit_label": "each",
                    "salesman_notes": "",
                },
            }
        )
        sort_order += 10

    if form.get("line_item_mode") == "explicit":
        line_items, one_time_subtotal = parse_quote_line_item_rows(form)

    if discount_amount > one_time_subtotal:
        raise ValueError("Discount cannot exceed the one-time subtotal.")
    if discount_amount:
        line_items.append(
            {
                "item_key": "manual_discount",
                "item_type": "discount",
                "category": "discount",
                "name": "Manual Discount",
                "description": str(form.get("discount_note") or "").strip(),
                "quantity": 1,
                "unit_price_cents": discount_amount,
                "sort_order": 900,
                "metadata": {
                    "note": str(form.get("discount_note") or "").strip(),
                    "unit_label": "each",
                    "salesman_notes": str(form.get("discount_note") or "").strip(),
                },
            }
        )

    term_months = parse_nonnegative_int(form.get("term_months"), "Recurring term")
    deposit_percent = parse_percent(form.get("deposit_percent"))
    assumptions_text = str(form.get("assumptions_text") or "").strip()
    assumptions = {
        "text": assumptions_text,
        "items": [line.strip() for line in assumptions_text.splitlines() if line.strip()],
    }
    package_name = str(package.get("name") or package_key)
    title = str(form.get("title") or "").strip() or f"{package_name} for {prospect.get('business_name')}"
    return {
        "package_key": package_key,
        "header": {
            "package_key": package_key,
            "package_name": package_name,
            "title": title,
            "client_business_name": str(form.get("client_business_name") or prospect.get("business_name") or "").strip(),
            "client_contact_name": str(form.get("client_contact_name") or "").strip(),
            "client_email": str(form.get("client_email") or "").strip(),
            "client_phone": str(form.get("client_phone") or prospect.get("phone") or "").strip(),
            "website_url": str(form.get("website_url") or prospect.get("website_url") or "").strip(),
            "term_months": term_months,
            "deposit_percent": deposit_percent,
            "valid_until": str(form.get("valid_until") or "").strip() or None,
            "client_visible_notes": str(form.get("client_visible_notes") or "").strip() or None,
            "assumptions": assumptions,
            "internal_notes": internal_notes or None,
            "metadata": {
                "quote_builder_version": 1,
                "discount_note": str(form.get("discount_note") or "").strip(),
                "requires_discovery": bool(package.get("requires_discovery")),
                "requires_custom_quote": bool(package.get("requires_custom_quote")),
            },
        },
        "line_items": line_items,
    }


def parse_quote_line_item_rows(form: Any) -> tuple[list[dict[str, Any]], int]:
    try:
        count = int(str(form.get("line_item_count") or "0"))
    except ValueError as exc:
        raise ValueError("Line item count must be numeric.") from exc
    line_items: list[dict[str, Any]] = []
    one_time_subtotal = 0
    for index in range(max(0, min(count, 100))):
        name = str(form.get(f"line_item_name_{index}") or "").strip()
        if not name:
            continue
        quantity = parse_quote_quantity(form.get(f"line_item_quantity_{index}"), name)
        unit_price = parse_quote_price(form.get(f"line_item_price_{index}"), name)
        if unit_price < 0:
            raise ValueError(f"{name} price cannot be negative.")
        recurring_interval = str(form.get(f"line_item_recurring_interval_{index}") or "").strip() or None
        if recurring_interval not in {None, "monthly"}:
            raise ValueError(f"{name} recurring interval must be one-time or monthly.")
        optional = form.get(f"line_item_optional_{index}") == "1"
        item_type = str(form.get(f"line_item_type_{index}") or "custom").strip().lower()
        if item_type not in quote_service.VALID_ITEM_TYPES or item_type == "discount":
            item_type = "custom"
        category = str(form.get(f"line_item_category_{index}") or ("recurring" if recurring_interval == "monthly" else "custom")).strip()
        unit_label = str(form.get(f"line_item_unit_label_{index}") or "each").strip() or "each"
        line_total = quote_line_total(unit_price, quantity)
        if recurring_interval != "monthly" and not optional:
            one_time_subtotal += line_total
        line_items.append(
            {
                "item_key": str(form.get(f"line_item_key_{index}") or "").strip() or None,
                "item_type": item_type,
                "category": category or None,
                "name": name,
                "description": str(form.get(f"line_item_description_{index}") or "").strip(),
                "quantity": quantity,
                "unit_price_cents": unit_price,
                "recurring_interval": recurring_interval,
                "is_optional": optional,
                "sort_order": index * 10,
                "metadata": {
                    "client_visible": form.get(f"line_item_client_visible_{index}") == "1",
                    "unit_label": unit_label,
                    "salesman_notes": str(form.get(f"line_item_salesman_notes_{index}") or "").strip(),
                },
            }
        )
    if not line_items:
        raise ValueError("Add at least one line item before saving the quote.")
    return line_items, one_time_subtotal


def cents_to_dollar_input(cents: Any) -> str:
    amount = int(cents or 0)
    dollars, remainder = divmod(abs(amount), 100)
    sign = "-" if amount < 0 else ""
    return f"{sign}{dollars}.{remainder:02d}" if remainder else f"{sign}{dollars}"


def parse_quote_price(value: Any, label: str) -> int:
    try:
        return quote_service.parse_money_to_cents(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid dollar amount.") from exc


def parse_quote_quantity(value: Any, label: str) -> Decimal:
    text = str(value if value not in {None, ""} else "1").strip()
    try:
        quantity = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} quantity must be numeric.") from exc
    if quantity < 0:
        raise ValueError(f"{label} quantity cannot be negative.")
    return quantity


def quote_line_total(unit_price_cents: int, quantity: Decimal) -> int:
    return int(
        (Decimal(unit_price_cents) * quantity).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def parse_nonnegative_int(value: Any, label: str) -> int:
    text = str(value if value not in {None, ""} else "0").strip()
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if number < 0:
        raise ValueError(f"{label} cannot be negative.")
    return number


def parse_percent(value: Any) -> int:
    number = parse_nonnegative_int(value if value not in {None, ""} else "50", "Deposit percent")
    if number > 100:
        raise ValueError("Deposit percent cannot exceed 100.")
    return number


def quote_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    if code == "saved":
        return {"status": "success", "message": "Quote saved."}
    if code == "deleted":
        return {"status": "success", "message": "Quote deleted."}
    if code == "revision_created":
        return {"status": "success", "message": "Quote revision created."}
    if code == "status:declined_closed_lost":
        return {"status": "success", "message": "Quote declined and CRM marked closed lost."}
    if code.startswith("status:"):
        return {"status": "success", "message": f"Quote marked {code.split(':', 1)[1]}."}
    return {"status": "success", "message": str(code)}


def handle_quote_lifecycle_action(
    connection: sqlite3.Connection,
    quote_id: int,
    status: str,
    *,
    note: str | None = None,
    close_lost: bool = False,
) -> dict[str, Any]:
    normalized = str(status or "").strip().lower()
    if normalized not in {"sent", "accepted", "declined"}:
        raise ValueError(f"Unsupported quote lifecycle action: {status}")

    quote = quote_service.update_quote_status(connection, quote_id, normalized, note=note)
    prospect = _load_prospect_from_connection(connection, int(quote["prospect_id"]))
    if prospect is None:
        raise ValueError(f"Prospect {quote['prospect_id']} does not exist.")

    if normalized == "sent":
        current_status = _normalize_token(prospect.get("status"))
        if current_status not in QUOTE_MARK_SENT_PROTECTED_STATUSES:
            apply_crm_stage_change(
                connection,
                prospect=prospect,
                new_status=state.ProspectStatus.PROPOSAL_SENT,
                note=note or f"Quote {quote.get('quote_key')} marked sent.",
                metadata={
                    "source": "quote_mark_sent",
                    "quote_id": quote.get("id"),
                    "quote_key": quote.get("quote_key"),
                },
            )
        create_quote_sent_auto_task(connection, prospect=prospect, quote=quote)
    elif normalized == "accepted":
        apply_crm_stage_change(
            connection,
            prospect=prospect,
            new_status=state.ProspectStatus.CLOSED_WON,
            note=note or f"Quote {quote.get('quote_key')} accepted.",
            metadata={
                "source": "quote_accepted",
                "quote_id": quote.get("id"),
                "quote_key": quote.get("quote_key"),
            },
        )
        create_closed_won_auto_tasks(connection, prospect=prospect, source="quote_accepted")
    elif normalized == "declined" and close_lost:
        apply_crm_stage_change(
            connection,
            prospect=prospect,
            new_status=state.ProspectStatus.CLOSED_LOST,
            note=note or f"Quote {quote.get('quote_key')} declined.",
            metadata={
                "source": "quote_declined",
                "quote_id": quote.get("id"),
                "quote_key": quote.get("quote_key"),
                "confirmed_close_lost": True,
            },
        )

    insert_quote_lifecycle_outreach_event(
        connection,
        quote=quote,
        lifecycle_event={
            "sent": "quote_marked_sent",
            "accepted": "quote_accepted",
            "declined": "quote_declined",
        }[normalized],
    )
    connection.commit()
    return quote


def insert_quote_lifecycle_outreach_event(
    connection: sqlite3.Connection,
    *,
    quote: dict[str, Any],
    lifecycle_event: str,
) -> None:
    now = utc_now()
    event_nonce = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    event_key_seed = f"{quote.get('id')}|{lifecycle_event}|{event_nonce}"
    event_key = f"{quote.get('prospect_id')}:quote_event:{stable_hash(event_key_seed)[:16]}"
    metadata = {
        "quote_id": quote.get("id"),
        "quote_key": quote.get("quote_key"),
        "quote_status": quote.get("status"),
        "quote_event_type": lifecycle_event,
        "one_time_total_cents": quote.get("one_time_total_cents"),
        "monthly_total_cents": quote.get("recurring_monthly_total_cents"),
    }
    connection.execute(
        """
        INSERT INTO outreach_events (
            event_key, prospect_id, campaign_key, channel, event_type, status,
            subject, metadata_json, created_at, updated_at
        ) VALUES (?, ?, 'quote', 'dashboard', 'quote_event', 'recorded', ?, ?, ?, ?)
        """,
        (
            event_key,
            int(quote["prospect_id"]),
            quote.get("title") or quote.get("quote_key"),
            json.dumps(metadata, sort_keys=True),
            now,
            now,
        ),
    )


def _load_prospect_from_connection(
    connection: sqlite3.Connection,
    prospect_id: int,
) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if row is None:
        return None
    prospect = _row_to_dict(row)
    prospect["pipeline_stage"] = compute_pipeline_stage(prospect)
    prospect["score_explanation"] = parse_json_field(prospect.get("score_explanation_json"))
    prospect["metadata"] = parse_json_field(prospect.get("metadata_json"))
    return prospect


def load_stage_history(prospect_id: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT *
        FROM outreach_events
        WHERE prospect_id = ?
          AND (event_type = 'crm_stage_change' OR event_type = 'quote_event' OR channel = 'email')
        ORDER BY created_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    events = []
    for row in rows:
        event = _row_to_dict(row)
        event["metadata"] = parse_json_field(event.get("metadata_json"))
        events.append(event)
    quote_rows = get_connection().execute(
        """
        SELECT *
        FROM quote_events
        WHERE prospect_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    for row in quote_rows:
        event = _row_to_dict(row)
        event["channel"] = "quote"
        event["subject"] = event.get("note") or ""
        event["metadata"] = parse_json_field(event.get("metadata_json"))
        events.append(event)
    events.sort(
        key=lambda event: (
            str(event.get("created_at") or event.get("updated_at") or ""),
            int(event.get("id") or 0),
        ),
        reverse=True,
    )
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
    append_visible_market_scope(clauses, params, filters.get("market", ""))
    append_active_prospect_filter(clauses, params)
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
        resolved_path = resolve_media_path(artifact.get("path")) or resolve_project_path(artifact.get("path"))
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
        prospect = require_prospect_access(prospect_id)
        market_state = prospect_state_from_record(prospect)
        user = current_dashboard_user()
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
                owner_username, market_state,
                send_after, subject, draft_artifact_id, public_packet_artifact_id,
                created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_key,
                prospect_id,
                row.get("contact_id"),
                email,
                OUTBOUND_DEFAULT_CAMPAIGN,
                OUTBOUND_DEFAULT_STEP,
                user.username if user else None,
                market_state,
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


def artifact_path_candidates(path: str | Path | None) -> list[str]:
    candidates: set[str] = set()
    raw = str(path or "").strip()
    if raw:
        candidates.add(raw)
        candidates.add(Path(raw).as_posix())
    resolved = resolve_project_path(path)
    if resolved is not None:
        candidates.add(str(resolved))
        candidates.add(resolved.as_posix())
        try:
            relative = resolved.relative_to(PROJECT_ROOT.resolve(strict=False))
        except ValueError:
            pass
        else:
            candidates.add(str(relative))
            candidates.add(relative.as_posix())
    return sorted(candidate for candidate in candidates if candidate)


def artifact_prospect_ids_for_path(path: str | Path | None) -> list[int]:
    candidates = artifact_path_candidates(path)
    if not candidates:
        return []
    placeholders = ", ".join("?" for _ in candidates)
    rows = get_connection().execute(
        f"""
        SELECT DISTINCT prospect_id
        FROM artifacts
        WHERE prospect_id IS NOT NULL
          AND (
            path IN ({placeholders})
            OR REPLACE(path, char(92), '/') IN ({placeholders})
          )
        """,
        [*candidates, *candidates],
    ).fetchall()
    return [int(row["prospect_id"]) for row in rows if row["prospect_id"] is not None]


def require_artifact_path_access(path: str | Path | None) -> None:
    if dashboard_user_is_admin():
        return
    prospect_ids = artifact_prospect_ids_for_path(path)
    if not prospect_ids:
        abort(404)
    for prospect_id in prospect_ids:
        require_prospect_access(prospect_id)


def require_project_file_access(path: str | Path | None) -> None:
    if dashboard_user_is_admin():
        return
    require_artifact_path_access(path)


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
    clauses = [
        "e.channel = 'email'",
        "e.campaign_key = ?",
        "e.status = 'sent'",
        "e.sent_at IS NOT NULL",
        "e.sent_at >= ?",
    ]
    params: list[Any] = [campaign, start]
    apply_prospect_scope(clauses, params, "p")
    row = get_connection().execute(
        f"""
        SELECT COUNT(*) AS count
        FROM outreach_events e
        JOIN prospects p ON p.id = e.prospect_id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchone()
    return int(row["count"] if row else 0)


def load_send_queue_rows(*, limit: int) -> list[dict[str, Any]]:
    clauses = [
        "q.step = ?",
        "q.campaign = ?",
        "UPPER(COALESCE(p.status, '')) NOT IN (?, ?, ?)",
        "UPPER(COALESCE(p.qualification_status, '')) NOT IN (?)",
    ]
    params: list[Any] = [
        OUTBOUND_DEFAULT_STEP,
        OUTBOUND_DEFAULT_CAMPAIGN,
        *TRASH_STATUSES,
        *TRASH_QUALIFICATION_STATUSES,
    ]
    apply_prospect_scope(clauses, params, "p")
    params.append(limit)
    rows = get_connection().execute(
        f"""
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
        WHERE {" AND ".join(clauses)}
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
        params,
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
    create_outreach_sent_auto_task(connection, row)


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
    if code and str(code).startswith("task_error:"):
        return str(code).split(":", 1)[1]
    messages = {
        "approved": "Approved for outreach.",
        "approved_missing_email": "Approved for outreach. No primary email was saved.",
        "rejected": "Rejected and discarded from review.",
        "held": "Held for later review.",
        "stage_updated": "CRM stage updated.",
        "contact_saved": "Primary contact saved.",
        "task_created": "Task added.",
        "task_updated": "Task updated.",
        "task_completed": "Task marked done.",
        "task_cancelled": "Task cancelled.",
        "task_snoozed": "Task snoozed.",
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


def trash_message_from_code(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    if code == "restored":
        return {"status": "success", "message": "Lead restored from trash."}
    if code.startswith("purged:"):
        parts = code.split(":")
        prospects = parts[1] if len(parts) > 1 else "0"
        files = parts[2] if len(parts) > 2 else "0"
        return {
            "status": "success",
            "message": f"Purged due screenshot media for {prospects} leads ({files} files removed).",
        }
    return None


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
        SELECT *
        FROM prospects
        WHERE id = ?
        """,
        (prospect_id,),
    ).fetchone()
    current_prospect = _row_to_dict(current) if current else {}
    old_status = current_prospect.get("status")
    old_next_action = current_prospect.get("next_action")
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
    if action == "reject":
        mark_prospect_trashed(
            connection,
            prospect_id=prospect_id,
            reason="manual_review_reject",
            category="manual_deleted",
            previous=current_prospect,
        )
    elif action in {"approve", "hold"}:
        clear_trash_metadata(connection, prospect_id)


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
    metadata: dict[str, Any] | None = None,
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
        metadata={"source": "crm_stage_form", **(metadata or {})},
    )
    if new_status == "DISCARDED":
        mark_prospect_trashed(
            connection,
            prospect_id=prospect["id"],
            reason="crm_stage_discarded",
            category="manual_deleted",
            previous=prospect,
        )
    elif new_status == "CLOSED_LOST":
        mark_prospect_trashed(
            connection,
            prospect_id=prospect["id"],
            reason="closed_lost",
            category="closed_lost",
            previous=prospect,
        )
    elif new_status not in TRASH_VISIBLE_STATUSES:
        clear_trash_metadata(connection, prospect["id"])
    create_stage_auto_tasks(
        connection,
        prospect=prospect,
        new_status=new_status,
        metadata=metadata,
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


def current_job_actor_metadata(market: str | None = None) -> dict[str, Any]:
    user = current_dashboard_user()
    market_state = configured_market_state(market)
    metadata: dict[str, Any] = {
        "requested_by_user": user.username if user else None,
        "requested_by_role": user.role if user else None,
        "market_state": market_state,
    }
    if user is not None:
        metadata["requested_by_allowed_states"] = list(user.allowed_states)
    return metadata


def ensure_current_user_can_create_market_job(market: str | None) -> str | None:
    market = str(market or "").strip() or None
    user = current_dashboard_user()
    if dashboard_user_is_admin(user):
        return configured_market_state(market)
    if not market:
        raise ValueError(territory_denial_message())
    if not current_user_can_access_market(market, user):
        raise ValueError(territory_denial_message())
    return configured_market_state(market)


def current_user_can_access_job(job: dict[str, Any]) -> bool:
    user = current_dashboard_user()
    if dashboard_user_is_admin(user):
        return True
    market_state = territories.normalize_state(job.get("market_state"))
    if not market_state:
        market_state = configured_market_state(str(job.get("market") or "").strip())
    return current_user_can_access_state(market_state, user)


def list_dashboard_jobs_for_current_user(limit: int = 50) -> list[dict[str, Any]]:
    fetch_limit = max(int(limit or 50), 50)
    jobs = dashboard_jobs.list_jobs(
        limit=fetch_limit,
        db_path=current_app.config["DATABASE_PATH"],
    )
    if dashboard_user_is_admin():
        return jobs[:limit]
    visible = [job for job in jobs if current_user_can_access_job(job)]
    return visible[:limit]


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
    if job_type == "reconcile_statuses" and not dashboard_user_is_admin():
        raise ValueError(territory_denial_message())

    limit_count = parse_job_limit(form.get("limit_count"))
    is_external = job_type in dashboard_jobs.EXTERNAL_JOB_TYPES
    if job_type == "places_pull" and limit_count is not None and limit_count > PLACES_JOB_LIMIT:
        raise ValueError(f"Places Pull is limited to {PLACES_JOB_LIMIT} rows.")
    if job_type == "audit" and limit_count is not None and limit_count > AUDIT_JOB_LIMIT:
        raise ValueError(f"Audit jobs are limited to {AUDIT_JOB_LIMIT} rows.")

    dry_run = form.get("dry_run") == "1"
    if job_type == "places_pull":
        dry_run = False
    confirmed = form.get("confirm_run") == "1"
    if is_external and not dry_run and not confirmed:
        raise ValueError("Confirm the run before starting a non-dry-run external job.")
    if job_type == "reconcile_statuses" and not dry_run and not confirmed:
        raise ValueError("Confirm apply before reconciling statuses.")

    market = str(form.get("market") or "").strip() or None
    if market == UNKNOWN_MARKET_VALUE:
        if not dashboard_user_is_admin():
            raise ValueError(territory_denial_message())
        raise ValueError("Select a specific configured market before starting a job.")
    market_state = ensure_current_user_can_create_market_job(market)
    niche = str(form.get("niche") or "").strip() or None
    if job_type == "places_pull" and (not market or not niche):
        raise ValueError("Places Pull requires a specific market and niche.")
    if job_type == "audit" and not market and form.get("allow_all_markets") != "1":
        raise ValueError("Audit jobs require a market unless Allow all markets is checked.")

    command_options: dict[str, Any] = {}
    metadata: dict[str, Any] = {
        "db_path": current_app.config["DATABASE_PATH"],
        **current_job_actor_metadata(market),
    }
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
        requested_by_user=metadata.get("requested_by_user"),
        market_state=market_state,
    )
    dashboard_jobs.run_job_async(job_key, db_path=current_app.config["DATABASE_PATH"])
    return job_key


def start_full_pipeline_from_form(form: Any) -> str:
    market = str(form.get("market") or "").strip()
    if not market or market == UNKNOWN_MARKET_VALUE:
        if market == UNKNOWN_MARKET_VALUE and not dashboard_user_is_admin():
            raise ValueError(territory_denial_message())
        raise ValueError("Choose one configured market before starting a full pipeline.")
    if market not in set(configured_market_keys()):
        raise ValueError("Full pipeline requires a market from config/markets.yaml.")
    market_state = ensure_current_user_can_create_market_job(market)

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

    dry_run_all = False
    if form.get("confirm_run") != "1":
        raise ValueError("Confirm the full pipeline before starting live external calls.")
    audit_mode = str(form.get("audit_mode") or "deep").strip().lower()
    if audit_mode not in {"deep", "fast"}:
        raise ValueError("Choose a valid audit mode.")

    actor_metadata = current_job_actor_metadata(market)
    job_key = dashboard_jobs.create_full_pipeline_job(
        market=market,
        niches=niches,
        per_niche_places_limit=places_limit,
        audit_limit=audit_limit,
        artifact_limit=artifact_limit,
        dry_run_all=dry_run_all,
        audit_fast=audit_mode == "fast",
        metadata={
            "db_path": current_app.config["DATABASE_PATH"],
            **actor_metadata,
            "include_reconcile_statuses": dashboard_user_is_admin(),
        },
        requested_by_user=actor_metadata.get("requested_by_user"),
        market_state=market_state,
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
