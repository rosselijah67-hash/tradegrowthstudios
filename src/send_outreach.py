"""Send low-volume approved outreach emails through SMTP."""

from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from . import db
from .cli_utils import build_parser, finish_command, positive_int, setup_command
from .config import load_yaml_config, project_path


COMMAND = "send_outreach"
CHANNEL = "email"
DEFAULT_CAMPAIGN = "intro_email"
DEFAULT_DAILY_CAP = 10
MAX_ATTACHMENT_BYTES = 1_500_000
SENDABLE_NEXT_ACTIONS = {
    "SEND_OUTREACH",
}
SENDABLE_STATUSES = {
    "OUTREACH_DRAFTED",
}
FOLLOWUP_NEXT_ACTIONS = {
    "WAIT_FOR_REPLY",
}
FOLLOWUP_STATUSES = {
    "OUTREACH_SENT",
}


@dataclass
class SendCandidate:
    prospect: dict[str, Any]
    contact: dict[str, Any] | None
    draft: dict[str, Any]
    email: str
    subject: str
    body: str
    event_key: str
    attachments: list[Path]


def build_arg_parser():
    parser = build_parser("Send low-volume approved outreach emails through SMTP.")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send through SMTP. Without this flag, only planned sends are logged.",
    )
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Send or preview one specific approved prospect.",
    )
    parser.add_argument(
        "--step",
        type=positive_int,
        default=1,
        help="Outreach sequence step to send. Defaults to 1.",
    )
    parser.add_argument(
        "--campaign",
        default=DEFAULT_CAMPAIGN,
        help="Campaign key from config/outreach.yaml.",
    )
    parser.add_argument(
        "--attach-screenshots",
        action="store_true",
        help="Attach approved desktop/mobile screenshots when allowed for this candidate.",
    )
    parser.add_argument(
        "--daily-cap",
        type=positive_int,
        default=None,
        help="Maximum actual sends for today. Defaults to config/outreach.yaml or 10.",
    )
    parser.add_argument(
        "--allow-missing-address",
        action="store_true",
        help="Allow actual sends without a physical mailing address. Logs a warning.",
    )
    parser.add_argument(
        "--allow-missing-unsubscribe",
        action="store_true",
        help="Allow actual sends without an unsubscribe email. Logs a warning.",
    )
    return parser


def _json_loads(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _normalize_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return None
    return email


def _env_or_default(env_key: str, default: Any = None) -> str | None:
    value = os.environ.get(env_key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _defaults(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("defaults") if isinstance(config, dict) else {}
    return value if isinstance(value, dict) else {}


def _configured_sender_name(defaults: dict[str, Any]) -> str:
    return (
        _env_or_default("OUTREACH_FROM_NAME")
        or str(defaults.get("from_name") or "").strip()
        or "Local Growth Audit"
    )


def _configured_business_name(defaults: dict[str, Any]) -> str | None:
    value = (
        _env_or_default("OUTREACH_BUSINESS_NAME")
        or str(defaults.get("business_name") or "").strip()
    )
    return value or None


def _configured_physical_address(defaults: dict[str, Any]) -> str | None:
    value = (
        _env_or_default("OUTREACH_PHYSICAL_ADDRESS")
        or _env_or_default("PHYSICAL_MAILING_ADDRESS")
        or str(defaults.get("physical_address") or "").strip()
    )
    return value or None


def _configured_unsubscribe_email(defaults: dict[str, Any]) -> str | None:
    return _normalize_email(
        _env_or_default("OUTREACH_UNSUBSCRIBE_EMAIL")
        or _env_or_default("UNSUBSCRIBE_EMAIL")
        or defaults.get("unsubscribe_email")
    )


def _configured_daily_cap(args: Any, defaults: dict[str, Any]) -> int:
    if args.daily_cap is not None:
        return int(args.daily_cap)
    configured = defaults.get("daily_cap")
    if configured in (None, ""):
        configured = defaults.get("max_emails_per_run")
    try:
        return positive_int(str(configured if configured not in (None, "") else DEFAULT_DAILY_CAP))
    except Exception:
        return DEFAULT_DAILY_CAP


def _attach_screenshots_default(defaults: dict[str, Any]) -> bool:
    return _truthy(defaults.get("attach_screenshots_default"))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _smtp_config(defaults: dict[str, Any]) -> dict[str, Any]:
    port_value = _env_or_default("SMTP_PORT", "587")
    try:
        port = int(port_value or 587)
    except ValueError:
        port = 587

    starttls_raw = _env_or_default("SMTP_STARTTLS") or _env_or_default("OUTREACH_SMTP_STARTTLS")
    if starttls_raw is None:
        starttls = port != 465
    else:
        starttls = _truthy(starttls_raw)

    reply_to_env = str(defaults.get("reply_to_env") or "").strip()
    reply_to = _env_or_default(reply_to_env) if reply_to_env else None

    return {
        "host": _env_or_default("SMTP_HOST"),
        "port": port,
        "username": _env_or_default("SMTP_USERNAME"),
        "password": _env_or_default("SMTP_PASSWORD"),
        "from_email": _normalize_email(_env_or_default("OUTREACH_FROM_EMAIL")),
        "from_name": _configured_sender_name(defaults),
        "reply_to": _normalize_email(reply_to),
        "starttls": starttls,
        "timeout": 30,
    }


def _select_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    prospect_id: int | None,
    step: int,
) -> list[dict[str, Any]]:
    if prospect_id is not None and step == 1:
        clauses = ["UPPER(COALESCE(human_review_decision, '')) = 'APPROVED'"]
        params: list[Any] = []
    else:
        allowed_next_actions = set(SENDABLE_NEXT_ACTIONS)
        allowed_statuses = set(SENDABLE_STATUSES)
        if step > 1:
            allowed_next_actions.update(FOLLOWUP_NEXT_ACTIONS)
            allowed_statuses.update(FOLLOWUP_STATUSES)

        clauses = [
            "UPPER(COALESCE(human_review_decision, '')) = 'APPROVED'",
            f"UPPER(COALESCE(next_action, '')) IN ({','.join('?' for _ in allowed_next_actions)})",
            f"UPPER(COALESCE(status, '')) IN ({','.join('?' for _ in allowed_statuses)})",
        ]
        params = sorted(allowed_next_actions) + sorted(allowed_statuses)

    if prospect_id is not None:
        clauses.append("id = ?")
        params.append(prospect_id)
    if market:
        clauses.append("market = ?")
        params.append(market)
    if niche:
        clauses.append("niche = ?")
        params.append(niche)

    sql = f"""
        SELECT *
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY expected_close_score DESC, website_pain_score DESC, id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def _load_contact(connection: Any, prospect_id: int) -> dict[str, Any] | None:
    rows = [
        db.row_to_dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM contacts
            WHERE prospect_id = ?
              AND email IS NOT NULL
              AND TRIM(email) <> ''
            """,
            (prospect_id,),
        ).fetchall()
    ]
    if not rows:
        return None

    for row in rows:
        row["metadata"] = _json_loads(row.get("metadata_json"), {})
        row["email"] = _normalize_email(row.get("email"))

    rows = [row for row in rows if row.get("email")]
    if not rows:
        return None

    rows.sort(key=_contact_sort_key)
    return rows[0]


def _contact_sort_key(contact: dict[str, Any]) -> tuple[int, int, float, int]:
    metadata = contact.get("metadata") if isinstance(contact.get("metadata"), dict) else {}
    primary = bool(
        metadata.get("primary_email")
        or metadata.get("selected_primary_email")
        or metadata.get("is_primary")
    )
    dashboard_manual = str(contact.get("source") or "") == "dashboard_manual"
    confidence = float(contact.get("confidence") or 0)
    return (-int(primary), -int(dashboard_manual), -confidence, int(contact.get("id") or 0))


def _load_draft(connection: Any, prospect_id: int, step: int) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type = 'email_draft'
          AND artifact_key = ?
          AND status = 'ready'
        LIMIT 1
        """,
        (prospect_id, f"{prospect_id}:email_{step}"),
    ).fetchone()
    if row is None:
        return None
    draft = db.row_to_dict(row)
    draft["metadata"] = _json_loads(draft.get("metadata_json"), {})
    return draft


def _read_draft_body(draft: dict[str, Any]) -> str | None:
    path = draft.get("path")
    if not path:
        return None
    resolved = project_path(path)
    if not resolved.is_file():
        return None
    text = resolved.read_text(encoding="utf-8", errors="replace")
    return _body_without_subject_or_placeholder_footer(text)


def _body_without_subject_or_placeholder_footer(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        lines = lines[1:]
        if lines and not lines[0].strip():
            lines = lines[1:]

    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() in {"-- [Your Name]", "-- {{ sender_name }}"}:
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).strip()


def _draft_subject(draft: dict[str, Any]) -> str | None:
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    subject = str(metadata.get("subject") or "").strip()
    return subject or None


def _draft_metadata_email(draft: dict[str, Any]) -> str | None:
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    return _normalize_email(metadata.get("recipient_email"))


def _recipient_email(contact: dict[str, Any] | None, draft: dict[str, Any]) -> str | None:
    return _draft_metadata_email(draft) or (contact or {}).get("email")


def _email_suppressed(connection: Any, email: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM suppression_list
        WHERE LOWER(TRIM(suppression_type)) = 'email'
          AND LOWER(TRIM(value)) = ?
          AND (expires_at IS NULL OR TRIM(expires_at) = '' OR expires_at > ?)
        LIMIT 1
        """,
        (email.lower(), db.utc_now()),
    ).fetchone()
    return row is not None


def _event_key(prospect_id: int, email: str, campaign: str, step: int) -> str:
    return f"{prospect_id}:{campaign}:{CHANNEL}:{step}:{email.lower()}"


def _duplicate_send_exists(connection: Any, event_key: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM outreach_events
        WHERE event_key = ?
          AND status IN ('sent', 'queued')
        LIMIT 1
        """,
        (event_key,),
    ).fetchone()
    return row is not None


def _sent_today_count(connection: Any, campaign: str) -> int:
    start = datetime.now(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).isoformat()
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM outreach_events
        WHERE channel = ?
          AND campaign_key = ?
          AND status = 'sent'
          AND sent_at IS NOT NULL
          AND sent_at >= ?
        """,
        (CHANNEL, campaign, start),
    ).fetchone()
    return int(row["count"] if row else 0)


def _candidate_allows_screenshot_attachments(
    *,
    prospect: dict[str, Any],
    contact: dict[str, Any] | None,
    draft: dict[str, Any],
    prospect_id_arg: int | None,
) -> bool:
    if prospect_id_arg is not None:
        return True

    payloads = []
    for row in (prospect, contact or {}, draft):
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            metadata = _json_loads(row.get("metadata_json"), {})
        if isinstance(metadata, dict):
            payloads.append(metadata)

    for metadata in payloads:
        if _truthy(metadata.get("attach_screenshots")):
            return True
        approved = metadata.get("approved_outreach")
        if isinstance(approved, dict) and _truthy(approved.get("attach_screenshots")):
            return True
    return False


def _screenshot_attachments(
    connection: Any,
    *,
    prospect: dict[str, Any],
    contact: dict[str, Any] | None,
    draft: dict[str, Any],
    prospect_id_arg: int | None,
    attach_requested: bool,
    logger: Any,
) -> list[Path]:
    if not attach_requested:
        return []
    if not _candidate_allows_screenshot_attachments(
        prospect=prospect,
        contact=contact,
        draft=draft,
        prospect_id_arg=prospect_id_arg,
    ):
        logger.warning(
            "screenshot_attachments_not_approved",
            extra={
                "event": "screenshot_attachments_not_approved",
                "prospect_id": prospect["id"],
            },
        )
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
        (prospect["id"],),
    ).fetchall()

    attachments: list[Path] = []
    for row in rows[:2]:
        path_value = row["path"]
        path = project_path(path_value)
        if not path.is_file():
            logger.warning(
                "screenshot_attachment_missing",
                extra={
                    "event": "screenshot_attachment_missing",
                    "prospect_id": prospect["id"],
                    "path": path_value,
                },
            )
            continue
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            logger.warning(
                "screenshot_attachment_too_large",
                extra={
                    "event": "screenshot_attachment_too_large",
                    "prospect_id": prospect["id"],
                    "path": path_value,
                    "size": size,
                    "max_size": MAX_ATTACHMENT_BYTES,
                },
            )
            continue
        attachments.append(path)
    return attachments


def _build_candidate(
    connection: Any,
    *,
    prospect: dict[str, Any],
    step: int,
    campaign: str,
    prospect_id_arg: int | None,
    attach_requested: bool,
    logger: Any,
) -> tuple[SendCandidate | None, str | None]:
    draft = _load_draft(connection, prospect["id"], step)
    if draft is None:
        return None, "missing_draft"

    contact = _load_contact(connection, prospect["id"])
    email = _recipient_email(contact, draft)
    if not email:
        return None, "missing_email"

    if _email_suppressed(connection, email):
        return None, "suppressed_email"

    event_key = _event_key(prospect["id"], email, campaign, step)
    if _duplicate_send_exists(connection, event_key):
        return None, "duplicate"

    subject = _draft_subject(draft)
    if not subject:
        return None, "missing_subject"

    body = _read_draft_body(draft)
    if not body:
        return None, "missing_body"

    attachments = _screenshot_attachments(
        connection,
        prospect=prospect,
        contact=contact,
        draft=draft,
        prospect_id_arg=prospect_id_arg,
        attach_requested=attach_requested,
        logger=logger,
    )
    return (
        SendCandidate(
            prospect=prospect,
            contact=contact,
            draft=draft,
            email=email,
            subject=subject,
            body=body,
            event_key=event_key,
            attachments=attachments,
        ),
        None,
    )


def _footer(
    *,
    sender_name: str,
    business_name: str | None,
    physical_address: str | None,
    unsubscribe_email: str | None,
) -> str:
    lines = ["-- ", sender_name]
    if business_name:
        lines.append(business_name)
    if physical_address:
        lines.append(physical_address)
    if unsubscribe_email:
        lines.append(f'To opt out, reply "unsubscribe" or email {unsubscribe_email}.')
    else:
        lines.append('To opt out, reply "unsubscribe".')
    return "\n".join(lines)


def _compose_message(
    candidate: SendCandidate,
    *,
    smtp_config: dict[str, Any],
    business_name: str | None,
    physical_address: str | None,
    unsubscribe_email: str | None,
) -> EmailMessage:
    message = EmailMessage()
    from_email = smtp_config["from_email"]
    from_name = smtp_config["from_name"]
    message["Subject"] = candidate.subject
    message["From"] = formataddr((from_name, from_email))
    message["To"] = candidate.email
    if smtp_config.get("reply_to"):
        message["Reply-To"] = smtp_config["reply_to"]
    if unsubscribe_email:
        message["List-Unsubscribe"] = f"<mailto:{unsubscribe_email}?subject=unsubscribe>"
    footer = _footer(
        sender_name=from_name,
        business_name=business_name,
        physical_address=physical_address,
        unsubscribe_email=unsubscribe_email,
    )
    message.set_content(f"{candidate.body.rstrip()}\n\n{footer}\n")

    for path in candidate.attachments:
        mime_type, _encoding = mimetypes.guess_type(path.name)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return message


def _send_message(message: EmailMessage, smtp_config: dict[str, Any]) -> None:
    host = smtp_config["host"]
    port = int(smtp_config["port"])
    timeout = int(smtp_config.get("timeout") or 30)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as smtp:
            _login_if_configured(smtp, smtp_config)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if smtp_config.get("starttls") and smtp.has_extn("starttls"):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        _login_if_configured(smtp, smtp_config)
        smtp.send_message(message)


def _login_if_configured(smtp: smtplib.SMTP, smtp_config: dict[str, Any]) -> None:
    username = smtp_config.get("username")
    password = smtp_config.get("password")
    if username or password:
        smtp.login(username or "", password or "")


def _upsert_outreach_event(
    connection: Any,
    *,
    candidate: SendCandidate,
    campaign: str,
    event_type: str,
    status: str,
    metadata: dict[str, Any],
    sent_at: str | None = None,
) -> None:
    now = db.utc_now()
    contact_id = candidate.contact.get("id") if candidate.contact else None
    connection.execute(
        """
        INSERT INTO outreach_events (
            event_key, prospect_id, contact_id, campaign_key, channel,
            event_type, status, subject, body_path, provider_message_id,
            metadata_json, scheduled_for, sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
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
            candidate.event_key,
            candidate.prospect["id"],
            contact_id,
            campaign,
            CHANNEL,
            event_type,
            status,
            candidate.subject,
            candidate.draft.get("path"),
            json.dumps(metadata, sort_keys=True),
            sent_at,
            now,
            now,
        ),
    )


def _mark_step_1_sent(connection: Any, prospect_id: int) -> None:
    connection.execute(
        """
        UPDATE prospects
        SET status = 'OUTREACH_SENT',
            next_action = 'WAIT_FOR_REPLY',
            updated_at = ?
        WHERE id = ?
        """,
        (db.utc_now(), prospect_id),
    )


def _candidate_metadata(candidate: SendCandidate, *, attach_requested: bool) -> dict[str, Any]:
    return {
        "recipient": candidate.email,
        "step": _draft_step(candidate.draft),
        "attach_screenshots": bool(candidate.attachments),
        "attach_screenshots_requested": attach_requested,
        "attachments": [str(path) for path in candidate.attachments],
        "draft_path": candidate.draft.get("path"),
    }


def _draft_step(draft: dict[str, Any]) -> int | None:
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    try:
        return int(metadata.get("step"))
    except (TypeError, ValueError):
        return None


def _validate_send_prerequisites(
    *,
    args: Any,
    logger: Any,
    smtp_config: dict[str, Any],
    physical_address: str | None,
    unsubscribe_email: str | None,
) -> bool:
    ok = True
    if not smtp_config.get("host") or not smtp_config.get("from_email"):
        logger.error(
            "smtp_config_missing",
            extra={
                "event": "smtp_config_missing",
                "has_host": bool(smtp_config.get("host")),
                "has_from_email": bool(smtp_config.get("from_email")),
            },
        )
        ok = False
    if (smtp_config.get("username") and not smtp_config.get("password")) or (
        smtp_config.get("password") and not smtp_config.get("username")
    ):
        logger.error(
            "smtp_auth_config_incomplete",
            extra={"event": "smtp_auth_config_incomplete"},
        )
        ok = False
    if not physical_address:
        if args.allow_missing_address:
            logger.warning(
                "missing_physical_address_allowed",
                extra={"event": "missing_physical_address_allowed"},
            )
        else:
            logger.error(
                "missing_physical_address",
                extra={"event": "missing_physical_address"},
            )
            ok = False
    if not unsubscribe_email:
        if args.allow_missing_unsubscribe:
            logger.warning(
                "missing_unsubscribe_allowed",
                extra={"event": "missing_unsubscribe_allowed"},
            )
        else:
            logger.error(
                "missing_unsubscribe",
                extra={"event": "missing_unsubscribe"},
            )
            ok = False
    return ok


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    outreach_config = load_yaml_config("outreach.yaml")
    defaults = _defaults(outreach_config)
    daily_cap = _configured_daily_cap(args, defaults)
    smtp_config = _smtp_config(defaults)
    business_name = _configured_business_name(defaults)
    physical_address = _configured_physical_address(defaults)
    unsubscribe_email = _configured_unsubscribe_email(defaults)
    send_enabled = bool(args.send and not args.dry_run)

    if args.send and args.dry_run:
        context.logger.warning(
            "send_flag_ignored_for_dry_run",
            extra={"event": "send_flag_ignored_for_dry_run"},
        )

    if send_enabled and not _validate_send_prerequisites(
        args=args,
        logger=context.logger,
        smtp_config=smtp_config,
        physical_address=physical_address,
        unsubscribe_email=unsubscribe_email,
    ):
        finish_command(context, selected=0, planned=0, sent=0, failed=0, skipped=0)
        return 1

    connection = db.init_db(args.db_path)
    prospects = _select_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        prospect_id=args.prospect_id,
        step=args.step,
    )
    sent_today = _sent_today_count(connection, args.campaign)
    remaining_today = max(0, daily_cap - sent_today)
    attach_requested = bool(
        args.attach_screenshots or _attach_screenshots_default(defaults)
    )

    planned = 0
    sent = 0
    failed = 0
    skipped = 0

    for prospect in prospects:
        if planned >= remaining_today:
            skipped += 1
            context.logger.info(
                "daily_cap_reached",
                extra={
                    "event": "daily_cap_reached",
                    "prospect_id": prospect["id"],
                    "daily_cap": daily_cap,
                    "sent_today": sent_today,
                    "planned_this_run": planned,
                },
            )
            continue

        candidate, skip_reason = _build_candidate(
            connection,
            prospect=prospect,
            step=args.step,
            campaign=args.campaign,
            prospect_id_arg=args.prospect_id,
            attach_requested=attach_requested,
            logger=context.logger,
        )
        if candidate is None:
            skipped += 1
            context.logger.info(
                "outreach_candidate_skipped",
                extra={
                    "event": "outreach_candidate_skipped",
                    "prospect_id": prospect["id"],
                    "business_name": prospect.get("business_name"),
                    "reason": skip_reason,
                    "campaign": args.campaign,
                    "step": args.step,
                },
            )
            continue

        planned += 1
        metadata = _candidate_metadata(candidate, attach_requested=attach_requested)
        if not send_enabled:
            context.logger.info(
                "outreach_would_send",
                extra={
                    "event": "outreach_would_send",
                    "prospect_id": prospect["id"],
                    "business_name": prospect.get("business_name"),
                    "recipient": candidate.email,
                    "campaign": args.campaign,
                    "step": args.step,
                    "subject": candidate.subject,
                    "draft_path": candidate.draft.get("path"),
                    "attachments": len(candidate.attachments),
                    "send_required": True,
                },
            )
            continue

        message = _compose_message(
            candidate,
            smtp_config=smtp_config,
            business_name=business_name,
            physical_address=physical_address,
            unsubscribe_email=unsubscribe_email,
        )
        try:
            _send_message(message, smtp_config)
        except Exception as exc:
            failed += 1
            failure_metadata = {
                **metadata,
                "error": str(exc)[:500],
            }
            _upsert_outreach_event(
                connection,
                candidate=candidate,
                campaign=args.campaign,
                event_type="send_failed",
                status="failed",
                metadata=failure_metadata,
                sent_at=None,
            )
            connection.commit()
            context.logger.warning(
                "outreach_send_failed",
                extra={
                    "event": "outreach_send_failed",
                    "prospect_id": prospect["id"],
                    "recipient": candidate.email,
                    "error": str(exc)[:500],
                },
            )
            continue

        sent += 1
        sent_at = db.utc_now()
        _upsert_outreach_event(
            connection,
            candidate=candidate,
            campaign=args.campaign,
            event_type="sent",
            status="sent",
            metadata=metadata,
            sent_at=sent_at,
        )
        if args.step == 1:
            _mark_step_1_sent(connection, prospect["id"])
        connection.commit()
        context.logger.info(
            "outreach_sent",
            extra={
                "event": "outreach_sent",
                "prospect_id": prospect["id"],
                "recipient": candidate.email,
                "campaign": args.campaign,
                "step": args.step,
                "subject": candidate.subject,
            },
        )

    if not send_enabled:
        context.logger.info(
            "outreach_send_not_enabled",
            extra={
                "event": "outreach_send_not_enabled",
                "reason": "dry_run" if args.dry_run else "missing_send_flag",
            },
        )

    connection.close()
    finish_command(
        context,
        selected=len(prospects),
        planned=planned,
        sent=sent,
        failed=failed,
        skipped=skipped,
        daily_cap=daily_cap,
        sent_today=sent_today,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
