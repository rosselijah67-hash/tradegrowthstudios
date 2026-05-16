"""Reclassify auto-rejected no-website prospects into the No Website bucket."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import db
from src.config import get_database_path, load_env
from src.state import NextAction, ProspectStatus, QualificationStatus


MANUAL_TRASH_REASONS = {"manual_review_reject", "crm_stage_discarded", "quick_deleted"}
PROTECTED_STATUSES = {
    ProspectStatus.CLOSED_WON,
    ProspectStatus.CLOSED_LOST,
    ProspectStatus.PROJECT_ACTIVE,
    ProspectStatus.PROJECT_COMPLETE,
    ProspectStatus.OUTREACH_SENT,
    ProspectStatus.CONTACT_MADE,
    ProspectStatus.CALL_BOOKED,
    ProspectStatus.PROPOSAL_SENT,
    ProspectStatus.DISCARDED,
    ProspectStatus.REJECTED_REVIEW,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move prospects that were auto-filtered only because they have no website "
            "out of trash and into NO_WEBSITE/COLD_CALL_WEBSITE."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print planned changes only.")
    mode.add_argument("--apply", action="store_true", help="Apply planned changes.")
    parser.add_argument("--db-path", default=None, help="Override DATABASE_PATH.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to inspect.")
    return parser


def json_loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def token(value: Any) -> str:
    return str(value or "").strip().upper()


def has_no_website(row: dict[str, Any]) -> bool:
    return not str(row.get("website_url") or "").strip() and not str(row.get("domain") or "").strip()


def contains_missing_website(value: Any) -> bool:
    if isinstance(value, str):
        return "missing" in value.lower() and "website" in value.lower()
    if isinstance(value, dict):
        return contains_missing_website(value.get("reason"))
    return False


def forced_reasons(metadata: dict[str, Any]) -> list[str]:
    pre_audit = metadata.get("pre_audit_eligibility")
    if not isinstance(pre_audit, dict):
        return []
    return [str(reason or "").strip() for reason in pre_audit.get("forced_reasons") or []]


def was_auto_no_website_reject(row: dict[str, Any], metadata: dict[str, Any]) -> bool:
    if not has_no_website(row):
        return False
    if token(row.get("status")) in PROTECTED_STATUSES:
        return False

    trash = metadata.get("trash") if isinstance(metadata.get("trash"), dict) else {}
    if str(trash.get("category") or "").strip().lower() == "manual_deleted":
        return False
    if str(trash.get("reason") or "").strip().lower() in MANUAL_TRASH_REASONS:
        return False

    disqualification_reason = str(metadata.get("disqualification_reason") or "").strip().lower()
    if disqualification_reason == "missing_website":
        return True
    if disqualification_reason:
        return False

    reasons = forced_reasons(metadata)
    if reasons and all(contains_missing_website(reason) for reason in reasons):
        return True

    pre_audit = metadata.get("pre_audit_eligibility")
    if isinstance(pre_audit, dict):
        signals = pre_audit.get("signals") if isinstance(pre_audit.get("signals"), dict) else {}
        reason_items = pre_audit.get("all_reasons") or pre_audit.get("top_reasons") or []
        if signals.get("has_website_url") is False and any(
            contains_missing_website(item) for item in reason_items
        ):
            return True

    return False


def reclassified_metadata(row: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata)
    now = db.utc_now()
    trash = updated.get("trash") if isinstance(updated.get("trash"), dict) else None
    if trash:
        history = updated.setdefault("trash_history", [])
        if isinstance(history, list):
            history.append({**trash, "reclassified_to_no_website_at": now})
        updated.pop("trash", None)

    pre_audit = updated.get("pre_audit_eligibility")
    if isinstance(pre_audit, dict):
        pre_audit = dict(pre_audit)
        pre_audit["qualification_status"] = QualificationStatus.NO_WEBSITE
        pre_audit["status"] = ProspectStatus.NO_WEBSITE
        pre_audit["next_action"] = NextAction.COLD_CALL_WEBSITE
        pre_audit["no_website_bucket"] = True
        updated["pre_audit_eligibility"] = pre_audit

    updated["no_website_bucket"] = {
        "active": True,
        "source": "reclassify_no_website_leads",
        "reason": "missing_website",
        "reclassified_at": now,
        "previous_status": row.get("status"),
        "previous_qualification_status": row.get("qualification_status"),
        "previous_next_action": row.get("next_action"),
    }
    return updated


def load_candidates(connection: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT *
        FROM prospects
        WHERE TRIM(COALESCE(website_url, '')) = ''
          AND TRIM(COALESCE(domain, '')) = ''
        ORDER BY id
    """
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    candidates = []
    for row in connection.execute(sql, params).fetchall():
        prospect = db.row_to_dict(row)
        metadata = json_loads(prospect.get("metadata_json"))
        if was_auto_no_website_reject(prospect, metadata):
            prospect["metadata"] = metadata
            candidates.append(prospect)
    return candidates


def apply_candidates(connection: Any, candidates: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for prospect in candidates:
        metadata = reclassified_metadata(prospect, prospect["metadata"])
        connection.execute(
            """
            UPDATE prospects
            SET qualification_status = ?,
                status = ?,
                next_action = ?,
                metadata_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                QualificationStatus.NO_WEBSITE,
                ProspectStatus.NO_WEBSITE,
                NextAction.COLD_CALL_WEBSITE,
                json.dumps(metadata, sort_keys=True),
                now,
                prospect["id"],
            ),
        )
    connection.commit()


def print_summary(candidates: list[dict[str, Any]], *, applied: bool, db_path: Path) -> None:
    print("No-website reclassification " + ("APPLY" if applied else "DRY RUN"))
    print(f"Database: {db_path}")
    print(f"Candidates: {len(candidates)}")
    for prospect in candidates[:100]:
        print(
            f"- #{prospect['id']} {prospect.get('business_name') or ''} "
            f"{prospect.get('market') or ''}: "
            f"{prospect.get('qualification_status')}/{prospect.get('status')} -> "
            f"{QualificationStatus.NO_WEBSITE}/{ProspectStatus.NO_WEBSITE}"
        )
    if len(candidates) > 100:
        print(f"... {len(candidates) - 100} more")
    if not applied:
        print("No changes written. Re-run with --apply to update the database.")


def main() -> int:
    args = build_parser().parse_args()
    load_env()
    db_path = Path(args.db_path) if args.db_path else get_database_path()
    connection = db.connect(db_path)
    try:
        candidates = load_candidates(connection, limit=args.limit)
        if args.apply:
            apply_candidates(connection, candidates)
        print_summary(candidates, applied=args.apply, db_path=db_path)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
