"""Pre-audit eligibility scoring from local and Places-derived prospect data."""

from __future__ import annotations

import csv
import json
from typing import Any

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import load_yaml_config, project_path
from .franchise_filter import check_franchise_exclusion


COMMAND = "eligibility"
CSV_PATH = "runs/latest/eligibility_summary.csv"
QUALIFICATION_THRESHOLD = 55
ELIGIBLE_SELECTION_STATUSES = ("DISCOVERED", "QUALIFIED", "DISQUALIFIED")
PROTECTED_LIFECYCLE_STATUSES = (
    "CLOSED_WON",
    "CLOSED_LOST",
    "PROJECT_ACTIVE",
    "PROJECT_COMPLETE",
    "OUTREACH_SENT",
    "CONTACT_MADE",
    "CALL_BOOKED",
    "PROPOSAL_SENT",
)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _clamp_score(value: int) -> int:
    return max(0, min(100, value))


def _has_value(value: Any) -> bool:
    return bool(str(value or "").strip())


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _market_priority(markets_config: dict[str, Any], market: str | None) -> bool:
    if not market:
        return False
    market_config = markets_config.get("markets", {}).get(market, {})
    return bool(
        isinstance(market_config, dict)
        and (
            market_config.get("priority")
            or market_config.get("is_priority")
            or market_config.get("priority_market")
        )
    )


def _add_reason(reasons: list[dict[str, Any]], points: int, reason: str) -> None:
    reasons.append({"points": points, "reason": reason})


def _top_reasons(reasons: list[dict[str, Any]], forced_reasons: list[str]) -> list[dict[str, Any]]:
    forced = [
        {"points": 0, "reason": reason, "forced_disqualification": True}
        for reason in forced_reasons
    ]
    scored = sorted(reasons, key=lambda item: abs(int(item["points"])), reverse=True)
    return (forced + scored)[:10]


def _metadata(prospect: dict[str, Any]) -> dict[str, Any]:
    loaded = _json_loads(prospect.get("metadata_json"), {})
    return loaded if isinstance(loaded, dict) else {}


def _franchise_override(prospect: dict[str, Any]) -> bool:
    return bool(_metadata(prospect).get("franchise_override"))


def _exclusion_match_label(exclusion: dict[str, Any]) -> str:
    return str(
        exclusion.get("matched_name")
        or exclusion.get("matched_domain")
        or exclusion.get("matched_regex")
        or "configured exclusion"
    )


def _score_prospect(
    prospect: dict[str, Any],
    *,
    scoring_config: dict[str, Any],
    markets_config: dict[str, Any],
) -> dict[str, Any]:
    preferred_niches = set(scoring_config.get("preferred_niches") or [])
    franchise_exclusion = check_franchise_exclusion(prospect)
    manual_override = _franchise_override(prospect)
    if franchise_exclusion.get("is_excluded"):
        franchise_exclusion = dict(franchise_exclusion)
        franchise_exclusion["manual_override"] = manual_override

    if franchise_exclusion.get("is_hard_exclude") and not manual_override:
        match_label = _exclusion_match_label(franchise_exclusion)
        reason = f"franchise/national-chain hard exclusion: {match_label}"
        return {
            "pre_audit_eligibility_score": 0,
            "qualification_status": "DISQUALIFIED",
            "status": "INELIGIBLE",
            "next_action": "DISCARD",
            "forced_disqualified": True,
            "forced_reasons": [reason],
            "threshold": QUALIFICATION_THRESHOLD,
            "top_reasons": _top_reasons([], [reason]),
            "franchise_exclusion": franchise_exclusion,
            "signals": {
                "business_status": str(prospect.get("business_status") or "").strip() or None,
                "has_website_url": _has_value(prospect.get("website_url")),
                "has_phone": _has_value(prospect.get("phone")),
                "rating": _parse_float(prospect.get("rating")),
                "user_rating_count": _parse_int(prospect.get("user_rating_count")),
                "preferred_niche": prospect.get("niche") in preferred_niches,
                "priority_market": _market_priority(markets_config, prospect.get("market")),
                "franchise_or_chain_detected": True,
                "franchise_or_chain_keyword": match_label,
                "franchise_exclusion": franchise_exclusion,
            },
            "algorithm_version": "phase1_pre_audit_eligibility_v2",
        }

    reasons: list[dict[str, Any]] = []
    forced_reasons: list[str] = []
    score = 0

    business_status = str(prospect.get("business_status") or "").strip()
    if business_status.upper() == "OPERATIONAL":
        score += 15
        _add_reason(reasons, 15, "business_status is OPERATIONAL")
    elif business_status:
        forced_reasons.append(f"business_status is {business_status}, not OPERATIONAL")

    if _has_value(prospect.get("website_url")):
        score += 15
        _add_reason(reasons, 15, "website_url present")
    else:
        forced_reasons.append("missing website_url")

    if _has_value(prospect.get("phone")):
        score += 10
        _add_reason(reasons, 10, "phone present")

    rating = _parse_float(prospect.get("rating"))
    if rating is not None and rating >= 4.5:
        score += 15
        _add_reason(reasons, 15, "rating >= 4.5")
    elif rating is not None and rating >= 4.0:
        score += 10
        _add_reason(reasons, 10, "rating 4.0-4.49")

    review_count = _parse_int(prospect.get("user_rating_count"))
    if review_count >= 100:
        score += 20
        _add_reason(reasons, 20, "100+ Google reviews")
    elif review_count >= 50:
        score += 15
        _add_reason(reasons, 15, "50-99 Google reviews")
    elif review_count >= 20:
        score += 10
        _add_reason(reasons, 10, "20-49 Google reviews")

    if prospect.get("niche") in preferred_niches:
        score += 10
        _add_reason(reasons, 10, "preferred Phase 1 niche")

    if _market_priority(markets_config, prospect.get("market")):
        score += 5
        _add_reason(reasons, 5, "priority market")

    if franchise_exclusion.get("is_soft_exclude"):
        penalty = int(franchise_exclusion.get("penalty") or 40)
        score -= penalty
        _add_reason(
            reasons,
            -penalty,
            f"soft franchise/national-chain exclusion: {_exclusion_match_label(franchise_exclusion)}",
        )
    elif franchise_exclusion.get("is_hard_exclude") and manual_override:
        _add_reason(
            reasons,
            0,
            f"franchise/national-chain exclusion manually overridden: {_exclusion_match_label(franchise_exclusion)}",
        )

    score = _clamp_score(score)
    forced_disqualified = bool(forced_reasons)
    qualification_status = (
        "QUALIFIED"
        if score >= QUALIFICATION_THRESHOLD and not forced_disqualified
        else "DISQUALIFIED"
    )
    status = "ELIGIBLE_FOR_AUDIT" if qualification_status == "QUALIFIED" else "INELIGIBLE"
    next_action = "RUN_AUDIT" if qualification_status == "QUALIFIED" else "DISCARD"
    top_reasons = _top_reasons(reasons, forced_reasons)

    return {
        "pre_audit_eligibility_score": score,
        "qualification_status": qualification_status,
        "status": status,
        "next_action": next_action,
        "forced_disqualified": forced_disqualified,
        "forced_reasons": forced_reasons,
        "threshold": QUALIFICATION_THRESHOLD,
        "top_reasons": top_reasons,
        "franchise_exclusion": franchise_exclusion,
        "signals": {
            "business_status": business_status or None,
            "has_website_url": _has_value(prospect.get("website_url")),
            "has_phone": _has_value(prospect.get("phone")),
            "rating": rating,
            "user_rating_count": review_count,
            "preferred_niche": prospect.get("niche") in preferred_niches,
            "priority_market": _market_priority(markets_config, prospect.get("market")),
            "franchise_or_chain_detected": bool(franchise_exclusion.get("is_excluded")),
            "franchise_or_chain_keyword": _exclusion_match_label(franchise_exclusion)
            if franchise_exclusion.get("is_excluded")
            else None,
            "franchise_exclusion": franchise_exclusion,
        },
        "algorithm_version": "phase1_pre_audit_eligibility_v2",
    }


def _select_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ELIGIBLE_SELECTION_STATUSES)
    protected = ",".join("?" for _ in PROTECTED_LIFECYCLE_STATUSES)
    clauses = [
        f"qualification_status IN ({placeholders})",
        f"(status IS NULL OR status NOT IN ({protected}))",
    ]
    params: list[Any] = list(ELIGIBLE_SELECTION_STATUSES)
    params.extend(PROTECTED_LIFECYCLE_STATUSES)

    if market:
        clauses.append("market = ?")
        params.append(market)
    if niche:
        clauses.append("niche = ?")
        params.append(niche)

    sql = f"SELECT * FROM prospects WHERE {' AND '.join(clauses)} ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def _merged_metadata_json(prospect: dict[str, Any], eligibility: dict[str, Any]) -> str:
    metadata = _json_loads(prospect.get("metadata_json"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["pre_audit_eligibility"] = eligibility
    metadata["franchise_exclusion"] = eligibility.get("franchise_exclusion") or {}
    return json.dumps(metadata, sort_keys=True)


def _needs_update(
    prospect: dict[str, Any],
    *,
    eligibility: dict[str, Any],
    metadata_json: str,
) -> bool:
    expected = {
        "business_eligibility_score": eligibility["pre_audit_eligibility_score"],
        "qualification_status": eligibility["qualification_status"],
        "status": eligibility["status"],
        "next_action": eligibility["next_action"],
        "metadata_json": metadata_json,
    }
    for key, value in expected.items():
        current = prospect.get(key)
        if key == "business_eligibility_score":
            current = _parse_int(current)
        if current != value:
            return True
    return False


def _update_prospect_eligibility(
    connection: Any,
    prospect: dict[str, Any],
    eligibility: dict[str, Any],
) -> bool:
    metadata_json = _merged_metadata_json(prospect, eligibility)
    if not _needs_update(prospect, eligibility=eligibility, metadata_json=metadata_json):
        return False

    connection.execute(
        """
        UPDATE prospects
        SET business_eligibility_score = ?,
            qualification_status = ?,
            status = ?,
            next_action = ?,
            metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            eligibility["pre_audit_eligibility_score"],
            eligibility["qualification_status"],
            eligibility["status"],
            eligibility["next_action"],
            metadata_json,
            db.utc_now(),
            prospect["id"],
        ),
    )
    return True


def _top_reasons_text(eligibility: dict[str, Any]) -> str:
    reasons = eligibility.get("top_reasons") or []
    values = []
    for item in reasons[:5]:
        if not isinstance(item, dict):
            continue
        points = int(item.get("points") or 0)
        suffix = f" ({points:+})" if points else ""
        values.append(f"{item.get('reason')}{suffix}")
    return " | ".join(values)


def _csv_row(prospect: dict[str, Any], eligibility: dict[str, Any]) -> dict[str, Any]:
    return {
        "prospect_id": prospect["id"],
        "business_name": prospect.get("business_name"),
        "market": prospect.get("market"),
        "niche": prospect.get("niche"),
        "website_url": prospect.get("website_url"),
        "phone": prospect.get("phone"),
        "rating": prospect.get("rating"),
        "user_rating_count": prospect.get("user_rating_count"),
        "pre_audit_eligibility_score": eligibility["pre_audit_eligibility_score"],
        "qualification_status": eligibility["qualification_status"],
        "status": eligibility["status"],
        "next_action": eligibility["next_action"],
        "forced_disqualified": eligibility["forced_disqualified"],
        "forced_reasons": " | ".join(eligibility["forced_reasons"]),
        "franchise_exclusion": eligibility.get("franchise_exclusion", {}).get("recommended_action"),
        "franchise_exclusion_reason": eligibility.get("franchise_exclusion", {}).get("reason"),
        "top_reasons": _top_reasons_text(eligibility),
    }


def _write_csv(rows: list[dict[str, Any]]) -> str:
    path = project_path(CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "prospect_id",
        "business_name",
        "market",
        "niche",
        "website_url",
        "phone",
        "rating",
        "user_rating_count",
        "pre_audit_eligibility_score",
        "qualification_status",
        "status",
        "next_action",
        "forced_disqualified",
        "forced_reasons",
        "franchise_exclusion",
        "franchise_exclusion_reason",
        "top_reasons",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def main() -> int:
    parser = build_parser("Calculate Phase 1 pre-audit prospect eligibility.")
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    scoring_config = load_yaml_config("scoring.yaml")
    markets_config = load_yaml_config("markets.yaml")
    connection = db.init_db(args.db_path)
    prospects = _select_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
    )

    rows: list[dict[str, Any]] = []
    qualified = 0
    disqualified = 0
    forced_disqualified = 0
    updated = 0

    for prospect in prospects:
        eligibility = _score_prospect(
            prospect,
            scoring_config=scoring_config,
            markets_config=markets_config,
        )
        qualified += int(eligibility["qualification_status"] == "QUALIFIED")
        disqualified += int(eligibility["qualification_status"] == "DISQUALIFIED")
        forced_disqualified += int(bool(eligibility["forced_disqualified"]))
        rows.append(_csv_row(prospect, eligibility))

        if args.dry_run:
            context.logger.info(
                "eligibility_would_score",
                extra={
                    "event": "eligibility_would_score",
                    "prospect_id": prospect["id"],
                    "business_name": prospect["business_name"],
                    "pre_audit_eligibility_score": eligibility[
                        "pre_audit_eligibility_score"
                    ],
                    "qualification_status": eligibility["qualification_status"],
                    "next_action": eligibility["next_action"],
                },
            )
            continue

        if _update_prospect_eligibility(connection, prospect, eligibility):
            updated += 1

    csv_path = None
    if not args.dry_run:
        csv_path = _write_csv(rows)
        connection.commit()

    connection.close()

    summary = {
        "processed": len(prospects),
        "qualified": qualified,
        "disqualified": disqualified,
        "forced_disqualified": forced_disqualified,
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(summary, sort_keys=True))
    finish_command(context, updated=updated, csv_path=csv_path, **summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
