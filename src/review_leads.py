"""List and record human review decisions for audited leads."""

from __future__ import annotations

import csv
from typing import Any

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "review_leads"
CSV_PATH = "runs/latest/human_review_queue.csv"
DECISIONS = {"APPROVED", "REJECTED", "PENDING"}


def _select_review_queue(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    clauses = [
        "audit_data_status = 'READY'",
        "human_review_status = 'PENDING'",
        "business_eligibility_score > 0",
    ]
    params: list[Any] = []
    if market:
        clauses.append("market = ?")
        params.append(market)
    if niche:
        clauses.append("niche = ?")
        params.append(niche)

    sql = f"""
        SELECT *
        FROM prospects
        WHERE {' AND '.join(clauses)}
        ORDER BY expected_close_score DESC, business_eligibility_score DESC, id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def _write_queue_csv(rows: list[dict[str, Any]]) -> str:
    path = project_path(CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "business_name",
        "market",
        "niche",
        "website_url",
        "business_eligibility_score",
        "website_pain_score",
        "expected_close_score",
        "next_action",
        "human_review_status",
        "human_review_decision",
        "human_review_score",
        "human_review_notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})
    return str(path)


def _record_decision(
    connection: Any,
    *,
    prospect_id: int,
    decision: str,
    score: int | None,
    notes: str | None,
) -> bool:
    row = connection.execute("SELECT id FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if not row:
        return False

    normalized = decision.upper()
    if normalized not in DECISIONS:
        raise ValueError(f"Decision must be one of {', '.join(sorted(DECISIONS))}")

    status = "PENDING" if normalized == "PENDING" else normalized
    reviewed_at = None if normalized == "PENDING" else db.utc_now()
    if normalized == "APPROVED":
        next_action = "APPROVED_FOR_OUTREACH"
        pipeline_status = "APPROVED_FOR_OUTREACH"
    elif normalized == "REJECTED":
        next_action = "REJECTED_BY_REVIEW"
        pipeline_status = "REJECTED_REVIEW"
    else:
        next_action = "HUMAN_REVIEW"
        pipeline_status = "PENDING_REVIEW"
    connection.execute(
        """
        UPDATE prospects
        SET human_review_status = ?,
            human_review_decision = ?,
            human_review_score = ?,
            human_review_notes = ?,
            human_reviewed_at = ?,
            next_action = CASE
                WHEN audit_data_status = 'READY' THEN ?
                ELSE next_action
            END,
            status = CASE
                WHEN audit_data_status = 'READY' THEN ?
                ELSE status
            END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            None if normalized == "PENDING" else normalized,
            score,
            notes,
            reviewed_at,
            next_action,
            pipeline_status,
            db.utc_now(),
            prospect_id,
        ),
    )
    return True


def main() -> int:
    parser = build_parser("List or record human review decisions for audited leads.")
    parser.add_argument("--prospect-id", type=int, default=None, help="Prospect to approve/reject.")
    parser.add_argument(
        "--decision",
        choices=["approved", "rejected", "pending"],
        default=None,
        help="Human review decision to record.",
    )
    parser.add_argument("--score", type=int, default=None, help="Optional human visual score.")
    parser.add_argument("--notes", default=None, help="Optional human review notes.")
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    connection = db.init_db(args.db_path)
    updated = 0
    csv_path = None

    if args.decision:
        if args.prospect_id is None:
            parser.error("--decision requires --prospect-id")
        if args.dry_run:
            context.logger.info(
                "human_review_would_update",
                extra={
                    "event": "human_review_would_update",
                    "prospect_id": args.prospect_id,
                    "decision": args.decision.upper(),
                    "score": args.score,
                },
            )
        else:
            updated = int(
                _record_decision(
                    connection,
                    prospect_id=args.prospect_id,
                    decision=args.decision,
                    score=args.score,
                    notes=args.notes,
                )
            )
            connection.commit()
    else:
        rows = _select_review_queue(
            connection,
            market=args.market,
            niche=args.niche,
            limit=args.limit,
        )
        for row in rows:
            print(
                f"{row['expected_close_score']:>3} | {row['id']} | "
                f"{row['business_name']} | {row.get('website_url')}"
            )
        if not args.dry_run:
            csv_path = _write_queue_csv(rows)

    connection.close()
    finish_command(context, updated=updated, csv_path=csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
