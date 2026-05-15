"""Normalize prospect status/next-action drift without schema changes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .config import get_database_path, load_env
from .state import (
    AuditDataStatus,
    HumanReviewDecision,
    HumanReviewStatus,
    NextAction,
    ProspectStatus,
    QualificationStatus,
    TERMINAL_CRM_STATUSES,
    normalize_status,
)


COMMAND = "reconcile_statuses"
LATER_LIFECYCLE_STATUSES = {
    ProspectStatus.APPROVED_FOR_OUTREACH,
    ProspectStatus.OUTREACH_DRAFTED,
    ProspectStatus.OUTREACH_SENT,
    ProspectStatus.CONTACT_MADE,
    ProspectStatus.CALL_BOOKED,
    ProspectStatus.PROPOSAL_SENT,
    ProspectStatus.CLOSED_WON,
    ProspectStatus.CLOSED_LOST,
    ProspectStatus.PROJECT_ACTIVE,
    ProspectStatus.PROJECT_COMPLETE,
    ProspectStatus.DISCARDED,
}


@dataclass(frozen=True)
class PlannedChange:
    prospect_id: int
    business_name: str
    old_status: str | None
    new_status: str | None
    old_next_action: str | None
    new_next_action: str | None
    reasons: tuple[str, ...]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile prospect status/next_action values.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print planned changes only.")
    mode.add_argument("--apply", action="store_true", help="Apply planned changes.")
    parser.add_argument("--db-path", default=None, help="Override DATABASE_PATH.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum prospects to inspect.")
    return parser


def _draft_exists(connection: Any, prospect_id: int) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type = 'email_draft'
          AND status = 'ready'
        LIMIT 1
        """,
        (prospect_id,),
    ).fetchone()
    return row is not None


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _plan_for_prospect(connection: Any, prospect: dict[str, Any]) -> PlannedChange | None:
    old_status = prospect.get("status")
    old_next_action = prospect.get("next_action")
    current_status = normalize_status(old_status)
    new_status = current_status or None
    new_next_action = str(old_next_action).strip().upper() if not _is_blank(old_next_action) else None
    reasons: list[str] = []

    if current_status in TERMINAL_CRM_STATUSES:
        return None

    if old_status == "new":
        new_status = ProspectStatus.NEW
        reasons.append("normalize lowercase status new -> NEW")

    qualification_status = str(prospect.get("qualification_status") or "").strip().upper()
    audit_data_status = str(prospect.get("audit_data_status") or "").strip().upper()
    human_review_status = str(prospect.get("human_review_status") or "").strip().upper()
    human_review_decision = (
        str(prospect.get("human_review_decision") or "").strip().upper() or None
    )

    if (
        qualification_status == QualificationStatus.DISQUALIFIED
        and current_status not in LATER_LIFECYCLE_STATUSES
    ):
        new_status = ProspectStatus.INELIGIBLE
        new_next_action = NextAction.DISCARD
        reasons.append("DISQUALIFIED prospect should be INELIGIBLE/DISCARD")

    if (
        audit_data_status == AuditDataStatus.READY
        and human_review_status == HumanReviewStatus.PENDING
        and human_review_decision is None
    ):
        new_status = ProspectStatus.PENDING_REVIEW
        new_next_action = NextAction.HUMAN_REVIEW
        reasons.append("READY + PENDING review should enter PENDING_REVIEW")

    if (
        human_review_decision == HumanReviewDecision.APPROVED
        and not _draft_exists(connection, int(prospect["id"]))
    ):
        new_status = ProspectStatus.APPROVED_FOR_OUTREACH
        new_next_action = NextAction.APPROVED_FOR_OUTREACH
        reasons.append("APPROVED without draft should be APPROVED_FOR_OUTREACH")

    if new_status == ProspectStatus.OUTREACH_DRAFTED:
        new_next_action = NextAction.SEND_OUTREACH
        reasons.append("OUTREACH_DRAFTED should use SEND_OUTREACH")

    if new_status == ProspectStatus.OUTREACH_SENT:
        new_next_action = NextAction.WAIT_FOR_REPLY
        reasons.append("OUTREACH_SENT should use WAIT_FOR_REPLY")

    if new_status == old_status and new_next_action == old_next_action:
        return None
    if not reasons:
        return None
    return PlannedChange(
        prospect_id=int(prospect["id"]),
        business_name=str(prospect.get("business_name") or ""),
        old_status=old_status,
        new_status=new_status,
        old_next_action=old_next_action,
        new_next_action=new_next_action,
        reasons=tuple(reasons),
    )


def plan_changes(connection: Any, *, limit: int | None = None) -> list[PlannedChange]:
    sql = "SELECT * FROM prospects ORDER BY id"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]
    changes = []
    for prospect in rows:
        change = _plan_for_prospect(connection, prospect)
        if change is not None:
            changes.append(change)
    return changes


def apply_changes(connection: Any, changes: list[PlannedChange]) -> None:
    now = db.utc_now()
    for change in changes:
        connection.execute(
            """
            UPDATE prospects
            SET status = ?,
                next_action = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (change.new_status, change.new_next_action, now, change.prospect_id),
        )
    connection.commit()


def print_summary(changes: list[PlannedChange], *, applied: bool) -> None:
    mode = "APPLY" if applied else "DRY RUN"
    print(f"Status reconciliation {mode}")
    print(f"Planned changes: {len(changes)}")
    if not changes:
        return

    by_reason: dict[str, int] = {}
    for change in changes:
        for reason in change.reasons:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    print("")
    print("Reason summary:")
    for reason, count in sorted(by_reason.items()):
        print(f"- {reason}: {count}")

    print("")
    print("Before -> after:")
    for change in changes:
        print(
            f"- #{change.prospect_id} {change.business_name}: "
            f"status {change.old_status!r} -> {change.new_status!r}; "
            f"next_action {change.old_next_action!r} -> {change.new_next_action!r}; "
            f"reasons={'; '.join(change.reasons)}"
        )


def main() -> int:
    args = build_arg_parser().parse_args()
    load_env()
    db_path = Path(args.db_path) if args.db_path else get_database_path()
    connection = db.connect(db_path)
    try:
        changes = plan_changes(connection, limit=args.limit)
        print_summary(changes, applied=args.apply)
        if args.apply:
            apply_changes(connection, changes)
            print("")
            print(f"Applied changes: {len(changes)}")
        else:
            print("")
            print("No changes written. Re-run with --apply to update the database.")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
