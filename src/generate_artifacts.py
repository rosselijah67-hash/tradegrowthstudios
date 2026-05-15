"""Generate static sales artifacts for scored leads."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "generate_artifacts"
SUMMARY_CSV_PATH = "runs/latest/artifact_summary.csv"
OUTPUT_ROOT = "runs/latest/artifacts"
AUDIT_ACTIONS = {
    "HUMAN_REVIEW",
    "APPROVED_FOR_OUTREACH",
    "AUDIT_ARTIFACT",
    "HOMEPAGE_PREVIEW",
    "PRIORITY_OUTREACH",
}
PREVIEW_ACTIONS = {"APPROVED_FOR_OUTREACH", "HOMEPAGE_PREVIEW", "PRIORITY_OUTREACH"}


REASON_COPY = {
    "mobile PageSpeed score < 50": (
        "Mobile performance may be costing calls from visitors on their phones.",
        "Improve mobile load speed, image weight, and above-the-fold layout.",
    ),
    "desktop PageSpeed score < 60": (
        "Desktop performance has room to improve.",
        "Trim unnecessary scripts and optimize page assets.",
    ),
    "no tel link found": (
        "Mobile visitors may not have a one-tap call path.",
        "Add a prominent tap-to-call button in the header and key service sections.",
    ),
    "no contact form found": (
        "The audit did not verify an embedded quote request form.",
        "Confirm the visible quote CTA opens a working form; if not, add a short estimate form.",
    ),
    "no clear quote/contact path found": (
        "Visitors may not have a clear quote or contact path.",
        "Add a prominent quote/contact CTA that leads directly to a working form or booking flow.",
    ),
    "no obvious service pages found": (
        "Service offerings are not easy to scan from the current site structure.",
        "Create clear service sections and pages for the highest-value jobs.",
    ),
    "no analytics/GTM/GA4 detected": (
        "Tracking was not obvious in the site audit.",
        "Add measurement so calls, form submissions, and high-intent pages can be reviewed.",
    ),
    "no schema detected": (
        "Structured business/service markup was not detected.",
        "Add basic local business schema after verifying business details.",
    ),
    "legacy page builder detected": (
        "The site appears to use an older page-builder stack.",
        "Rebuild key pages with a cleaner, faster WordPress structure.",
    ),
    "locked website platform detected": (
        "The site appears to be on a locked hosted platform.",
        "Move the offer and lead-capture flow into a more flexible WordPress setup.",
    ),
    "weak title/meta": (
        "Search snippets may not clearly sell the service or location.",
        "Rewrite title and meta messaging around the main service, city, and call action.",
    ),
    "screenshot ready for visual review": (
        "The current site is ready for a visual conversion review.",
        "Use the desktop and mobile screenshots to mark layout, CTA, and trust gaps.",
    ),
}

NICHE_SERVICES = {
    "roofing": ["Primary Roofing Page", "Repair Request Flow", "Replacement Estimate Flow"],
    "hvac": ["Primary HVAC Page", "Service Request Flow", "Replacement Estimate Flow"],
    "plumbing": ["Primary Plumbing Page", "Repair Request Flow", "Project Estimate Flow"],
    "electrical": ["Primary Electrical Page", "Service Request Flow", "Project Estimate Flow"],
    "garage_doors": ["Primary Garage Door Page", "Repair Request Flow", "Replacement Estimate Flow"],
}


def build_arg_parser():
    parser = build_parser("Generate static sales artifacts for scored leads.")
    parser.add_argument(
        "--score-min",
        type=int,
        default=0,
        help="Minimum expected_close_score/review priority to generate artifacts.",
    )
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Generate artifacts for one specific prospect, ignoring score/action filters.",
    )
    return parser


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _load_templates():
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before generating artifacts.") from exc

    env = Environment(
        loader=FileSystemLoader(str(project_path("templates"))),
        autoescape=select_autoescape(("html", "xml", "j2")),
    )
    return {
        "audit_card": env.get_template("audit_card.html.j2"),
        "preview_homepage": env.get_template("preview_homepage.html.j2"),
    }


def _select_candidates(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    score_min: int,
) -> list[dict[str, Any]]:
    clauses = [
        f"next_action IN ({','.join('?' for _ in sorted(AUDIT_ACTIONS))})",
        "audit_data_status = 'READY'",
        "expected_close_score >= ?",
    ]
    params: list[Any] = sorted(AUDIT_ACTIONS) + [score_min]

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


def _select_candidate_by_id(connection: Any, prospect_id: int) -> list[dict[str, Any]]:
    row = connection.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    return [db.row_to_dict(row)] if row else []


def _artifact_block_reason(connection: Any, prospect: dict[str, Any]) -> str | None:
    if prospect.get("audit_data_status") != "READY":
        return f"audit_data_status={prospect.get('audit_data_status') or 'UNKNOWN'}"
    scores = _pagespeed_scores(connection, prospect, _score_explanation(prospect))
    if scores["mobile"] is None or scores["desktop"] is None:
        return "missing_pagespeed_scores"
    return None


def _audit(connection: Any, prospect_id: int, audit_type: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM website_audits
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


def _relative_asset(path: str | None, output_file: Path) -> str | None:
    if not path:
        return None
    absolute = project_path(path)
    return os.path.relpath(absolute, output_file.parent).replace("\\", "/")


def _artifact_url(path: str) -> str | None:
    base_url = os.environ.get("ARTIFACT_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    normalized_path = path.replace("\\", "/").lstrip("/")
    return f"{base_url}/{normalized_path}"


def _phone_display(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone


def _phone_tel(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"+1{digits}" if len(digits) == 10 else digits


def _niche_label(niche: str | None) -> str:
    if not niche:
        return "Home Service"
    return niche.replace("_", " ").title()


def _city(prospect: dict[str, Any]) -> str:
    return (
        prospect.get("city_guess")
        or prospect.get("city")
        or prospect.get("market")
        or "Local"
    )


def _next_action_short(next_action: str | None) -> str:
    if not next_action:
        return "Review"
    return next_action.replace("_", " ").title()


def _score_explanation(prospect: dict[str, Any]) -> dict[str, Any]:
    parsed = _json_loads(prospect.get("score_explanation_json"), {})
    return parsed if isinstance(parsed, dict) else {}


def _pagespeed_scores(connection: Any, prospect: dict[str, Any], explanation: dict[str, Any]) -> dict[str, Any]:
    signals = explanation.get("signals") or {}
    mobile = signals.get("mobile_pagespeed_score")
    desktop = signals.get("desktop_pagespeed_score")
    mobile_source = signals.get("mobile_pagespeed_source")
    desktop_source = signals.get("desktop_pagespeed_source")
    if mobile is None:
        mobile_audit = _audit(connection, prospect["id"], "pagespeed_mobile")
        mobile = mobile_audit.get("score") if mobile_audit else None
        mobile_source = (_findings(mobile_audit).get("source") if mobile_audit else None)
        if mobile_source is None and mobile_audit and mobile_audit.get("status") == "succeeded":
            mobile_source = "pagespeed_insights"
    elif mobile_source is None:
        mobile_audit = _audit(connection, prospect["id"], "pagespeed_mobile")
        mobile_source = (_findings(mobile_audit).get("source") if mobile_audit else None)
        if mobile_source is None and mobile_audit and mobile_audit.get("status") == "succeeded":
            mobile_source = "pagespeed_insights"
    if desktop is None:
        desktop_audit = _audit(connection, prospect["id"], "pagespeed_desktop")
        desktop = desktop_audit.get("score") if desktop_audit else None
        desktop_source = (_findings(desktop_audit).get("source") if desktop_audit else None)
        if desktop_source is None and desktop_audit and desktop_audit.get("status") == "succeeded":
            desktop_source = "pagespeed_insights"
    elif desktop_source is None:
        desktop_audit = _audit(connection, prospect["id"], "pagespeed_desktop")
        desktop_source = (_findings(desktop_audit).get("source") if desktop_audit else None)
        if desktop_source is None and desktop_audit and desktop_audit.get("status") == "succeeded":
            desktop_source = "pagespeed_insights"
    return {
        "mobile": mobile,
        "desktop": desktop,
        "mobile_source": _speed_source_label(mobile_source),
        "desktop_source": _speed_source_label(desktop_source),
    }


def _speed_source_label(source: Any) -> str:
    if source == "pagespeed_insights":
        return "PageSpeed Insights"
    if source == "local_speed_probe":
        return "Local Speed Fallback"
    return "Unknown Speed Source"


def _issue_and_improvement(reason: str) -> tuple[str, str]:
    return REASON_COPY.get(
        reason,
        (
            reason[0].upper() + reason[1:] if reason else "Website review item",
            "Review this item manually before using it in outreach.",
        ),
    )


def _top_issue_data(explanation: dict[str, Any]) -> tuple[list[str], list[str]]:
    reasons = explanation.get("top_reasons") or []
    website_reasons = [
        reason for reason in reasons
        if isinstance(reason, dict) and reason.get("category") == "website_pain"
    ]
    issue_text: list[str] = []
    improvement_text: list[str] = []
    for reason in website_reasons[:5]:
        issue, improvement = _issue_and_improvement(str(reason.get("reason") or ""))
        issue_text.append(issue)
        improvement_text.append(improvement)

    if not issue_text:
        issue_text.append("No deterministic website pain was found.")
        improvement_text.append("Complete human visual review before approving this lead for outreach.")
    return issue_text[:5], improvement_text[:5]


def _sales_angle(issues: list[str]) -> str:
    if not issues:
        return "Focus on making the website easier to understand and act on from mobile search traffic."
    return issues[0]


def _service_items(niche: str | None) -> list[str]:
    return NICHE_SERVICES.get(niche or "", ["Primary Service Page", "Request Flow", "Estimate Flow"])


def _template_context(
    connection: Any,
    prospect: dict[str, Any],
    output_file: Path,
) -> dict[str, Any]:
    explanation = _score_explanation(prospect)
    screenshots = _screenshot_paths(connection, prospect["id"])
    scores = _pagespeed_scores(connection, prospect, explanation)
    issues, improvements = _top_issue_data(explanation)
    city = _city(prospect)
    niche_label = _niche_label(prospect.get("niche"))
    phone_display = _phone_display(prospect.get("phone"))
    phone_tel = _phone_tel(prospect.get("phone"))
    business_name = prospect.get("business_name") or "Local Business"

    return {
        "business_name": business_name,
        "niche_label": niche_label,
        "city": city,
        "market": prospect.get("market"),
        "website_url": prospect.get("website_url"),
        "phone_display": phone_display,
        "phone_tel": phone_tel,
        "expected_close_score": prospect.get("expected_close_score") or 0,
        "next_action": prospect.get("next_action") or "REVIEW",
        "next_action_short": _next_action_short(prospect.get("next_action")),
        "mobile_pagespeed_score": scores["mobile"],
        "desktop_pagespeed_score": scores["desktop"],
        "mobile_pagespeed_source": scores["mobile_source"],
        "desktop_pagespeed_source": scores["desktop_source"],
        "desktop_screenshot_src": _relative_asset(screenshots["desktop"], output_file),
        "mobile_screenshot_src": _relative_asset(screenshots["mobile"], output_file),
        "top_issues": issues,
        "recommended_improvements": improvements,
        "sales_angle": _sales_angle(issues),
        "service_items": _service_items(prospect.get("niche")),
        "hero_headline": f"{city} {niche_label} Services",
        "hero_subhead": (
            "A clear homepage concept focused on calls, estimate requests, "
            "and fast mobile decision-making."
        ),
        "service_area_text": (
            f"Concept section for verified service areas around {city}. "
            "Specific cities should be confirmed before publishing."
        ),
    }


def _render_to_file(template: Any, context: dict[str, Any], output_path: Path) -> str:
    html = template.render(**context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or output_path.read_text(encoding="utf-8") != html:
        output_path.write_text(html, encoding="utf-8")
    return html


def _store_artifact(
    connection: Any,
    *,
    prospect: dict[str, Any],
    artifact_type: str,
    artifact_key: str,
    relative_path: str,
    html: str,
    metadata: dict[str, Any],
) -> int:
    artifact_url = _artifact_url(relative_path)
    return db.upsert_artifact(
        connection,
        artifact_key=artifact_key,
        prospect_id=prospect["id"],
        artifact_type=artifact_type,
        path=relative_path,
        artifact_url=artifact_url,
        content_hash=db.stable_hash(html),
        status="ready",
        metadata={**metadata, "artifact_url": artifact_url},
    )


def _write_summary(rows: list[dict[str, Any]]) -> str:
    path = project_path(SUMMARY_CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "prospect_id",
        "business_name",
        "next_action",
        "expected_close_score",
        "audit_card_path",
        "audit_card_url",
        "homepage_preview_path",
        "homepage_preview_url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def _generate_for_prospect(
    connection: Any,
    templates: dict[str, Any],
    prospect: dict[str, Any],
) -> dict[str, Any]:
    prospect_dir = f"{OUTPUT_ROOT}/{prospect['id']}"
    audit_relative_path = f"{prospect_dir}/audit_card.html"
    preview_relative_path = f"{prospect_dir}/preview_homepage.html"
    audit_output = project_path(audit_relative_path)
    preview_output = project_path(preview_relative_path)

    summary = {
        "prospect_id": prospect["id"],
        "business_name": prospect.get("business_name"),
        "next_action": prospect.get("next_action"),
        "expected_close_score": prospect.get("expected_close_score"),
        "audit_card_path": "",
        "audit_card_url": "",
        "homepage_preview_path": "",
        "homepage_preview_url": "",
    }

    context = _template_context(connection, prospect, audit_output)
    audit_html = _render_to_file(templates["audit_card"], context, audit_output)
    _store_artifact(
        connection,
        prospect=prospect,
        artifact_type="audit_card",
        artifact_key=f"{prospect['id']}:audit_card",
        relative_path=audit_relative_path,
        html=audit_html,
        metadata={"next_action": prospect.get("next_action")},
    )
    summary["audit_card_path"] = audit_relative_path
    summary["audit_card_url"] = _artifact_url(audit_relative_path) or ""

    if prospect.get("next_action") in PREVIEW_ACTIONS:
        preview_context = _template_context(connection, prospect, preview_output)
        preview_html = _render_to_file(templates["preview_homepage"], preview_context, preview_output)
        _store_artifact(
            connection,
            prospect=prospect,
            artifact_type="homepage_preview",
            artifact_key=f"{prospect['id']}:homepage_preview",
            relative_path=preview_relative_path,
            html=preview_html,
            metadata={"next_action": prospect.get("next_action")},
        )
        summary["homepage_preview_path"] = preview_relative_path
        summary["homepage_preview_url"] = _artifact_url(preview_relative_path) or ""

    return summary


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    connection = db.init_db(args.db_path)
    if args.prospect_id is not None:
        prospects = _select_candidate_by_id(connection, args.prospect_id)
    else:
        prospects = _select_candidates(
            connection,
            market=args.market,
            niche=args.niche,
            limit=args.limit,
            score_min=args.score_min,
        )

    generated = 0
    summary_rows: list[dict[str, Any]] = []
    summary_csv_path: str | None = None

    if args.dry_run:
        for prospect in prospects:
            block_reason = _artifact_block_reason(connection, prospect)
            if block_reason:
                context.logger.warning(
                    "artifact_skipped_incomplete_data",
                    extra={
                        "event": "artifact_skipped_incomplete_data",
                        "prospect_id": prospect["id"],
                        "business_name": prospect.get("business_name"),
                        "reason": block_reason,
                    },
                )
                continue
            artifact_count = 1 + int(prospect.get("next_action") in PREVIEW_ACTIONS)
            generated += artifact_count
            context.logger.info(
                "artifacts_would_generate",
                extra={
                    "event": "artifacts_would_generate",
                    "prospect_id": prospect["id"],
                    "business_name": prospect.get("business_name"),
                    "next_action": prospect.get("next_action"),
                    "expected_close_score": prospect.get("expected_close_score"),
                    "artifact_count": artifact_count,
                },
            )
    else:
        templates = _load_templates()
        for prospect in prospects:
            block_reason = _artifact_block_reason(connection, prospect)
            if block_reason:
                context.logger.warning(
                    "artifact_skipped_incomplete_data",
                    extra={
                        "event": "artifact_skipped_incomplete_data",
                        "prospect_id": prospect["id"],
                        "business_name": prospect.get("business_name"),
                        "reason": block_reason,
                    },
                )
                continue
            summary = _generate_for_prospect(connection, templates, prospect)
            summary_rows.append(summary)
            generated += 1 + int(bool(summary["homepage_preview_path"]))
            context.logger.info(
                "artifacts_generated",
                extra={
                    "event": "artifacts_generated",
                    "prospect_id": prospect["id"],
                    "audit_card_path": summary["audit_card_path"],
                    "homepage_preview_path": summary["homepage_preview_path"],
                },
            )
        summary_csv_path = _write_summary(summary_rows)
        connection.commit()

    connection.close()
    finish_command(
        context,
        selected=len(prospects),
        generated=generated,
        summary_csv_path=None if args.dry_run else summary_csv_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
