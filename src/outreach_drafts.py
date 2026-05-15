"""Generate deterministic outreach email drafts from stored audit evidence."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import db, outreach_copy
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "outreach_drafts"
OUTPUT_ROOT = "runs/latest/outreach_drafts"
DRAFT_STEPS = (1, 2, 3, 4)
CONVERSION_PATH_RE = re.compile(
    r"\b(book|booking|schedule|appointment|calendar|estimate|quote|contact)\b",
    re.IGNORECASE,
)
SUCCESS_PAGESPEED_STATUSES = {"succeeded", "fallback_succeeded"}
DEFAULT_SENDER_NAME = "Local Growth Audit"


VISUAL_CATEGORY_ORDER = [
    "mobile_layout",
    "hero_section",
    "call_to_action",
    "header_navigation",
    "visual_clutter",
    "readability",
    "design_age",
    "form_or_booking_path",
    "service_clarity",
    "trust_signals",
    "content_depth",
    "seo_structure",
    "performance_perception",
    "layout_consistency",
    "conversion_path",
]

VISUAL_THEME_MAP = {
    "mobile_layout": ("mobile", "cta", "buyer_path"),
    "hero_section": ("buyer_path", "first_impression"),
    "call_to_action": ("cta", "buyer_path"),
    "header_navigation": ("cta", "buyer_path"),
    "visual_clutter": ("first_impression", "buyer_path"),
    "readability": ("first_impression", "buyer_path"),
    "design_age": ("first_impression",),
    "form_or_booking_path": ("cta", "buyer_path"),
    "service_clarity": ("service", "buyer_path"),
    "trust_signals": ("trust", "buyer_path"),
    "content_depth": ("service", "buyer_path"),
    "seo_structure": ("service", "local_seo"),
    "performance_perception": ("mobile", "pagespeed", "first_impression"),
    "layout_consistency": ("first_impression",),
    "conversion_path": ("cta", "buyer_path"),
}

VISUAL_CLAIM_COPY = {
    "mobile_layout": (
        "The mobile layout creates clear friction for visitors trying to evaluate or "
        "contact the business."
    ),
    "hero_section": (
        "The first screen does not establish the service, location, and next action as "
        "quickly as it could."
    ),
    "call_to_action": (
        "The call/request path is less prominent than it should be for a high-intent "
        "service visitor."
    ),
    "header_navigation": (
        "The header/navigation competes with the primary action more than it should."
    ),
    "visual_clutter": (
        "The page presents competing elements that make the next step less obvious."
    ),
    "readability": (
        "Several sections are harder to scan than they should be for a service buyer."
    ),
    "design_age": (
        "The visual presentation likely weakens the first impression."
    ),
    "form_or_booking_path": (
        "The request/booking path is not obvious enough from the main conversion areas."
    ),
    "service_clarity": (
        "The site does not clarify the core services quickly enough for a visitor."
    ),
    "trust_signals": (
        "Trust signals are weak, buried, or not organized around the conversion path."
    ),
    "content_depth": (
        "The service content is thin for a high-intent visitor comparing options."
    ),
    "seo_structure": (
        "The service/page structure is thin for local-search and service-specific "
        "discovery."
    ),
    "performance_perception": (
        "The page presentation feels heavier than it should on mobile."
    ),
    "layout_consistency": (
        "The layout lacks consistency between sections, which may weaken perceived polish."
    ),
    "conversion_path": (
        "The route from landing on the site to calling or requesting service is not "
        "direct enough."
    ),
}

REASON_KEY_MAP = {
    "mobile PageSpeed score < 50": "pagespeed:mobile_low",
    "desktop PageSpeed score < 60": "pagespeed:desktop_low",
    "no tel link found": "site:no_tel_link",
    "no contact form found": "site:no_form",
    "no clear quote/contact path found": "site:no_conversion_path",
    "no obvious service pages found": "site:no_service_pages",
    "no analytics/GTM/GA4 detected": "site:no_tracking",
    "no schema detected": "site:no_schema",
    "legacy page builder detected": "site:legacy_builder",
    "locked website platform detected": "site:locked_platform",
    "weak title/meta": "site:weak_title_meta",
}

SUBJECTS = {
    1: "Website notes for {business_name}",
    2: "Mobile call path on {business_name}",
    3: "Service-page clarity for {business_name}",
    4: "Closing the site notes for {business_name}",
}

EMAIL_1_SUBJECT_OPTIONS = (
    "Website notes for {business_name}",
    "A few site fixes for {business_name}",
    "{business_name} call path",
)


@dataclass
class Issue:
    key: str
    claim: str
    evidence: str
    source: str
    themes: tuple[str, ...]
    priority: int
    severity: int | None = None
    points: int | None = None
    reason: str | None = None

    def metadata(self) -> dict[str, Any]:
        data = asdict(self)
        data["themes"] = list(self.themes)
        return data


def build_arg_parser():
    parser = build_parser("Generate grounded outreach drafts for approved prospects.")
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Generate drafts for one approved prospect.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate drafts even when all four draft artifacts already exist.",
    )
    parser.add_argument(
        "--include-missing-email",
        action="store_true",
        help="Allow draft generation when no selected contact email exists.",
    )
    parser.add_argument(
        "--variant-index",
        type=int,
        default=0,
        help="Deterministic copy variant index. Defaults to 0.",
    )
    parser.add_argument(
        "--style",
        choices=["owner_friendly", "direct", "technical"],
        default="owner_friendly",
        help="Outbound copy style. Defaults to owner_friendly.",
    )
    parser.add_argument(
        "--steps",
        default="all",
        help="Draft steps to generate: all, one step, or comma-separated steps like 1,2,3,4.",
    )
    parser.add_argument(
        "--first-batch",
        action="store_true",
        help="Generate only Step 1 for first live outbound testing.",
    )
    parser.add_argument(
        "--clean-followups",
        action="store_true",
        help="When generating Step 1 only, delete existing Step 2-4 draft artifacts and files.",
    )
    return parser


def parse_draft_steps(value: Any) -> tuple[int, ...]:
    raw = str(value or "all").strip().lower()
    if raw in {"", "all"}:
        return DRAFT_STEPS

    steps: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            step = int(token)
        except ValueError as exc:
            raise ValueError("--steps must be all, a step number, or a comma-separated list.") from exc
        if step not in DRAFT_STEPS:
            raise ValueError(f"Unsupported draft step: {step}")
        if step not in steps:
            steps.append(step)

    if not steps:
        raise ValueError("--steps did not include any draft steps.")
    return tuple(steps)


def _json_loads(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _load_templates(steps: tuple[int, ...] = DRAFT_STEPS) -> dict[int, Any]:
    try:
        from jinja2 import Environment, FileSystemLoader
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before generating outreach drafts.") from exc

    env = Environment(
        loader=FileSystemLoader(str(project_path("templates/outreach"))),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    return {step: env.get_template(f"email_{step}.txt.j2") for step in steps}


def _select_candidates(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    prospect_id: int | None,
    allow_regeneration: bool,
) -> list[dict[str, Any]]:
    clauses = ["UPPER(COALESCE(human_review_decision, '')) = 'APPROVED'"]
    params: list[Any] = []

    if prospect_id is not None:
        clauses.append("id = ?")
        params.append(prospect_id)
        if allow_regeneration:
            clauses.append(
                "UPPER(COALESCE(next_action, '')) IN ('APPROVED_FOR_OUTREACH', 'SEND_OUTREACH')"
            )
        else:
            clauses.append("UPPER(COALESCE(next_action, '')) = 'APPROVED_FOR_OUTREACH'")
    else:
        clauses.append("UPPER(COALESCE(next_action, '')) = 'APPROVED_FOR_OUTREACH'")

    clauses.append(
        """
        (
            status IS NULL
            OR TRIM(status) = ''
            OR UPPER(status) IN ('APPROVED_FOR_OUTREACH', 'OUTREACH_DRAFTED')
        )
        """
    )

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


def _normalize_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return None
    return email


def _email_suppressed(connection: Any, email: str | None) -> bool:
    if not email:
        return False
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


def _draft_relative_path(prospect_id: int, step: int) -> str:
    return f"{OUTPUT_ROOT}/{prospect_id}/email_{step}.txt"


def _drafts_exist(
    connection: Any,
    prospect_id: int,
    steps: tuple[int, ...] = DRAFT_STEPS,
) -> bool:
    artifact_keys = [f"{prospect_id}:email_{step}" for step in steps]
    placeholders = ",".join("?" for _ in artifact_keys)
    rows = connection.execute(
        f"""
        SELECT artifact_key, path, status
        FROM artifacts
        WHERE artifact_key IN ({placeholders})
          AND artifact_type = 'email_draft'
        """,
        artifact_keys,
    ).fetchall()
    by_key = {row["artifact_key"]: row for row in rows}
    for step in steps:
        row = by_key.get(f"{prospect_id}:email_{step}")
        if row is None or row["status"] != "ready" or not row["path"]:
            return False
        if not project_path(row["path"]).is_file():
            return False
    return True


def _clean_followup_drafts(connection: Any, prospect_id: int) -> int:
    rows = connection.execute(
        """
        SELECT id, path
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type = 'email_draft'
          AND artifact_key IN (?, ?, ?)
        """,
        (
            prospect_id,
            f"{prospect_id}:email_2",
            f"{prospect_id}:email_3",
            f"{prospect_id}:email_4",
        ),
    ).fetchall()
    deleted = 0
    for row in rows:
        path = _safe_draft_path(row["path"])
        if path and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        connection.execute("DELETE FROM artifacts WHERE id = ?", (row["id"],))
        deleted += 1
    return deleted


def _safe_draft_path(path_value: Any) -> Path | None:
    if not path_value:
        return None
    path = project_path(path_value).resolve(strict=False)
    root = project_path(OUTPUT_ROOT).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _load_audits(connection: Any, prospect_id: int) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM website_audits
        WHERE prospect_id = ?
        ORDER BY id
        """,
        (prospect_id,),
    ).fetchall()
    audits: dict[str, dict[str, Any]] = {}
    for row in rows:
        audit = db.row_to_dict(row)
        audit["findings"] = _json_loads(audit.get("findings_json"), {})
        audit["raw"] = _json_loads(audit.get("raw_json"), {})
        audits[str(audit["audit_type"])] = audit
    return audits


def _load_artifacts(connection: Any, prospect_id: int) -> dict[str, list[dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT *
        FROM artifacts
        WHERE prospect_id = ?
        ORDER BY artifact_type, id
        """,
        (prospect_id,),
    ).fetchall()
    artifacts: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        artifact = db.row_to_dict(row)
        artifact["metadata"] = _json_loads(artifact.get("metadata_json"), {})
        artifacts.setdefault(str(artifact["artifact_type"]), []).append(artifact)
    return artifacts


def _score_explanation(
    prospect: dict[str, Any],
    audits: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    explanation = _json_loads(prospect.get("score_explanation_json"), {})
    if isinstance(explanation, dict) and explanation:
        return explanation
    lead_score = audits.get("lead_score") or {}
    findings = lead_score.get("findings")
    return findings if isinstance(findings, dict) else {}


def _build_issues(prospect: dict[str, Any], audits: dict[str, dict[str, Any]]) -> list[Issue]:
    issues_by_key: dict[str, Issue] = {}
    explanation = _score_explanation(prospect, audits)

    for issue in _visual_issues(audits.get("visual_review")):
        _add_issue(issues_by_key, issue)
    for issue in _pagespeed_issues(audits):
        _add_issue(issues_by_key, issue)
    for issue in _site_issues(audits.get("site")):
        _add_issue(issues_by_key, issue)

    _apply_website_pain_reason_priority(issues_by_key, explanation)

    return sorted(
        issues_by_key.values(),
        key=lambda issue: (-issue.priority, issue.key),
    )


def _add_issue(issues_by_key: dict[str, Issue], issue: Issue) -> None:
    existing = issues_by_key.get(issue.key)
    if existing is None or issue.priority > existing.priority:
        issues_by_key[issue.key] = issue


def _visual_issues(audit: dict[str, Any] | None) -> list[Issue]:
    if not audit:
        return []
    findings = audit.get("findings")
    if not isinstance(findings, dict):
        return []

    raw_issues: list[dict[str, Any]] = []
    if isinstance(findings.get("top_issues"), list):
        raw_issues.extend(item for item in findings["top_issues"] if isinstance(item, dict))

    issue_map = findings.get("issues")
    if isinstance(issue_map, dict):
        for category, item in issue_map.items():
            if not isinstance(item, dict):
                continue
            if any(existing.get("category") == category for existing in raw_issues):
                continue
            severity = _int_or_none(item.get("severity")) or 0
            if not item.get("present") or severity < 3:
                continue
            raw_issues.append({"category": category, **item})

    issues = []
    for item in raw_issues:
        category = str(item.get("category") or "").strip()
        severity = _int_or_none(item.get("severity")) or 0
        if severity < 3:
            continue
        label = str(item.get("label") or _visual_label(category)).strip()
        evidence_parts = [f"visual_review: {label}, severity {severity}/5"]
        if item.get("evidence_area"):
            evidence_parts.append(f"area: {item['evidence_area']}")
        if item.get("note"):
            evidence_parts.append(f"note: {item['note']}")
        priority = 2000 + severity * 25 - _visual_index(category)
        issues.append(
            Issue(
                key=f"visual:{category}",
                claim=VISUAL_CLAIM_COPY.get(
                    category,
                    f"{label} is a notable website issue in the saved visual review.",
                ),
                evidence="; ".join(evidence_parts),
                source="visual_review",
                themes=VISUAL_THEME_MAP.get(category, ("buyer_path",)),
                priority=priority,
                severity=severity,
            )
        )
    return issues


def _pagespeed_issues(audits: dict[str, dict[str, Any]]) -> list[Issue]:
    issues = []
    mobile = audits.get("pagespeed_mobile")
    mobile_score = _pagespeed_score(mobile)
    if mobile_score is not None and mobile_score < 50:
        issues.append(
            Issue(
                key="pagespeed:mobile_low",
                claim=(
                    "The audit shows a mobile PageSpeed performance score of "
                    f"{mobile_score}, a concerning mobile conversion issue."
                ),
                evidence=_pagespeed_evidence(mobile, "mobile"),
                source="pagespeed_mobile",
                themes=("mobile", "pagespeed", "cta"),
                priority=790 + (50 - mobile_score),
            )
        )

    desktop = audits.get("pagespeed_desktop")
    desktop_score = _pagespeed_score(desktop)
    if desktop_score is not None and desktop_score < 60:
        issues.append(
            Issue(
                key="pagespeed:desktop_low",
                claim=(
                    "The audit shows a desktop PageSpeed performance score of "
                    f"{desktop_score}, so the page may feel heavier than necessary."
                ),
                evidence=_pagespeed_evidence(desktop, "desktop"),
                source="pagespeed_desktop",
                themes=("pagespeed", "first_impression"),
                priority=650 + (60 - desktop_score),
            )
        )
    return issues


def _pagespeed_score(audit: dict[str, Any] | None) -> int | None:
    if not audit or str(audit.get("status") or "") not in SUCCESS_PAGESPEED_STATUSES:
        return None
    score = audit.get("score")
    if score is None and isinstance(audit.get("findings"), dict):
        score = audit["findings"].get("performance_score")
    return _int_or_none(score)


def _pagespeed_evidence(audit: dict[str, Any] | None, strategy: str) -> str:
    if not audit:
        return f"pagespeed_{strategy}: missing"
    findings = audit.get("findings") if isinstance(audit.get("findings"), dict) else {}
    metrics = findings.get("metrics") if isinstance(findings, dict) else {}
    metric_bits = []
    if isinstance(metrics, dict):
        for metric_key in ("largest-contentful-paint", "interactive", "total-blocking-time"):
            metric = metrics.get(metric_key)
            if isinstance(metric, dict) and metric.get("display_value"):
                metric_bits.append(f"{metric_key}: {metric['display_value']}")
    evidence = [
        f"pagespeed_{strategy}: status {audit.get('status')}",
        f"score {audit.get('score')}",
    ]
    if metric_bits:
        evidence.append(", ".join(metric_bits))
    return "; ".join(evidence)


def _site_issues(audit: dict[str, Any] | None) -> list[Issue]:
    if not audit or str(audit.get("status") or "") != "succeeded":
        return []
    findings = audit.get("findings")
    if not isinstance(findings, dict):
        return []

    issues: list[Issue] = []
    if not findings.get("tel_links"):
        issues.append(
            Issue(
                key="site:no_tel_link",
                claim=(
                    "The site audit did not find a one-tap phone link, making it harder "
                    "than necessary for a visitor to call from a phone."
                ),
                evidence="site audit: tel_links empty",
                source="site",
                themes=("mobile", "cta"),
                priority=760,
            )
        )

    if not _has_conversion_path(findings):
        issues.append(
            Issue(
                key="site:no_conversion_path",
                claim=(
                    "The site audit did not verify a clear quote/contact path, so "
                    "requesting service may be harder than necessary."
                ),
                evidence="site audit: no form, booking link, or contact link with conversion intent",
                source="site",
                themes=("cta", "buyer_path"),
                priority=740,
            )
        )

    if not _has_forms(findings):
        issues.append(
            Issue(
                key="site:no_form",
                claim=(
                    "The site audit did not verify an embedded request form, which is "
                    "worth checking before relying on the current CTA path."
                ),
                evidence="site audit: forms empty",
                source="site",
                themes=("cta", "buyer_path"),
                priority=710,
            )
        )

    if not _has_service_pages(findings):
        issues.append(
            Issue(
                key="site:no_service_pages",
                claim=(
                    "The site crawl did not find obvious service-page links, which can "
                    "make the local-service buyer path less clear."
                ),
                evidence="site audit: service_page_links empty and page URLs do not show service pages",
                source="site",
                themes=("service", "buyer_path", "local_seo"),
                priority=730,
            )
        )

    if not _has_tracking(findings):
        issues.append(
            Issue(
                key="site:no_tracking",
                claim=(
                    "The site audit did not detect GA4, GTAG, GTM, or Facebook Pixel "
                    "tracking, so lead-source measurement appears thin."
                ),
                evidence="site audit: tracking flags false",
                source="site",
                themes=("tracking",),
                priority=620,
            )
        )

    if not _has_schema(findings):
        issues.append(
            Issue(
                key="site:no_schema",
                claim=(
                    "The site audit did not detect structured schema markup, which is "
                    "worth fixing for a local service website."
                ),
                evidence="site audit: schema json_ld_count is 0 and types empty",
                source="site",
                themes=("local_seo", "service"),
                priority=610,
            )
        )

    technology = findings.get("technology") if isinstance(findings.get("technology"), dict) else {}
    if any(technology.get(key) for key in ("divi", "beaver_builder", "wpbakery", "oxygen")):
        issues.append(
            Issue(
                key="site:legacy_builder",
                claim=(
                    "The site audit detected older page-builder signals, which can make "
                    "mobile and conversion fixes more fragile."
                ),
                evidence="site audit: legacy page-builder technology flag detected",
                source="site",
                themes=("pagespeed", "first_impression"),
                priority=630,
            )
        )
    if any(technology.get(key) for key in ("wix", "squarespace")):
        issues.append(
            Issue(
                key="site:locked_platform",
                claim=(
                    "The site audit detected a locked hosted-platform signal, which can "
                    "limit how quickly the offer and lead-capture path can be adjusted."
                ),
                evidence="site audit: locked hosted-platform technology flag detected",
                source="site",
                themes=("cta", "buyer_path"),
                priority=600,
            )
        )

    if _weak_title_meta(findings):
        issues.append(
            Issue(
                key="site:weak_title_meta",
                claim=(
                    "The title/meta messaging looks thin, making the service and location "
                    "harder to understand from search or a shared link."
                ),
                evidence="site audit: short title or meta description",
                source="site",
                themes=("local_seo", "service", "first_impression"),
                priority=590,
            )
        )

    return issues


def _apply_website_pain_reason_priority(
    issues_by_key: dict[str, Issue],
    explanation: dict[str, Any],
) -> None:
    reasons = explanation.get("top_reasons") if isinstance(explanation, dict) else []
    if not isinstance(reasons, list):
        return

    for index, item in enumerate(reasons):
        if not isinstance(item, dict) or item.get("category") != "website_pain":
            continue
        reason = str(item.get("reason") or "")
        key = REASON_KEY_MAP.get(reason)
        if not key:
            continue
        points = _int_or_none(item.get("points")) or 0
        priority_bonus = 300 + points * 20 - index
        issue = issues_by_key.get(key)
        if issue is None:
            issue = _issue_from_score_reason(key, reason, points, priority_bonus)
            if issue is None:
                continue
            issues_by_key[key] = issue
        else:
            issue.priority += priority_bonus
            issue.points = points
            issue.reason = reason
            issue.evidence = (
                f"{issue.evidence}; lead_score top website_pain reason: {reason} ({points:+})"
            )


def _issue_from_score_reason(
    key: str,
    reason: str,
    points: int,
    priority_bonus: int,
) -> Issue | None:
    fallback_claims = {
        "pagespeed:mobile_low": (
            "The stored website review flagged mobile PageSpeed below the target range, "
            "which is a concerning mobile conversion issue."
        ),
        "pagespeed:desktop_low": (
            "The stored website review flagged desktop PageSpeed below the target range, "
            "so the page may feel heavier than necessary."
        ),
        "site:no_tel_link": (
            "The stored website review flagged no tel link found, which can make calling "
            "harder from mobile."
        ),
        "site:no_form": (
            "The stored website review flagged no contact form found, so the request path "
            "is worth checking."
        ),
        "site:no_conversion_path": (
            "The stored website review flagged no clear quote/contact path, so requesting "
            "service may be harder than necessary."
        ),
        "site:no_service_pages": (
            "The stored website review flagged no obvious service pages found, which can "
            "make service clarity thin."
        ),
        "site:no_tracking": (
            "The stored website review flagged no analytics/GTM/GA4 detected, so "
            "lead-source measurement appears thin."
        ),
        "site:no_schema": (
            "The stored website review flagged no schema detected, which is worth fixing "
            "for a local service website."
        ),
        "site:legacy_builder": (
            "The stored website review flagged legacy page-builder signals, which can make "
            "mobile and conversion fixes more fragile."
        ),
        "site:locked_platform": (
            "The stored website review flagged a locked platform signal, which can limit "
            "offer and lead-capture adjustments."
        ),
        "site:weak_title_meta": (
            "The stored website review flagged weak title/meta messaging, making the service "
            "and location harder to understand from search or a shared link."
        ),
    }
    claim = fallback_claims.get(key)
    if claim is None:
        return None
    return Issue(
        key=key,
        claim=claim,
        evidence=f"lead_score top website_pain reason: {reason} ({points:+})",
        source="lead_score",
        themes=_themes_for_key(key),
        priority=700 + priority_bonus,
        points=points,
        reason=reason,
    )


def _themes_for_key(key: str) -> tuple[str, ...]:
    if key == "pagespeed:mobile_low":
        return ("mobile", "pagespeed", "cta")
    if key == "pagespeed:desktop_low":
        return ("pagespeed", "first_impression")
    if key in {"site:no_tel_link", "site:no_form", "site:no_conversion_path"}:
        return ("mobile", "cta", "buyer_path")
    if key == "site:no_service_pages":
        return ("service", "buyer_path", "local_seo")
    if key == "site:no_tracking":
        return ("tracking",)
    if key == "site:no_schema":
        return ("local_seo", "service")
    return ("buyer_path",)


def _has_forms(findings: dict[str, Any]) -> bool:
    forms = findings.get("forms") or []
    return isinstance(forms, list) and len(forms) > 0


def _link_has_conversion_intent(link: Any) -> bool:
    if isinstance(link, dict):
        text = f"{link.get('url') or ''} {link.get('text') or ''}"
    else:
        text = str(link or "")
    return CONVERSION_PATH_RE.search(text) is not None


def _has_conversion_path(findings: dict[str, Any]) -> bool:
    if _has_forms(findings):
        return True
    for key in ("booking_links", "contact_page_links"):
        links = findings.get(key) or []
        if isinstance(links, list) and any(_link_has_conversion_intent(link) for link in links):
            return True
    return False


def _has_service_pages(findings: dict[str, Any]) -> bool:
    service_links = findings.get("service_page_links") or []
    if isinstance(service_links, list) and service_links:
        return True
    page_urls = findings.get("page_urls") or []
    return isinstance(page_urls, list) and any("service" in str(url).lower() for url in page_urls)


def _has_tracking(findings: dict[str, Any]) -> bool:
    tracking = findings.get("tracking") if isinstance(findings.get("tracking"), dict) else {}
    return bool(
        tracking.get("has_ga4_or_gtag")
        or tracking.get("has_gtm")
        or tracking.get("has_facebook_pixel")
    )


def _has_schema(findings: dict[str, Any]) -> bool:
    schema = findings.get("schema") if isinstance(findings.get("schema"), dict) else {}
    return bool((schema.get("json_ld_count") or 0) > 0 or schema.get("types"))


def _weak_title_meta(findings: dict[str, Any]) -> bool:
    title = str(findings.get("title") or "").strip()
    meta = str(findings.get("meta_description") or "").strip()
    return len(title) < 20 or len(meta) < 50


def _visual_label(category: str) -> str:
    return category.replace("_", " ").title() if category else "Visual Issue"


def _visual_index(category: str) -> int:
    try:
        return VISUAL_CATEGORY_ORDER.index(category)
    except ValueError:
        return len(VISUAL_CATEGORY_ORDER)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _subject_for_step(step: int, prospect: dict[str, Any]) -> str:
    business_name = prospect.get("business_name") or "your website"
    return SUBJECTS[step].format(business_name=business_name)


def _email_1_subject_options(prospect: dict[str, Any]) -> list[str]:
    business_name = prospect.get("business_name") or "your website"
    return [subject.format(business_name=business_name) for subject in EMAIL_1_SUBJECT_OPTIONS]


def _issue_payload(issue: Issue) -> dict[str, Any]:
    return issue.metadata()


def _select_step_issues(issues: list[Issue], themes: set[str], *, limit: int) -> list[Issue]:
    selected: list[Issue] = []
    selected_keys: set[str] = set()

    for issue in issues:
        if themes.intersection(issue.themes):
            selected.append(issue)
            selected_keys.add(issue.key)
            if len(selected) >= limit:
                return selected

    target_count = min(3, limit, len(issues))
    for issue in issues:
        if len(selected) >= target_count:
            break
        if issue.key in selected_keys:
            continue
        selected.append(issue)
        selected_keys.add(issue.key)

    return selected[:limit]


def _template_context(
    *,
    prospect: dict[str, Any],
    contact: dict[str, Any] | None,
    all_issues: list[Issue],
    step_issues: list[Issue],
    step: int,
    subject: str,
) -> dict[str, Any]:
    contact_name = (contact or {}).get("name")
    return {
        "subject": subject,
        "business_name": prospect.get("business_name") or "your business",
        "contact_name": str(contact_name).strip() if contact_name else "",
        "recipient_email": (contact or {}).get("email"),
        "website_url": prospect.get("website_url"),
        "market": prospect.get("market"),
        "niche": prospect.get("niche"),
        "issues": [_issue_payload(issue) for issue in step_issues],
        "all_issue_count": len(all_issues),
        "audit_reference": "the short website notes",
        "opt_out_line": 'P.S. If this is not relevant, reply "not interested" and I will not follow up.',
        "sender_name": DEFAULT_SENDER_NAME,
        "step": step,
    }


def _step_issues(issues: list[Issue], step: int) -> list[Issue]:
    if step == 1:
        return issues[:5]
    if step == 2:
        return _select_step_issues(issues, {"mobile", "cta", "pagespeed"}, limit=4)
    if step == 3:
        return _select_step_issues(
            issues,
            {"buyer_path", "service", "local_seo", "trust", "tracking"},
            limit=4,
        )
    return issues[:3]


def _render_draft(
    template: Any,
    context: dict[str, Any],
    output_path: Path,
) -> str:
    text = template.render(**context).strip() + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists() or output_path.read_text(encoding="utf-8") != text:
        output_path.write_text(text, encoding="utf-8")
    return text


def _store_draft_artifact(
    connection: Any,
    *,
    prospect: dict[str, Any],
    step: int,
    relative_path: str,
    text: str,
    subject: str,
    recipient_email: str | None,
    selected_issues: list[Any],
    subject_options: list[str],
    selected_subject_category: str | None,
    selected_opening_category: str | None,
    style: str,
    variant_index: int,
    public_packet_url: str,
    copy_quality_flags: list[str],
    missing_public_packet_url: bool,
    issue_count_total: int,
    first_batch_mode: bool,
) -> None:
    metadata = {
        "subject": subject,
        "subject_options": subject_options,
        "selected_subject_category": selected_subject_category,
        "selected_opening_category": selected_opening_category,
        "step": step,
        "style": style,
        "variant_index": variant_index,
        "recipient_email": recipient_email,
        "public_packet_url": public_packet_url,
        "selected_issues": [_selected_issue_metadata(issue) for issue in selected_issues],
        "copy_quality_flags": copy_quality_flags,
        "missing_public_packet_url": missing_public_packet_url,
        "issue_count_total": issue_count_total,
        "first_batch_mode": first_batch_mode,
    }

    db.upsert_artifact(
        connection,
        artifact_key=f"{prospect['id']}:email_{step}",
        prospect_id=prospect["id"],
        artifact_type="email_draft",
        path=relative_path,
        content_hash=db.stable_hash(text),
        status="ready",
        metadata=metadata,
    )


def _selected_issue_metadata(issue: Any) -> dict[str, Any]:
    if isinstance(issue, Issue):
        return _issue_payload(issue)
    if isinstance(issue, dict):
        return {
            key: value
            for key, value in issue.items()
            if key
            in {
                "key",
                "category",
                "short_bullet",
                "expanded_sentence",
                "evidence_sentence",
                "theme",
                "themes",
                "source",
                "severity",
                "confidence",
                "priority",
                "technical_only",
                "mobile_cta",
                "manual_visual",
                "reason",
                "points",
                "step",
            }
        }
    return {"value": str(issue)}


def _mark_outreach_drafted(connection: Any, prospect_id: int) -> None:
    connection.execute(
        """
        UPDATE prospects
        SET status = 'OUTREACH_DRAFTED',
            next_action = 'SEND_OUTREACH',
            updated_at = ?
        WHERE id = ?
        """,
        (db.utc_now(), prospect_id),
    )


def _generate_for_prospect(
    connection: Any,
    *,
    templates: dict[int, Any] | None,
    prospect: dict[str, Any],
    dry_run: bool,
    force: bool,
    include_missing_email: bool,
    style: str,
    variant_index: int,
    steps: tuple[int, ...],
    first_batch_mode: bool,
    clean_followups: bool,
    logger: Any,
) -> dict[str, Any]:
    contact = _load_contact(connection, prospect["id"])
    recipient_email = contact.get("email") if contact else None

    if recipient_email is None and not include_missing_email:
        logger.info(
            "outreach_drafts_missing_email",
            extra={
                "event": "outreach_drafts_missing_email",
                "prospect_id": prospect["id"],
                "business_name": prospect.get("business_name"),
            },
        )
        return {"status": "skipped", "reason": "missing_email", "drafts": 0}

    if _email_suppressed(connection, recipient_email):
        logger.info(
            "outreach_drafts_suppressed_email",
            extra={
                "event": "outreach_drafts_suppressed_email",
                "prospect_id": prospect["id"],
                "business_name": prospect.get("business_name"),
                "recipient_email": recipient_email,
            },
        )
        return {"status": "skipped", "reason": "suppressed_email", "drafts": 0}

    cleaned_followups = 0
    if clean_followups and steps == (1,) and not dry_run:
        cleaned_followups = _clean_followup_drafts(connection, prospect["id"])

    existing_drafts = _drafts_exist(connection, prospect["id"], steps)
    if existing_drafts and not force:
        if not dry_run:
            _mark_outreach_drafted(connection, prospect["id"])
        logger.info(
            "outreach_drafts_already_exist",
            extra={
                "event": "outreach_drafts_already_exist",
                "prospect_id": prospect["id"],
                "business_name": prospect.get("business_name"),
                "steps": list(steps),
                "first_batch_mode": first_batch_mode,
                "cleaned_followups": cleaned_followups,
            },
        )
        return {"status": "skipped", "reason": "existing_drafts", "drafts": 0}

    audits = _load_audits(connection, prospect["id"])
    issues = _build_issues(prospect, audits)
    if not issues:
        logger.info(
            "outreach_drafts_no_grounded_issues",
            extra={
                "event": "outreach_drafts_no_grounded_issues",
                "prospect_id": prospect["id"],
                "business_name": prospect.get("business_name"),
            },
        )
        return {"status": "skipped", "reason": "no_grounded_issues", "drafts": 0}

    if dry_run:
        logger.info(
            "outreach_drafts_would_generate",
            extra={
                "event": "outreach_drafts_would_generate",
                "prospect_id": prospect["id"],
                "business_name": prospect.get("business_name"),
                "recipient_email": recipient_email,
                "issue_keys": [issue.key for issue in issues[:5]],
            },
        )
        return {"status": "would_generate", "reason": None, "drafts": len(steps)}

    if templates is None:
        raise RuntimeError("Templates must be loaded for non-dry-run generation.")

    artifacts = _load_artifacts(connection, prospect["id"])
    contacts = [contact] if contact else []
    for step in steps:
        relative_path = _draft_relative_path(prospect["id"], step)
        context = outreach_copy.build_outreach_render_context(
            prospect=prospect,
            contact=contact,
            contacts=contacts,
            audits=audits,
            artifacts=artifacts,
            issues=issues,
            step=step,
            style=style,
            variant_index=variant_index,
        )
        selected_issues = context["selected_issues"]
        subject = context["subject"]
        text = _render_draft(templates[step], context, project_path(relative_path))
        copy_quality_flags = outreach_copy.detect_copy_quality_flags(text, context)
        context["copy_quality_flags"] = copy_quality_flags
        if copy_quality_flags:
            logger.warning(
                "outreach_copy_quality_flags",
                extra={
                    "event": "outreach_copy_quality_flags",
                    "prospect_id": prospect["id"],
                    "business_name": prospect.get("business_name"),
                    "step": step,
                    "flags": copy_quality_flags,
                },
            )
        _store_draft_artifact(
            connection,
            prospect=prospect,
            step=step,
            relative_path=relative_path,
            text=text,
            subject=subject,
            recipient_email=recipient_email,
            selected_issues=selected_issues,
            subject_options=context["subject_options"],
            selected_subject_category=context.get("selected_subject_category"),
            selected_opening_category=context.get("selected_opening_category"),
            style=style,
            variant_index=variant_index,
            public_packet_url=context["public_packet_url"],
            copy_quality_flags=copy_quality_flags,
            missing_public_packet_url=bool(context["missing_public_packet_url"]),
            issue_count_total=int(context["issue_count_total"]),
            first_batch_mode=first_batch_mode,
        )

    _mark_outreach_drafted(connection, prospect["id"])
    logger.info(
        "outreach_drafts_generated",
        extra={
            "event": "outreach_drafts_generated",
            "prospect_id": prospect["id"],
            "business_name": prospect.get("business_name"),
            "recipient_email": recipient_email,
            "style": style,
            "variant_index": variant_index,
            "steps": list(steps),
            "first_batch_mode": first_batch_mode,
            "cleaned_followups": cleaned_followups,
        },
    )
    return {"status": "generated", "reason": None, "drafts": len(steps)}


def _legacy_template_context(
    *,
    prospect: dict[str, Any],
    contact: dict[str, Any] | None,
    all_issues: list[Issue],
    step_issues: list[Issue],
    step: int,
    subject: str,
) -> dict[str, Any]:
    """Retained for old tests/imports; new rendering uses outreach_copy."""
    return _template_context(
        prospect=prospect,
        contact=contact,
        all_issues=all_issues,
        step_issues=step_issues,
        step=step,
        subject=subject,
    )


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    context = setup_command(args, COMMAND)
    try:
        selected_steps = (1,) if args.first_batch else parse_draft_steps(args.steps)
    except ValueError as exc:
        parser.error(str(exc))
    first_batch_mode = selected_steps == (1,)
    if args.clean_followups and selected_steps != (1,):
        parser.error("--clean-followups is only valid when generating Step 1 only.")

    connection = db.init_db(args.db_path)
    prospects = _select_candidates(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        prospect_id=args.prospect_id,
        allow_regeneration=args.force and args.prospect_id is not None,
    )

    templates = None if args.dry_run else _load_templates(selected_steps)
    generated = 0
    would_generate = 0
    skipped = 0
    draft_count = 0

    for prospect in prospects:
        result = _generate_for_prospect(
            connection,
            templates=templates,
            prospect=prospect,
            dry_run=args.dry_run,
            force=args.force,
            include_missing_email=args.include_missing_email,
            style=args.style,
            variant_index=args.variant_index,
            steps=selected_steps,
            first_batch_mode=first_batch_mode,
            clean_followups=args.clean_followups,
            logger=context.logger,
        )
        if result["status"] == "generated":
            generated += 1
            draft_count += int(result["drafts"])
        elif result["status"] == "would_generate":
            would_generate += 1
            draft_count += int(result["drafts"])
        else:
            skipped += 1

    if not args.dry_run:
        connection.commit()
    connection.close()

    finish_command(
        context,
        selected=len(prospects),
        generated=generated,
        would_generate=would_generate,
        skipped=skipped,
        drafts=draft_count,
        steps=list(selected_steps),
        first_batch_mode=first_batch_mode,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
