"""Sync inbound replies, bounces, and unsubscribes into the local CRM."""

from __future__ import annotations

import argparse
import csv
import email
import hashlib
import imaplib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from . import db
from .config import get_database_path, load_env, project_path
from .state import NextAction, ProspectStatus


COMMAND = "inbox_sync"
DEFAULT_SINCE_DAYS = 14
SUMMARY_JSON_PATH = "runs/latest/inbox_sync.json"
SUMMARY_TEXT_PATH = "runs/latest/inbox_sync.txt"
MANUAL_IMPORT_PATH = "runs/latest/inbound_replies.csv"

VALID_CATEGORIES = {
    "interested",
    "not_interested",
    "unsubscribe",
    "bounce",
    "auto_reply",
    "unknown_reply",
}
REPLY_CATEGORIES = {"interested", "not_interested", "unknown_reply"}
SUPPRESSION_CATEGORIES = {"unsubscribe", "bounce"}

UNSUBSCRIBE_PATTERNS = [
    r"\bunsubscribe\b",
    r"\bremove me\b",
    r"\bstop emailing\b",
    r"\btake me off\b",
    r"\bdo not contact\b",
    r"\bdon't contact\b",
]
BOUNCE_PATTERNS = [
    r"delivery status notification",
    r"delivery failure",
    r"undeliverable",
    r"mail delivery failed",
    r"address not found",
    r"recipient address rejected",
    r"message bounced",
]
AUTO_REPLY_PATTERNS = [
    r"out of office",
    r"out-of-office",
    r"automatic reply",
    r"auto.?reply",
    r"vacation responder",
    r"away from the office",
]
NOT_INTERESTED_PATTERNS = [
    r"\bnot interested\b",
    r"\bno thanks\b",
    r"\bno thank you\b",
    r"\bwe are all set\b",
    r"\balready have\b",
]
INTERESTED_PATTERNS = [
    r"\binterested\b",
    r"\bcall\b",
    r"\bprice\b",
    r"\bquote\b",
    r"\bmore info\b",
    r"\bmore information\b",
    r"\bdiscuss\b",
    r"\bsend details\b",
    r"\btell me more\b",
]


@dataclass
class InboundMessage:
    source: str
    message_id: str
    from_email: str
    subject: str
    body: str
    received_at: str | None
    references: str
    in_reply_to: str
    manual_category: str | None = None
    note: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync inbound replies, bounces, and unsubscribes into the local CRM."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect only. No database writes.")
    mode.add_argument("--apply", action="store_true", help="Apply CRM, suppression, and queue updates.")
    parser.add_argument("--since-days", type=positive_int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--db-path", default=None, help="Override DATABASE_PATH.")
    parser.add_argument(
        "--manual-csv",
        default=MANUAL_IMPORT_PATH,
        help="Manual import CSV path. Defaults to runs/latest/inbound_replies.csv.",
    )
    parser.add_argument(
        "--imap-folder",
        default="INBOX",
        help="Mailbox folder to read when IMAP config is present. Defaults to INBOX.",
    )
    parser.add_argument("--limit", type=positive_int, default=None, help="Maximum messages to process.")
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def normalize_email(value: Any) -> str | None:
    address = parseaddr(str(value or ""))[1].strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        return None
    local, domain = address.rsplit("@", 1)
    if not local or "." not in domain:
        return None
    return address


def parse_bool(value: Any, *, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def imap_configured() -> bool:
    return all(os.environ.get(key) for key in ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD"))


def load_imap_messages(*, since_days: int, folder: str, limit: int | None) -> list[InboundMessage]:
    host = os.environ["IMAP_HOST"]
    port = int(os.environ.get("IMAP_PORT") or (993 if parse_bool(os.environ.get("IMAP_USE_SSL"), default=True) else 143))
    use_ssl = parse_bool(os.environ.get("IMAP_USE_SSL"), default=True)
    username = os.environ["IMAP_USERNAME"]
    password = os.environ["IMAP_PASSWORD"]

    client_cls = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
    messages: list[InboundMessage] = []
    with client_cls(host, port) as client:
        client.login(username, password)
        client.select(folder, readonly=True)
        since_date = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
        status, data = client.search(None, "SINCE", since_date)
        if status != "OK":
            return []
        ids = data[0].split()
        if limit:
            ids = ids[-limit:]
        for msg_id in ids:
            status, fetched = client.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            for part in fetched:
                if not isinstance(part, tuple):
                    continue
                parsed = email.message_from_bytes(part[1])
                inbound = inbound_from_email(parsed, source="imap")
                if inbound:
                    messages.append(inbound)
    return messages


def inbound_from_email(message: Message, *, source: str) -> InboundMessage | None:
    from_email = normalize_email(message.get("From"))
    if not from_email:
        return None
    subject = str(message.get("Subject") or "").strip()
    message_id = str(message.get("Message-ID") or "").strip() or f"{source}:{from_email}:{subject}:{utc_now()}"
    received_at = None
    date_header = message.get("Date")
    if date_header:
        try:
            received_at = parsedate_to_datetime(date_header).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except (TypeError, ValueError, IndexError, AttributeError):
            received_at = None
    return InboundMessage(
        source=source,
        message_id=message_id,
        from_email=from_email,
        subject=subject,
        body=extract_text_body(message),
        received_at=received_at,
        references=str(message.get("References") or ""),
        in_reply_to=str(message.get("In-Reply-To") or ""),
    )


def extract_text_body(message: Message) -> str:
    if message.is_multipart():
        parts = []
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if content_type != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts).strip()
    payload = message.get_payload(decode=True)
    if payload:
        charset = message.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace").strip()
    return str(message.get_payload() or "").strip()


def load_manual_messages(path_value: str | Path, *, limit: int | None) -> list[InboundMessage]:
    path = project_path(path_value)
    if not path.is_file():
        return []
    messages = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=1):
            email_value = normalize_email(row.get("email"))
            if not email_value:
                continue
            category = normalize_category(row.get("category")) or "unknown_reply"
            note = str(row.get("note") or "").strip()
            messages.append(
                InboundMessage(
                    source="manual_csv",
                    message_id=f"manual:{email_value}:{category}:{index}",
                    from_email=email_value,
                    subject=note[:120],
                    body=note,
                    received_at=utc_now(),
                    references="",
                    in_reply_to="",
                    manual_category=category,
                    note=note,
                )
            )
            if limit and len(messages) >= limit:
                break
    return messages


def normalize_category(value: Any) -> str | None:
    category = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return category if category in VALID_CATEGORIES else None


def classify_message(message: InboundMessage) -> str:
    if message.manual_category:
        return message.manual_category
    reply_text = primary_reply_text(message.body)
    haystack = f"{message.subject}\n{reply_text}".lower()
    from_email = message.from_email.lower()
    if from_email.startswith(("mailer-daemon@", "postmaster@")) or matches_any(haystack, BOUNCE_PATTERNS):
        return "bounce"
    if matches_any(haystack, UNSUBSCRIBE_PATTERNS):
        return "unsubscribe"
    if matches_any(haystack, AUTO_REPLY_PATTERNS):
        return "auto_reply"
    if matches_any(haystack, NOT_INTERESTED_PATTERNS):
        return "not_interested"
    if matches_any(haystack, INTERESTED_PATTERNS):
        return "interested"
    return "unknown_reply"


def primary_reply_text(body: str) -> str:
    lines = []
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if not line:
            if lines:
                lines.append("")
            continue
        if line.startswith(">"):
            continue
        if line == "--":
            break
        if re.match(r"on .+ wrote:$", lower):
            break
        if lower.startswith(("from:", "sent:", "to:", "subject:")) and lines:
            break
        if "to opt out" in lower:
            break
        lines.append(raw_line)
    return "\n".join(lines).strip()


def matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def load_match_context(connection: Any) -> dict[str, Any]:
    contacts = connection.execute(
        """
        SELECT c.*, p.business_name, p.status AS prospect_status, p.next_action
        FROM contacts c
        JOIN prospects p ON p.id = c.prospect_id
        WHERE c.email IS NOT NULL
          AND TRIM(c.email) <> ''
        """
    ).fetchall()
    contacts_by_email: dict[str, list[dict[str, Any]]] = {}
    for row in contacts:
        contact = dict(row)
        email_value = normalize_email(contact.get("email"))
        if not email_value:
            continue
        contact["email"] = email_value
        contacts_by_email.setdefault(email_value, []).append(contact)

    events = connection.execute(
        """
        SELECT *
        FROM outreach_events
        WHERE channel = 'email'
          AND (status = 'sent' OR event_type = 'sent')
        ORDER BY sent_at DESC, id DESC
        """
    ).fetchall()
    events_by_message_id: dict[str, dict[str, Any]] = {}
    for row in events:
        event = dict(row)
        metadata = parse_json(event.get("metadata_json"), {})
        for key in ("message_id", "provider_message_id", "smtp_message_id"):
            value = str(metadata.get(key) or event.get("provider_message_id") or "").strip()
            if value:
                events_by_message_id[value] = event

    prospects = connection.execute(
        """
        SELECT id, business_name
        FROM prospects
        WHERE business_name IS NOT NULL
          AND TRIM(business_name) <> ''
        """
    ).fetchall()
    prospect_names = [
        (int(row["id"]), str(row["business_name"]))
        for row in prospects
        if len(str(row["business_name"]).strip()) >= 6
    ]
    return {
        "contacts_by_email": contacts_by_email,
        "events_by_message_id": events_by_message_id,
        "prospect_names": prospect_names,
    }


def match_message(message: InboundMessage, context: dict[str, Any]) -> dict[str, Any] | None:
    contacts = context["contacts_by_email"].get(message.from_email)
    if contacts:
        return {
            "prospect_id": int(contacts[0]["prospect_id"]),
            "contact_id": int(contacts[0]["id"]),
            "email": message.from_email,
            "method": "sender_contact_email",
            "business_name": contacts[0].get("business_name"),
        }

    body_emails = set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", message.body, flags=re.IGNORECASE))
    for email_value in sorted(normalize_email(value) for value in body_emails):
        if not email_value:
            continue
        contacts = context["contacts_by_email"].get(email_value)
        if contacts:
            return {
                "prospect_id": int(contacts[0]["prospect_id"]),
                "contact_id": int(contacts[0]["id"]),
                "email": email_value,
                "method": "body_contact_email",
                "business_name": contacts[0].get("business_name"),
            }

    reference_blob = f"{message.references} {message.in_reply_to}"
    for message_id, event in context["events_by_message_id"].items():
        if message_id and message_id in reference_blob:
            return {
                "prospect_id": int(event["prospect_id"]),
                "contact_id": event.get("contact_id"),
                "email": message.from_email,
                "method": "message_reference",
                "business_name": None,
            }

    subject = normalize_match_text(message.subject)
    for prospect_id, business_name in context["prospect_names"]:
        if subject_contains_business_name(subject, business_name):
            return {
                "prospect_id": prospect_id,
                "contact_id": None,
                "email": message.from_email,
                "method": "subject_business_name",
                "business_name": business_name,
            }
    return None


def normalize_match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def subject_contains_business_name(subject: str, business_name: str) -> bool:
    candidate = normalize_match_text(business_name)
    if len(candidate) < 6:
        return False
    pattern = r"(?<!\w)" + re.escape(candidate) + r"(?!\w)"
    return re.search(pattern, subject, flags=re.IGNORECASE) is not None


def apply_match(
    connection: Any,
    *,
    message: InboundMessage,
    match: dict[str, Any],
    category: str,
) -> None:
    now = utc_now()
    prospect_id = int(match["prospect_id"])
    email_value = normalize_email(match.get("email") or message.from_email)
    contact_id = match.get("contact_id")
    metadata = {
        "source": message.source,
        "message_id": message.message_id,
        "from_email": message.from_email,
        "matched_email": email_value,
        "match_method": match.get("method"),
        "category": category,
        "subject": message.subject,
        "note": message.note,
        "received_at": message.received_at,
    }

    if category in SUPPRESSION_CATEGORIES and email_value:
        upsert_suppression(connection, email_value, reason=category, metadata=metadata)
    if category == "bounce" and contact_id:
        mark_contact_bounced(connection, int(contact_id), metadata=metadata)

    if category == "unsubscribe":
        update_prospect_status(
            connection,
            prospect_id=prospect_id,
            status=ProspectStatus.DISCARDED,
            next_action=NextAction.NONE,
            now=now,
        )
    elif category == "not_interested":
        update_prospect_status(
            connection,
            prospect_id=prospect_id,
            status=ProspectStatus.CLOSED_LOST,
            next_action=NextAction.NONE,
            now=now,
        )
    elif category == "bounce":
        update_prospect_status(
            connection,
            prospect_id=prospect_id,
            status=ProspectStatus.CLOSED_LOST,
            next_action=NextAction.NONE,
            now=now,
        )
    elif category in {"interested", "unknown_reply"}:
        update_prospect_status(
            connection,
            prospect_id=prospect_id,
            status=ProspectStatus.CONTACT_MADE,
            next_action=NextAction.SCHEDULE_CALL,
            now=now,
        )

    if email_value and category != "auto_reply":
        cancel_queued_followups(connection, prospect_id=prospect_id, email_value=email_value, now=now)
    record_inbound_event(
        connection,
        prospect_id=prospect_id,
        contact_id=int(contact_id) if contact_id else None,
        category=category,
        message=message,
        metadata=metadata,
    )


def update_prospect_status(
    connection: Any,
    *,
    prospect_id: int,
    status: str,
    next_action: str,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE prospects
        SET status = ?,
            next_action = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (status, next_action, now, prospect_id),
    )


def upsert_suppression(
    connection: Any,
    email_value: str,
    *,
    reason: str,
    metadata: dict[str, Any],
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO suppression_list (
            suppression_type, value, reason, source, metadata_json, expires_at, created_at, updated_at
        ) VALUES ('email', ?, ?, 'inbox_sync', ?, NULL, ?, ?)
        ON CONFLICT(suppression_type, value) DO UPDATE SET
            reason = excluded.reason,
            source = excluded.source,
            metadata_json = excluded.metadata_json,
            expires_at = NULL,
            updated_at = excluded.updated_at
        """,
        (email_value.lower(), reason, json.dumps(metadata, sort_keys=True), now, now),
    )


def mark_contact_bounced(connection: Any, contact_id: int, *, metadata: dict[str, Any]) -> None:
    row = connection.execute(
        "SELECT metadata_json FROM contacts WHERE id = ?",
        (contact_id,),
    ).fetchone()
    current = parse_json(row["metadata_json"] if row else None, {})
    current.update(
        {
            "email_bounced": True,
            "bad_email": True,
            "bounce_detected_at": utc_now(),
            "bounce_source": "inbox_sync",
            "bounce_metadata": metadata,
        }
    )
    connection.execute(
        """
        UPDATE contacts
        SET metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(current, sort_keys=True), utc_now(), contact_id),
    )


def cancel_queued_followups(connection: Any, *, prospect_id: int, email_value: str, now: str) -> int:
    rows = connection.execute(
        """
        SELECT id, metadata_json
        FROM outreach_queue
        WHERE prospect_id = ?
          AND LOWER(email) = ?
          AND status = 'queued'
        """,
        (prospect_id, email_value.lower()),
    ).fetchall()
    for row in rows:
        metadata = parse_json(row["metadata_json"], {})
        metadata.update(
            {
                "cancelled_by": "inbox_sync",
                "cancelled_reason": "inbound_reply_or_suppression",
                "cancelled_at": now,
            }
        )
        connection.execute(
            """
            UPDATE outreach_queue
            SET status = 'cancelled',
                updated_at = ?,
                metadata_json = ?
            WHERE id = ?
            """,
            (now, json.dumps(metadata, sort_keys=True), row["id"]),
        )
    return len(rows)


def record_inbound_event(
    connection: Any,
    *,
    prospect_id: int,
    contact_id: int | None,
    category: str,
    message: InboundMessage,
    metadata: dict[str, Any],
) -> None:
    event_type = {
        "auto_reply": "auto_reply",
        "bounce": "bounce",
        "unsubscribe": "unsubscribe",
    }.get(category, "inbound_reply")
    event_key_seed = f"{prospect_id}:{category}:{message.message_id}:{message.from_email}"
    event_key = f"inbound:{hashlib.sha256(event_key_seed.encode('utf-8')).hexdigest()[:32]}"
    now = utc_now()
    connection.execute(
        """
        INSERT INTO outreach_events (
            event_key, prospect_id, contact_id, campaign_key, channel,
            event_type, status, subject, body_path, provider_message_id,
            metadata_json, scheduled_for, sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'intro_email', 'email', ?, 'recorded', ?, NULL, ?, ?, NULL, NULL, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            event_key,
            prospect_id,
            contact_id,
            event_type,
            message.subject,
            message.message_id,
            json.dumps(metadata, sort_keys=True),
            now,
            now,
        ),
    )


def process_messages(connection: Any, messages: list[InboundMessage], *, apply: bool) -> dict[str, Any]:
    context = load_match_context(connection)
    rows = []
    counts = {
        "processed": 0,
        "matched": 0,
        "unmatched": 0,
        "interested": 0,
        "not_interested": 0,
        "unsubscribe": 0,
        "bounce": 0,
        "auto_reply": 0,
        "unknown_reply": 0,
        "applied": 0,
    }
    for message in messages:
        counts["processed"] += 1
        category = classify_message(message)
        match = match_message(message, context)
        if match:
            counts["matched"] += 1
            counts[category] += 1
            if apply:
                apply_match(connection, message=message, match=match, category=category)
                counts["applied"] += 1
        else:
            counts["unmatched"] += 1
        rows.append(
            {
                "source": message.source,
                "from_email": redact_email(message.from_email),
                "subject": message.subject,
                "category": category,
                "matched": bool(match),
                "prospect_id": match.get("prospect_id") if match else None,
                "business_name": match.get("business_name") if match else None,
                "match_method": match.get("method") if match else None,
                "note": message.note,
            }
        )
    if apply:
        connection.commit()
    return {"counts": counts, "rows": rows}


def redact_email(value: Any) -> str:
    email_value = normalize_email(value)
    if not email_value:
        return ""
    local, domain = email_value.rsplit("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


def write_summary(summary: dict[str, Any]) -> None:
    json_path = project_path(SUMMARY_JSON_PATH)
    text_path = project_path(SUMMARY_TEXT_PATH)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    counts = summary.get("counts", {})
    lines = [
        f"mode: {summary.get('mode')}",
        f"source: {summary.get('source')}",
        f"created_at: {summary.get('created_at')}",
        f"processed: {counts.get('processed', 0)}",
        f"matched: {counts.get('matched', 0)}",
        f"unmatched: {counts.get('unmatched', 0)}",
        f"interested: {counts.get('interested', 0)}",
        f"not_interested: {counts.get('not_interested', 0)}",
        f"unsubscribe: {counts.get('unsubscribe', 0)}",
        f"bounce: {counts.get('bounce', 0)}",
        f"auto_reply: {counts.get('auto_reply', 0)}",
        f"unknown_reply: {counts.get('unknown_reply', 0)}",
        f"applied: {counts.get('applied', 0)}",
    ]
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def open_connection(db_path: str | Path | None, *, apply: bool) -> sqlite3.Connection:
    if apply:
        return db.init_db(db_path)
    path = Path(db_path) if db_path else get_database_path()
    if not path.is_absolute():
        path = project_path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Database not found for dry-run: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def run(args: argparse.Namespace) -> int:
    load_env()
    source = "imap" if imap_configured() else "manual_csv"
    if source == "imap":
        messages = load_imap_messages(
            since_days=args.since_days,
            folder=args.imap_folder,
            limit=args.limit,
        )
    else:
        messages = load_manual_messages(args.manual_csv, limit=args.limit)

    apply = bool(args.apply)
    connection = open_connection(args.db_path, apply=apply)
    result = process_messages(connection, messages, apply=apply)
    summary = {
        "created_at": utc_now(),
        "mode": "apply" if apply else "dry_run",
        "source": source,
        "since_days": args.since_days,
        "manual_csv": str(args.manual_csv),
        "counts": result["counts"],
        "rows": result["rows"],
    }
    write_summary(summary)
    print(json.dumps(summary["counts"], indent=2, sort_keys=True))
    connection.close()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if not args.apply:
        args.dry_run = True
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
