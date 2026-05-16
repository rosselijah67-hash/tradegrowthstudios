"""Calculate Phase 1 lead scores for audited prospects."""

from __future__ import annotations

import csv
import json
import re
from typing import Any

from . import actor_context, db
from .cli_utils import build_parser, finish_command, setup_command
from .config import load_yaml_config, project_path
from .pagespeed import PAGESPEED_SUCCESS_STATUSES
from .state import (
    AuditDataStatus,
    HumanReviewDecision,
    HumanReviewStatus,
    NextAction,
    ProspectStatus,
)


COMMAND = "score_leads"
CSV_PATH = "runs/latest/top_leads.csv"
BUSINESS_ELIGIBILITY_MAX = 55
WEBSITE_PAIN_MAX = 45
FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
}
CONVERSION_PATH_RE = re.compile(
    r"\b(book|booking|schedule|appointment|calendar|estimate|quote|contact)\b",
    re.IGNORECASE,
)
PROTECTED_SCORING_STATUSES = (
    "NO_WEBSITE",
    "INELIGIBLE",
    "REJECTED_REVIEW",
    "DISCARDED",
    "CLOSED_WON",
    "CLOSED_LOST",
    "PROJECT_ACTIVE",
    "PROJECT_COMPLETE",
)
PROTECTED_SCORING_NEXT_ACTIONS = ("REJECTED_BY_REVIEW",)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _select_audited_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    prospect_id: int | None = None,
) -> list[dict[str, Any]]:
    blocked_statuses = ",".join("?" for _ in PROTECTED_SCORING_STATUSES)
    blocked_next_actions = ",".join("?" for _ in PROTECTED_SCORING_NEXT_ACTIONS)
    if prospect_id is not None:
        clauses = ["id = ?"]
        params: list[Any] = [prospect_id]
    else:
        clauses = [
            "EXISTS ("
            "SELECT 1 FROM website_audits "
            "WHERE website_audits.prospect_id = prospects.id "
            "AND website_audits.audit_type = 'site' "
            "AND website_audits.status = 'succeeded'"
            ")"
        ]
        params = []
        if market:
            clauses.append("market = ?")
            params.append(market)
        if niche:
            clauses.append("niche = ?")
            params.append(niche)
    clauses.append(f"(status IS NULL OR status NOT IN ({blocked_statuses}))")
    clauses.append(f"(next_action IS NULL OR next_action NOT IN ({blocked_next_actions}))")
    params.extend(PROTECTED_SCORING_STATUSES)
    params.extend(PROTECTED_SCORING_NEXT_ACTIONS)
    actor_context.append_actor_scope(clauses, params, "prospects")

    sql = f"SELECT * FROM prospects WHERE {' AND '.join(clauses)} ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def _audit(connection: Any, prospect_id: int, audit_type: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM website_audits
        WHERE prospect_id = ? AND audit_type = ?
        LIMIT 1
        """,
        (prospect_id, audit_type),
    ).fetchone()
    return db.row_to_dict(row) if row else None


def _findings(audit_row: dict[str, Any] | None) -> dict[str, Any]:
    if not audit_row:
        return {}
    parsed = _json_loads(audit_row.get("findings_json"), {})
    return parsed if isinstance(parsed, dict) else {}


def _screenshot_paths(connection: Any, prospect_id: int) -> dict[str, str | None]:
    rows = connection.execute(
        """
        SELECT artifact_type, path
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type IN ('screenshot_desktop', 'screenshot_mobile')
          AND status = 'ready'
        """,
        (prospect_id,),
    ).fetchall()
    paths = {"desktop": None, "mobile": None}
    for row in rows:
        if row["artifact_type"] == "screenshot_desktop":
            paths["desktop"] = row["path"]
        elif row["artifact_type"] == "screenshot_mobile":
            paths["mobile"] = row["path"]
    return paths


def _has_ready_screenshot(connection: Any, prospect_id: int) -> bool:
    paths = _screenshot_paths(connection, prospect_id)
    return bool(paths["desktop"] and paths["mobile"])


def _audit_status(audit_row: dict[str, Any] | None) -> str | None:
    return str(audit_row.get("status") or "") if audit_row else None


def _pagespeed_score(audit_row: dict[str, Any] | None) -> int | None:
    if not audit_row or _audit_status(audit_row) not in PAGESPEED_SUCCESS_STATUSES:
        return None
    score = audit_row.get("score")
    return int(score) if score is not None else None


def _pagespeed_source(audit_row: dict[str, Any] | None) -> str | None:
    findings = _findings(audit_row)
    source = str(findings.get("source") or "") or None
    if source is None and audit_row and _audit_status(audit_row) == "succeeded":
        return "pagespeed_insights"
    return source


def _audit_data_status(
    *,
    site_audit: dict[str, Any] | None,
    mobile_audit: dict[str, Any] | None,
    desktop_audit: dict[str, Any] | None,
    screenshot_ready: bool,
) -> str:
    if not site_audit or site_audit.get("status") != "succeeded":
        return "NEEDS_SITE_AUDIT"
    site_findings = _findings(site_audit)
    audit_mode = str(site_findings.get("audit_mode") or "deep").strip().lower()
    if (
        audit_mode != "fast"
        and (_pagespeed_score(mobile_audit) is None or _pagespeed_score(desktop_audit) is None)
    ):
        return "NEEDS_PAGESPEED"
    if not screenshot_ready:
        return "NEEDS_SCREENSHOTS"
    return "READY"


def _emails(site_findings: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("mailto_emails", "visible_emails"):
        raw_values = site_findings.get(key) or []
        if isinstance(raw_values, list):
            values.extend(str(value).strip() for value in raw_values)

    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        lowered = value.lower()
        if not lowered or lowered in seen or "@" not in lowered:
            continue
        seen.add(lowered)
        output.append(lowered)
    return output


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def _business_domain_emails(emails: list[str], prospect_domain: str | None) -> list[str]:
    if not prospect_domain:
        return []
    domain = prospect_domain.lower()
    matches: list[str] = []
    for email in emails:
        email_domain = _email_domain(email)
        if email_domain in FREE_EMAIL_DOMAINS:
            continue
        if email_domain == domain or email_domain.endswith(f".{domain}"):
            matches.append(email)
    return matches


def _text_matches_keyword(value: str, keyword: str) -> bool:
    haystack = re.sub(r"[^a-z0-9]+", " ", value.lower())
    needle = re.sub(r"[^a-z0-9]+", " ", keyword.lower()).strip()
    if needle == "ars":
        return f" {needle} " in f" {haystack} "
    return bool(needle and needle in haystack)


def _is_franchise(prospect: dict[str, Any], franchise_keywords: list[str]) -> bool:
    metadata = _json_loads(prospect.get("metadata_json"), {})
    reason = str(metadata.get("disqualification_reason", "") if isinstance(metadata, dict) else "")
    if reason.startswith("franchise:"):
        return True

    text = " ".join(
        str(value or "")
        for value in (
            prospect.get("business_name"),
            prospect.get("domain"),
            prospect.get("website_url"),
        )
    )
    return any(_text_matches_keyword(text, keyword) for keyword in franchise_keywords)


def _has_contact_or_about(site_findings: dict[str, Any]) -> bool:
    if site_findings.get("contact_page_links"):
        return True
    page_urls = site_findings.get("page_urls") or []
    return any(
        "contact" in str(url).lower() or "about" in str(url).lower()
        for url in page_urls
    )


def _has_service_pages(site_findings: dict[str, Any]) -> bool:
    if site_findings.get("service_page_links"):
        return True
    page_urls = site_findings.get("page_urls") or []
    return any("service" in str(url).lower() for url in page_urls)


def _has_forms(site_findings: dict[str, Any]) -> bool:
    forms = site_findings.get("forms") or []
    return isinstance(forms, list) and len(forms) > 0


def _link_has_conversion_intent(link: Any) -> bool:
    if isinstance(link, dict):
        haystack = f"{link.get('url') or ''} {link.get('text') or ''}"
    else:
        haystack = str(link)
    return CONVERSION_PATH_RE.search(haystack) is not None


def _has_conversion_path(site_findings: dict[str, Any]) -> bool:
    if _has_forms(site_findings):
        return True
    for key in ("booking_links", "contact_page_links"):
        links = site_findings.get(key) or []
        if isinstance(links, list) and any(_link_has_conversion_intent(link) for link in links):
            return True
    return False


def _has_analytics(site_findings: dict[str, Any]) -> bool:
    tracking = site_findings.get("tracking") or {}
    if not isinstance(tracking, dict):
        return False
    return bool(
        tracking.get("has_ga4_or_gtag")
        or tracking.get("has_gtm")
        or tracking.get("has_facebook_pixel")
    )


def _has_schema(site_findings: dict[str, Any]) -> bool:
    schema = site_findings.get("schema") or {}
    if not isinstance(schema, dict):
        return False
    return bool((schema.get("json_ld_count") or 0) > 0 or schema.get("types"))


def _technology(site_findings: dict[str, Any]) -> dict[str, Any]:
    technology = site_findings.get("technology") or {}
    if not isinstance(technology, dict):
        return {}
    return technology


def _has_legacy_builder(site_findings: dict[str, Any]) -> bool:
    technology = _technology(site_findings)
    return any(
        bool(technology.get(key))
        for key in ("divi", "beaver_builder", "wpbakery", "oxygen")
    )


def _has_locked_platform(site_findings: dict[str, Any]) -> bool:
    technology = _technology(site_findings)
    return any(bool(technology.get(key)) for key in ("wix", "squarespace"))


def _weak_title_meta(site_findings: dict[str, Any]) -> bool:
    title = str(site_findings.get("title") or "").strip()
    meta = str(site_findings.get("meta_description") or "").strip()
    return len(title) < 20 or len(meta) < 50


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


def _next_action(
    score: int,
    thresholds: dict[str, Any],
    *,
    eligibility_status: str = "ELIGIBLE",
    audit_data_status: str = "READY",
    human_review_decision: str | None = None,
) -> str:
    _ = thresholds
    if audit_data_status != AuditDataStatus.READY:
        return NextAction.RUN_AUDIT
    if eligibility_status != "ELIGIBLE":
        return NextAction.DISCARD

    decision = (human_review_decision or "").upper()
    if decision == HumanReviewDecision.APPROVED:
        return NextAction.APPROVED_FOR_OUTREACH
    if decision == HumanReviewDecision.REJECTED:
        return "REJECTED_BY_REVIEW"
    return NextAction.HUMAN_REVIEW


def _business_eligibility_status(
    business_eligibility: int,
    *,
    franchise: bool,
    contactability: int,
    data_availability: int,
    scoring_config: dict[str, Any],
) -> str:
    eligibility_config = scoring_config.get("eligibility") or {}
    minimum = int(eligibility_config.get("min_business_eligibility_score", 35))
    min_contactability = int(eligibility_config.get("min_contactability_score", 2))
    min_data_availability = int(eligibility_config.get("min_data_availability_score", 3))
    if franchise:
        return "INELIGIBLE_FRANCHISE"
    if business_eligibility < minimum:
        return "LOW_BUSINESS_ELIGIBILITY"
    if contactability < min_contactability:
        return "LOW_CONTACTABILITY"
    if data_availability < min_data_availability:
        return "LOW_DATA_AVAILABILITY"
    return "ELIGIBLE"


def _human_review_status(
    *,
    audit_data_status: str,
    eligibility_status: str,
    human_review_decision: str | None,
) -> str:
    decision = (human_review_decision or "").upper()
    if decision in {"APPROVED", "REJECTED"}:
        return decision
    if audit_data_status == AuditDataStatus.READY and eligibility_status == "ELIGIBLE":
        return HumanReviewStatus.PENDING
    return HumanReviewStatus.NOT_READY


def _expected_close_score(website_pain: int, eligibility_status: str) -> int:
    if eligibility_status != "ELIGIBLE":
        return 0
    return _clamp(round((website_pain / WEBSITE_PAIN_MAX) * 100), 0, 100)


def _add_reason(reasons: list[dict[str, Any]], category: str, points: int, reason: str) -> None:
    reasons.append({"category": category, "points": points, "reason": reason})


def _score_prospect(
    connection: Any,
    prospect: dict[str, Any],
    *,
    scoring_config: dict[str, Any],
    markets_config: dict[str, Any],
) -> dict[str, Any]:
    preferred_niches = set(scoring_config.get("preferred_niches") or [])
    franchise_keywords = scoring_config.get("franchise_keywords") or []
    thresholds = scoring_config.get("next_actions") or {}

    site_audit = _audit(connection, prospect["id"], "site")
    site_findings = _findings(site_audit)
    mobile_audit = _audit(connection, prospect["id"], "pagespeed_mobile")
    desktop_audit = _audit(connection, prospect["id"], "pagespeed_desktop")
    mobile_score = _pagespeed_score(mobile_audit)
    desktop_score = _pagespeed_score(desktop_audit)
    screenshot_paths = _screenshot_paths(connection, prospect["id"])
    screenshot_ready = _has_ready_screenshot(connection, prospect["id"])
    audit_data_status = _audit_data_status(
        site_audit=site_audit,
        mobile_audit=mobile_audit,
        desktop_audit=desktop_audit,
        screenshot_ready=screenshot_ready,
    )
    emails = _emails(site_findings)
    business_emails = _business_domain_emails(emails, prospect.get("domain"))
    reasons: list[dict[str, Any]] = []

    business_viability = 0
    rating = prospect.get("rating")
    if rating is not None and float(rating) >= 4.5:
        business_viability += 7
        _add_reason(reasons, "business_viability", 7, "rating >= 4.5")
    elif rating is not None and 4.0 <= float(rating) < 4.5:
        business_viability += 4
        _add_reason(reasons, "business_viability", 4, "rating 4.0-4.49")

    review_count = int(prospect.get("user_rating_count") or 0)
    if review_count >= 100:
        business_viability += 8
        _add_reason(reasons, "business_viability", 8, "100+ Google reviews")
    elif review_count >= 50:
        business_viability += 6
        _add_reason(reasons, "business_viability", 6, "50-99 Google reviews")
    elif review_count >= 20:
        business_viability += 3
        _add_reason(reasons, "business_viability", 3, "20-49 Google reviews")

    if prospect.get("niche") in preferred_niches:
        business_viability += 5
        _add_reason(reasons, "business_viability", 5, "preferred Phase 1 niche")

    franchise = _is_franchise(prospect, franchise_keywords)
    if franchise:
        business_viability -= 15
        _add_reason(reasons, "business_viability", -15, "franchise or chain detected")
    else:
        business_viability += 5
        _add_reason(reasons, "business_viability", 5, "local/non-franchise")
    business_viability = _clamp(business_viability, 0, 25)

    website_pain = 0
    if mobile_score is not None and int(mobile_score) < 50:
        website_pain += 8
        _add_reason(reasons, "website_pain", 8, "mobile PageSpeed score < 50")
    if desktop_score is not None and int(desktop_score) < 60:
        website_pain += 4
        _add_reason(reasons, "website_pain", 4, "desktop PageSpeed score < 60")
    if not site_findings.get("tel_links"):
        website_pain += 8
        _add_reason(reasons, "website_pain", 8, "no tel link found")
    if not _has_conversion_path(site_findings):
        website_pain += 5
        _add_reason(reasons, "website_pain", 5, "no clear quote/contact path found")
    if not _has_service_pages(site_findings):
        website_pain += 6
        _add_reason(reasons, "website_pain", 6, "no obvious service pages found")
    if not _has_analytics(site_findings):
        website_pain += 3
        _add_reason(reasons, "website_pain", 3, "no analytics/GTM/GA4 detected")
    if not _has_schema(site_findings):
        website_pain += 3
        _add_reason(reasons, "website_pain", 3, "no schema detected")
    if _has_legacy_builder(site_findings):
        website_pain += 5
        _add_reason(
            reasons,
            "website_pain",
            5,
            "legacy page builder detected",
        )
    elif _has_locked_platform(site_findings):
        website_pain += 3
        _add_reason(reasons, "website_pain", 3, "locked website platform detected")
    if _weak_title_meta(site_findings):
        website_pain += 3
        _add_reason(reasons, "website_pain", 3, "weak title/meta")
    website_pain = _clamp(website_pain, 0, WEBSITE_PAIN_MAX)

    contactability = 0
    if emails:
        contactability += 8
        _add_reason(reasons, "contactability", 8, "email found on website")
    if business_emails:
        contactability += 5
        _add_reason(reasons, "contactability", 5, "business-domain email found")
    if prospect.get("phone"):
        contactability += 2
        _add_reason(reasons, "contactability", 2, "phone present")
    contactability = _clamp(contactability, 0, 15)

    data_availability = 0
    if site_audit and site_audit.get("status") == "succeeded":
        data_availability += 3
        _add_reason(reasons, "data_availability", 3, "website accessible")
    if _has_service_pages(site_findings):
        data_availability += 3
        _add_reason(reasons, "data_availability", 3, "service pages found")
    if prospect.get("phone") and (prospect.get("formatted_address") or prospect.get("address")):
        data_availability += 2
        _add_reason(reasons, "data_availability", 2, "phone and address available")
    if _has_contact_or_about(site_findings):
        data_availability += 2
        _add_reason(reasons, "data_availability", 2, "contact/about page found")
    data_availability = _clamp(data_availability, 0, 10)

    market_fit = 0
    if _market_priority(markets_config, prospect.get("market")):
        market_fit = 5
        _add_reason(reasons, "market_fit", 5, "priority market")

    business_eligibility = _clamp(
        business_viability + contactability + data_availability + market_fit,
        0,
        BUSINESS_ELIGIBILITY_MAX,
    )
    eligibility_status = _business_eligibility_status(
        business_eligibility,
        franchise=franchise,
        contactability=contactability,
        data_availability=data_availability,
        scoring_config=scoring_config,
    )
    expected_close_score = _expected_close_score(website_pain, eligibility_status)
    human_review_status = _human_review_status(
        audit_data_status=audit_data_status,
        eligibility_status=eligibility_status,
        human_review_decision=prospect.get("human_review_decision"),
    )
    next_action = _next_action(
        expected_close_score,
        thresholds,
        eligibility_status=eligibility_status,
        audit_data_status=audit_data_status,
        human_review_decision=prospect.get("human_review_decision"),
    )
    status_update = None
    if audit_data_status == AuditDataStatus.READY and eligibility_status != "ELIGIBLE":
        status_update = ProspectStatus.INELIGIBLE
    elif (
        audit_data_status == AuditDataStatus.READY
        and human_review_status == HumanReviewStatus.PENDING
    ):
        status_update = ProspectStatus.PENDING_REVIEW
    reasons_sorted = sorted(reasons, key=lambda item: item["points"], reverse=True)
    explanation = {
        "business_viability_score": business_viability,
        "business_eligibility_score": business_eligibility,
        "business_eligibility_status": eligibility_status,
        "audit_mode": str(site_findings.get("audit_mode") or "deep").strip().lower(),
        "audit_data_status": audit_data_status,
        "website_pain_score": website_pain,
        "contactability_score": contactability,
        "data_availability_score": data_availability,
        "market_fit_score": market_fit,
        "expected_close_score": expected_close_score,
        "next_action": next_action,
        "status": status_update,
        "human_review_status": human_review_status,
        "top_reasons": reasons_sorted[:10],
        "signals": {
            "mobile_pagespeed_score": mobile_score,
            "desktop_pagespeed_score": desktop_score,
            "mobile_pagespeed_status": _audit_status(mobile_audit),
            "desktop_pagespeed_status": _audit_status(desktop_audit),
            "mobile_pagespeed_source": _pagespeed_source(mobile_audit),
            "desktop_pagespeed_source": _pagespeed_source(desktop_audit),
            "email_candidates": emails,
            "business_domain_emails": business_emails,
            "desktop_screenshot_path": screenshot_paths["desktop"],
            "mobile_screenshot_path": screenshot_paths["mobile"],
            "franchise_detected": franchise,
            "screenshot_ready": screenshot_ready,
        },
    }
    return explanation


def _update_prospect_score(connection: Any, prospect_id: int, explanation: dict[str, Any]) -> None:
    connection.execute(
        """
        UPDATE prospects
        SET business_viability_score = ?,
            business_eligibility_score = ?,
            website_pain_score = ?,
            contactability_score = ?,
            data_availability_score = ?,
            market_fit_score = ?,
            expected_close_score = ?,
            score_explanation_json = ?,
            audit_data_status = ?,
            human_review_status = ?,
            next_action = ?,
            status = COALESCE(?, status),
            updated_at = ?
        WHERE id = ?
        """,
        (
            explanation["business_viability_score"],
            explanation["business_eligibility_score"],
            explanation["website_pain_score"],
            explanation["contactability_score"],
            explanation["data_availability_score"],
            explanation["market_fit_score"],
            explanation["expected_close_score"],
            json.dumps(explanation, sort_keys=True),
            explanation["audit_data_status"],
            explanation["human_review_status"],
            explanation["next_action"],
            explanation.get("status"),
            db.utc_now(),
            prospect_id,
        ),
    )


def _top_5_reasons(explanation: dict[str, Any]) -> str:
    reasons = explanation.get("top_reasons") or []
    return " | ".join(
        f"{item['reason']} ({item['points']:+})"
        for item in reasons[:5]
    )


def _top_reasons_by_category(explanation: dict[str, Any], categories: set[str]) -> str:
    reasons = explanation.get("top_reasons") or []
    filtered = [
        item for item in reasons
        if isinstance(item, dict) and item.get("category") in categories
    ]
    return " | ".join(
        f"{item['reason']} ({item['points']:+})"
        for item in filtered[:5]
    )


def _csv_row(
    connection: Any,
    prospect: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, Any]:
    screenshots = _screenshot_paths(connection, prospect["id"])
    email_candidates = explanation.get("signals", {}).get("email_candidates") or []
    return {
        "prospect_id": prospect["id"],
        "business_name": prospect.get("business_name"),
        "niche": prospect.get("niche"),
        "market": prospect.get("market"),
        "website_url": prospect.get("website_url"),
        "phone": prospect.get("phone"),
        "email_candidates": "; ".join(email_candidates),
        "business_eligibility_score": explanation["business_eligibility_score"],
        "business_eligibility_status": explanation["business_eligibility_status"],
        "audit_data_status": explanation["audit_data_status"],
        "human_review_status": explanation["human_review_status"],
        "human_review_decision": prospect.get("human_review_decision"),
        "website_pain_score": explanation["website_pain_score"],
        "expected_close_score": explanation["expected_close_score"],
        "next_action": explanation["next_action"],
        "top_5_website_pain_reasons": _top_reasons_by_category(explanation, {"website_pain"}),
        "top_5_business_eligibility_reasons": _top_reasons_by_category(
            explanation,
            {"business_viability", "contactability", "data_availability", "market_fit"},
        ),
        "top_5_reasons": _top_5_reasons(explanation),
        "desktop_screenshot_path": screenshots["desktop"],
        "mobile_screenshot_path": screenshots["mobile"],
    }


def _write_csv(rows: list[dict[str, Any]]) -> str:
    path = project_path(CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "prospect_id",
        "business_name",
        "niche",
        "market",
        "website_url",
        "phone",
        "email_candidates",
        "business_eligibility_score",
        "business_eligibility_status",
        "audit_data_status",
        "human_review_status",
        "human_review_decision",
        "website_pain_score",
        "expected_close_score",
        "next_action",
        "top_5_website_pain_reasons",
        "top_5_business_eligibility_reasons",
        "top_5_reasons",
        "desktop_screenshot_path",
        "mobile_screenshot_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def main() -> int:
    parser = build_parser("Calculate Phase 1 lead scores for audited prospects.")
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Score one specific prospect, ignoring market/niche/audited selection filters.",
    )
    args = parser.parse_args()
    context = setup_command(args, COMMAND)
    try:
        actor_context.validate_actor_market_access(args.market, allow_global_scope=True)
    except actor_context.ActorAccessError as exc:
        raise SystemExit(str(exc)) from exc

    scoring_config = load_yaml_config("scoring.yaml")
    markets_config = load_yaml_config("markets.yaml")
    connection = db.init_db(args.db_path)
    prospects = _select_audited_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        prospect_id=args.prospect_id,
    )

    scored_rows: list[dict[str, Any]] = []
    for prospect in prospects:
        explanation = _score_prospect(
            connection,
            prospect,
            scoring_config=scoring_config,
            markets_config=markets_config,
        )
        if args.dry_run:
            context.logger.info(
                "lead_would_score",
                extra={
                    "event": "lead_would_score",
                    "prospect_id": prospect["id"],
                    "business_name": prospect["business_name"],
                    "expected_close_score": explanation["expected_close_score"],
                    "next_action": explanation["next_action"],
                },
            )
        else:
            _update_prospect_score(connection, prospect["id"], explanation)
            db.upsert_audit(
                connection,
                prospect_id=prospect["id"],
                audit_type="lead_score",
                status="scored",
                score=explanation["expected_close_score"],
                summary=f"Lead score: {explanation['expected_close_score']} ({explanation['next_action']})",
                findings=explanation,
                raw={},
                audited_at=db.utc_now(),
            )
        scored_rows.append(_csv_row(connection, prospect, explanation))

    csv_path = None
    if not args.dry_run:
        scored_rows.sort(
            key=lambda row: (
                row["audit_data_status"] == "READY",
                int(row["expected_close_score"]),
                int(row["business_eligibility_score"]),
            ),
            reverse=True,
        )
        csv_path = _write_csv(scored_rows)
        connection.commit()

    connection.close()
    finish_command(
        context,
        selected=len(prospects),
        scored=len(scored_rows),
        csv_path=csv_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
