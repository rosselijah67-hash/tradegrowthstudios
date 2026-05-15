"""Promote locally discovered website email candidates into contact readiness."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "contact_readiness"
CSV_PATH = "runs/latest/contact_readiness.csv"
READY_PRIORITY_MAX = 5
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
EMAIL_SYNTAX_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$",
    re.IGNORECASE,
)
ROLE_LOCAL_PARTS = {
    "admin",
    "contact",
    "estimate",
    "estimates",
    "hello",
    "help",
    "info",
    "office",
    "sales",
    "schedule",
    "scheduling",
    "service",
    "support",
}
FREE_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "pm.me",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}
PRIMARY_METADATA_KEYS = (
    "primary_email",
    "selected_primary_email",
    "is_primary",
    "primary_candidate",
)


@dataclass
class EmailEvidence:
    email: str
    source_fields: set[str] = field(default_factory=set)
    existing_contact: dict[str, Any] | None = None


@dataclass
class GradedEmail:
    email: str
    syntax_valid: bool
    domain_matches_business_domain: bool
    is_role_email: bool
    is_free_email_domain: bool
    source_confidence: float
    send_priority: int
    category: str
    source_fields: list[str]
    existing_contact: dict[str, Any] | None = None
    is_suppressed: bool = False


def build_arg_parser():
    parser = build_parser("Grade local email candidates and update contact readiness.")
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Evaluate one prospect regardless of review status.",
    )
    parser.add_argument(
        "--include-unapproved",
        action="store_true",
        help="Include non-approved prospects in filtered batch mode.",
    )
    return parser


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in {None, ""}:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _normalize_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    if not email:
        return None
    if email.startswith("mailto:"):
        email = email.replace("mailto:", "", 1)
    email = email.split("?", 1)[0].strip(" <>.,;:'\"()[]{}")
    return email or None


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def _email_local(email: str) -> str:
    return email.split("@", 1)[0].lower()


def _site_domain(prospect: dict[str, Any]) -> str | None:
    domain = str(prospect.get("domain") or "").strip().lower()
    if domain:
        return domain[4:] if domain.startswith("www.") else domain
    url = str(prospect.get("website_url") or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = f"https://{url}"
    hostname = urlparse(url).hostname
    if not hostname:
        return None
    hostname = hostname.lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def _matches_business_domain(email_domain: str, business_domain: str | None) -> bool:
    if not business_domain:
        return False
    return email_domain == business_domain or email_domain.endswith(f".{business_domain}")


def _is_role_email(email: str) -> bool:
    local = _email_local(email)
    first_token = re.split(r"[._+\-]", local, maxsplit=1)[0]
    return local in ROLE_LOCAL_PARTS or first_token in ROLE_LOCAL_PARTS


def _is_primary_contact(contact: dict[str, Any] | None) -> bool:
    if not contact:
        return False
    metadata = contact.get("metadata")
    if not isinstance(metadata, dict):
        metadata = _json_loads(contact.get("metadata_json"), {})
    return any(bool(metadata.get(key)) for key in PRIMARY_METADATA_KEYS)


def _is_manual_contact(contact: dict[str, Any] | None) -> bool:
    return bool(contact and str(contact.get("source") or "") == "dashboard_manual")


def _add_evidence(
    evidence: dict[str, EmailEvidence],
    raw_email: Any,
    source_field: str,
    *,
    existing_contact: dict[str, Any] | None = None,
) -> None:
    email = _normalize_email(raw_email)
    if not email:
        return
    item = evidence.setdefault(email, EmailEvidence(email=email))
    item.source_fields.add(source_field)
    if existing_contact is not None:
        item.existing_contact = existing_contact


def _add_many(
    evidence: dict[str, EmailEvidence],
    values: Any,
    source_field: str,
) -> None:
    if isinstance(values, list):
        for value in values:
            _add_evidence(evidence, value, source_field)
    elif isinstance(values, str):
        for email in EMAIL_RE.findall(values):
            _add_evidence(evidence, email, source_field)


def _extract_from_score(prospect: dict[str, Any], evidence: dict[str, EmailEvidence]) -> None:
    score = _json_loads(prospect.get("score_explanation_json"), {})
    signals = score.get("signals") if isinstance(score, dict) else {}
    if not isinstance(signals, dict):
        return
    _add_many(evidence, signals.get("email_candidates"), "score.signals.email_candidates")
    _add_many(evidence, signals.get("business_domain_emails"), "score.signals.business_domain_emails")


def _extract_from_audits(
    connection: Any,
    prospect_id: int,
    evidence: dict[str, EmailEvidence],
) -> None:
    rows = connection.execute(
        """
        SELECT audit_type, findings_json
        FROM website_audits
        WHERE prospect_id = ?
        """,
        (prospect_id,),
    ).fetchall()
    for row in rows:
        audit_type = str(row["audit_type"] or "audit")
        findings = _json_loads(row["findings_json"], {})
        if isinstance(findings, dict):
            for key in ("mailto_emails", "visible_emails", "email_candidates", "business_domain_emails"):
                _add_many(evidence, findings.get(key), f"website_audits.{audit_type}.{key}")


def _load_existing_contacts(connection: Any, prospect_id: int) -> list[dict[str, Any]]:
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
    for row in rows:
        row["metadata"] = _json_loads(row.get("metadata_json"), {})
        row["email"] = _normalize_email(row.get("email"))
    return [row for row in rows if row.get("email")]


def _extract_from_contacts(
    contacts: list[dict[str, Any]],
    evidence: dict[str, EmailEvidence],
) -> None:
    for contact in contacts:
        source = str(contact.get("source") or "existing_contact")
        _add_evidence(
            evidence,
            contact.get("email"),
            f"contacts.{source}",
            existing_contact=contact,
        )


def _category_for(
    *,
    syntax_valid: bool,
    existing_contact: dict[str, Any] | None,
    domain_match: bool,
    role_email: bool,
    free_email: bool,
) -> str:
    if not syntax_valid:
        return "rejected_invalid"
    if _is_manual_contact(existing_contact):
        return "existing_manual_contact"
    if domain_match and not role_email:
        return "website_business_domain_direct"
    if domain_match and role_email:
        return "website_role_email"
    if free_email:
        return "website_free_email"
    return "unknown_source"


def _priority_for(category: str, existing_contact: dict[str, Any] | None) -> int:
    if category == "existing_manual_contact" and _is_primary_contact(existing_contact):
        return 1
    if category == "existing_manual_contact":
        return 2
    return {
        "website_business_domain_direct": 3,
        "website_role_email": 4,
        "website_free_email": 5,
        "unknown_source": 6,
        "rejected_invalid": 99,
    }.get(category, 98)


def _confidence_for(category: str, source_fields: set[str]) -> float:
    base = {
        "existing_manual_contact": 0.95,
        "website_business_domain_direct": 0.85,
        "website_role_email": 0.72,
        "website_free_email": 0.55,
        "unknown_source": 0.4,
        "rejected_invalid": 0.0,
    }.get(category, 0.3)
    if any("mailto_emails" in field for field in source_fields):
        base += 0.03
    if any(field.endswith(".summary") for field in source_fields):
        base -= 0.05
    return max(0.0, min(0.99, round(base, 2)))


def grade_candidate(
    prospect: dict[str, Any],
    evidence: EmailEvidence,
) -> GradedEmail:
    syntax_valid = bool(EMAIL_SYNTAX_RE.match(evidence.email))
    email_domain = _email_domain(evidence.email)
    business_domain = _site_domain(prospect)
    domain_match = _matches_business_domain(email_domain, business_domain)
    role_email = _is_role_email(evidence.email)
    free_email = email_domain in FREE_EMAIL_DOMAINS
    category = _category_for(
        syntax_valid=syntax_valid,
        existing_contact=evidence.existing_contact,
        domain_match=domain_match,
        role_email=role_email,
        free_email=free_email,
    )
    return GradedEmail(
        email=evidence.email,
        syntax_valid=syntax_valid,
        domain_matches_business_domain=domain_match,
        is_role_email=role_email,
        is_free_email_domain=free_email,
        source_confidence=_confidence_for(category, evidence.source_fields),
        send_priority=_priority_for(category, evidence.existing_contact),
        category=category,
        source_fields=sorted(evidence.source_fields),
        existing_contact=evidence.existing_contact,
    )


def sort_key(candidate: GradedEmail) -> tuple[int, float, str]:
    return (candidate.send_priority, -candidate.source_confidence, candidate.email)


def is_email_suppressed(connection: Any, email: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM suppression_list
        WHERE LOWER(COALESCE(suppression_type, '')) = 'email'
          AND LOWER(TRIM(value)) = ?
          AND (expires_at IS NULL OR TRIM(expires_at) = '' OR expires_at > ?)
        LIMIT 1
        """,
        (email.lower(), db.utc_now()),
    ).fetchone()
    return row is not None


def select_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    prospect_id: int | None,
    include_unapproved: bool,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if prospect_id is not None:
        clauses.append("id = ?")
        params.append(prospect_id)
    else:
        if market:
            clauses.append("market = ?")
            params.append(market)
        if niche:
            clauses.append("niche = ?")
            params.append(niche)
        if not include_unapproved:
            clauses.append(
                """
                (
                    UPPER(COALESCE(human_review_decision, '')) = 'APPROVED'
                    OR UPPER(COALESCE(status, '')) IN ('APPROVED_FOR_OUTREACH', 'OUTREACH_DRAFTED')
                    OR UPPER(COALESCE(next_action, '')) IN ('APPROVED_FOR_OUTREACH', 'SEND_OUTREACH')
                )
                """
            )
    where_sql = " AND ".join(clauses) if clauses else "1 = 1"
    sql = f"""
        SELECT *
        FROM prospects
        WHERE {where_sql}
        ORDER BY expected_close_score DESC, id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def collect_evidence(connection: Any, prospect: dict[str, Any]) -> tuple[list[GradedEmail], int]:
    evidence: dict[str, EmailEvidence] = {}
    contacts = _load_existing_contacts(connection, prospect["id"])
    _extract_from_score(prospect, evidence)
    _extract_from_audits(connection, prospect["id"], evidence)
    _extract_from_contacts(contacts, evidence)
    graded = [grade_candidate(prospect, item) for item in evidence.values()]
    rejected = 0
    sendable: list[GradedEmail] = []
    for item in graded:
        if not item.syntax_valid:
            rejected += 1
            continue
        if item.send_priority > READY_PRIORITY_MAX:
            rejected += 1
            continue
        if is_email_suppressed(connection, item.email):
            item.is_suppressed = True
            rejected += 1
            continue
        sendable.append(item)
    return sorted(sendable, key=sort_key), rejected


def readiness_metadata(best: GradedEmail | None, sendable_count: int, rejected_count: int) -> dict[str, Any]:
    return {
        "best_email": best.email if best else None,
        "best_email_category": best.category if best else None,
        "sendable_email_count": sendable_count,
        "rejected_email_count": rejected_count,
        "contact_ready": best is not None,
    }


def _contact_metadata(candidate: GradedEmail, *, primary_candidate: bool) -> dict[str, Any]:
    metadata = {
        "category": candidate.category,
        "source_fields": candidate.source_fields,
        "send_priority": candidate.send_priority,
        "website_domain_match": candidate.domain_matches_business_domain,
        "syntax_valid": candidate.syntax_valid,
        "domain_matches_business_domain": candidate.domain_matches_business_domain,
        "is_role_email": candidate.is_role_email,
        "is_free_email_domain": candidate.is_free_email_domain,
        "source_confidence": candidate.source_confidence,
        "primary_candidate": primary_candidate,
    }
    if primary_candidate:
        metadata.update({"primary_email": True, "is_primary": True})
    return metadata


def upsert_contact(
    connection: Any,
    *,
    prospect_id: int,
    candidate: GradedEmail,
    primary_candidate: bool,
) -> int:
    now = db.utc_now()
    existing = connection.execute(
        """
        SELECT *
        FROM contacts
        WHERE prospect_id = ?
          AND LOWER(COALESCE(email, '')) = ?
        LIMIT 1
        """,
        (prospect_id, candidate.email),
    ).fetchone()
    metadata_update = _contact_metadata(candidate, primary_candidate=primary_candidate)
    if existing is not None:
        row = db.row_to_dict(existing)
        metadata = _json_loads(row.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["contact_readiness"] = metadata_update
        if primary_candidate:
            metadata.update({"primary_candidate": True, "primary_email": True, "is_primary": True})
        preserve_manual = str(row.get("source") or "") == "dashboard_manual"
        new_source = row.get("source") if preserve_manual else "contact_readiness"
        new_confidence = (
            row.get("confidence")
            if preserve_manual and row.get("confidence") is not None
            else candidate.source_confidence
        )
        metadata_json = json.dumps(metadata, sort_keys=True)
        if (
            str(row.get("source") or "") == str(new_source or "")
            and row.get("confidence") == new_confidence
            and str(row.get("metadata_json") or "{}") == metadata_json
        ):
            return int(row["id"])
        connection.execute(
            """
            UPDATE contacts
            SET source = ?,
                confidence = ?,
                metadata_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_source,
                new_confidence,
                metadata_json,
                now,
                row["id"],
            ),
        )
        return int(row["id"])

    contact_key = f"contact_readiness:{prospect_id}:{db.stable_hash(candidate.email)[:16]}"
    metadata = {"contact_readiness": metadata_update}
    if primary_candidate:
        metadata.update({"primary_candidate": True, "primary_email": True, "is_primary": True})
    connection.execute(
        """
        INSERT INTO contacts (
            contact_key, prospect_id, contact_type, role, email, source,
            confidence, metadata_json, created_at, updated_at
        ) VALUES (?, ?, 'business', ?, ?, 'contact_readiness', ?, ?, ?, ?)
        ON CONFLICT(contact_key) DO UPDATE SET
            email = excluded.email,
            source = excluded.source,
            confidence = excluded.confidence,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            contact_key,
            prospect_id,
            "email candidate",
            candidate.email,
            candidate.source_confidence,
            json.dumps(metadata, sort_keys=True),
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM contacts WHERE contact_key = ?",
        (contact_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Contact upsert failed for {candidate.email}")
    return int(row["id"])


def has_primary_email_contact(connection: Any, prospect_id: int) -> bool:
    for contact in _load_existing_contacts(connection, prospect_id):
        if _is_primary_contact(contact):
            return True
    return False


def demote_other_primary_candidates(connection: Any, *, prospect_id: int, primary_contact_id: int) -> None:
    rows = connection.execute(
        """
        SELECT id, metadata_json
        FROM contacts
        WHERE prospect_id = ?
          AND id <> ?
        """,
        (prospect_id, primary_contact_id),
    ).fetchall()
    now = db.utc_now()
    for row in rows:
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        changed = False
        for key in PRIMARY_METADATA_KEYS:
            if metadata.get(key):
                metadata[key] = False
                changed = True
        readiness = metadata.get("contact_readiness")
        if isinstance(readiness, dict) and readiness.get("primary_candidate"):
            readiness["primary_candidate"] = False
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


def update_prospect_readiness(
    connection: Any,
    *,
    prospect: dict[str, Any],
    readiness: dict[str, Any],
) -> None:
    metadata = _json_loads(prospect.get("metadata_json"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["contact_readiness"] = readiness
    connection.execute(
        """
        UPDATE prospects
        SET metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, sort_keys=True), db.utc_now(), prospect["id"]),
    )


def redact_email(email: str | None) -> str:
    if not email:
        return ""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 2:
        redacted_local = f"{local[:1]}***"
    else:
        redacted_local = f"{local[:2]}***"
    return f"{redacted_local}@{domain}"


def csv_reason(best: GradedEmail | None, rejected_count: int, total_count: int) -> str:
    if best:
        return f"ready via {best.category}"
    if total_count == 0:
        return "no email candidates found in local evidence"
    if rejected_count == total_count:
        return "no safe website-published email candidate selected"
    return "no safe sendable email candidate selected"


def write_csv(rows: list[dict[str, Any]]) -> str:
    path = project_path(CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "prospect_id",
        "business_name",
        "market",
        "niche",
        "website_url",
        "best_email_redacted",
        "best_email_category",
        "contact_ready",
        "sendable_email_count",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return CSV_PATH


def process_prospect(connection: Any, prospect: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    candidates, rejected_count = collect_evidence(connection, prospect)
    best = candidates[0] if candidates else None
    readiness = readiness_metadata(best, len(candidates), rejected_count)
    primary_exists = has_primary_email_contact(connection, prospect["id"])
    upserted = 0
    if not dry_run:
        primary_contact_id = None
        for candidate in candidates:
            is_best_primary = best is not None and candidate.email == best.email and not primary_exists
            contact_id = upsert_contact(
                connection,
                prospect_id=prospect["id"],
                candidate=candidate,
                primary_candidate=is_best_primary,
            )
            upserted += 1
            if is_best_primary:
                primary_contact_id = contact_id
        if primary_contact_id is not None:
            demote_other_primary_candidates(
                connection,
                prospect_id=prospect["id"],
                primary_contact_id=primary_contact_id,
            )
        update_prospect_readiness(connection, prospect=prospect, readiness=readiness)

    total_candidates = len(candidates) + rejected_count
    return {
        "csv": {
            "prospect_id": prospect["id"],
            "business_name": prospect.get("business_name"),
            "market": prospect.get("market"),
            "niche": prospect.get("niche"),
            "website_url": prospect.get("website_url"),
            "best_email_redacted": redact_email(best.email if best else None),
            "best_email_category": best.category if best else "",
            "contact_ready": bool(best),
            "sendable_email_count": len(candidates),
            "reason": csv_reason(best, rejected_count, total_candidates),
        },
        "ready": bool(best),
        "sendable": len(candidates),
        "rejected": rejected_count,
        "contacts_upserted": upserted,
        "best_category": best.category if best else None,
    }


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    connection = db.init_db(args.db_path)
    prospects = select_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        prospect_id=args.prospect_id,
        include_unapproved=args.include_unapproved,
    )

    csv_rows: list[dict[str, Any]] = []
    processed = 0
    ready_count = 0
    contacts_upserted = 0
    sendable_candidates = 0
    rejected_candidates = 0

    for prospect in prospects:
        result = process_prospect(connection, prospect, dry_run=args.dry_run)
        csv_rows.append(result["csv"])
        processed += 1
        ready_count += int(result["ready"])
        contacts_upserted += int(result["contacts_upserted"])
        sendable_candidates += int(result["sendable"])
        rejected_candidates += int(result["rejected"])
        context.logger.info(
            "contact_readiness_evaluated",
            extra={
                "event": "contact_readiness_evaluated",
                "prospect_id": prospect["id"],
                "business_name": prospect.get("business_name"),
                "contact_ready": result["ready"],
                "sendable_email_count": result["sendable"],
                "rejected_email_count": result["rejected"],
                "best_email_category": result["best_category"],
            },
        )

    csv_path = write_csv(csv_rows)
    if args.dry_run:
        connection.rollback()
    else:
        connection.commit()
    connection.close()

    finish_command(
        context,
        selected=len(prospects),
        processed=processed,
        contact_ready=ready_count,
        contacts_upserted=contacts_upserted,
        sendable_candidates=sendable_candidates,
        rejected_candidates=rejected_candidates,
        csv_path=csv_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
