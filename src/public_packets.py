"""Generate sanitized static public audit packets for approved prospects."""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "public_packets"
OUTPUT_ROOT = "public_outreach"
PACKET_ROOT = f"{OUTPUT_ROOT}/p"
ASSET_ROOT = f"{OUTPUT_ROOT}/assets"
PUBLIC_CSS_SOURCE = "static/public_packet.css"
PUBLIC_CSS_OUTPUT = f"{ASSET_ROOT}/public_packet.css"
ROBOTS_PATH = f"{OUTPUT_ROOT}/robots.txt"
HEADERS_PATH = f"{OUTPUT_ROOT}/_headers"
TOKEN_BYTES = 24
MAX_ISSUES = 5


@dataclass(frozen=True)
class PacketIssue:
    title: str
    evidence: str
    recommendation: str
    source: str


def build_arg_parser():
    parser = build_parser("Generate static noindex public audit packets.")
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Generate one approved prospect packet.",
    )
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="Replace any existing public packet token before generating.",
    )
    return parser


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None or value == "":
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _load_templates():
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before generating public packets.") from exc

    env = Environment(
        loader=FileSystemLoader(str(project_path("templates/public_packet"))),
        autoescape=select_autoescape(("html", "xml", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("index.html.j2")


def _select_candidates(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    prospect_id: int | None,
) -> list[dict[str, Any]]:
    clauses = [
        "UPPER(COALESCE(human_review_decision, '')) = 'APPROVED'",
        "UPPER(COALESCE(status, '')) IN ('APPROVED_FOR_OUTREACH', 'OUTREACH_DRAFTED')",
        "UPPER(COALESCE(audit_data_status, '')) = 'READY'",
    ]
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

    sql = f"""
        SELECT *
        FROM prospects
        WHERE {" AND ".join(clauses)}
        ORDER BY expected_close_score DESC, id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def _load_audits(connection: Any, prospect_id: int) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM website_audits
        WHERE prospect_id = ?
        ORDER BY audited_at DESC, id DESC
        """,
        (prospect_id,),
    ).fetchall()
    audits: dict[str, dict[str, Any]] = {}
    for row in rows:
        audit = db.row_to_dict(row)
        audit["findings"] = _json_loads(audit.get("findings_json"), {})
        audit["raw"] = _json_loads(audit.get("raw_json"), {})
        audits.setdefault(str(audit.get("audit_type")), audit)
    return audits


def _load_artifacts(connection: Any, prospect_id: int) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM artifacts
        WHERE prospect_id = ?
        ORDER BY artifact_type, id
        """,
        (prospect_id,),
    ).fetchall()
    artifacts: dict[str, dict[str, Any]] = {}
    for row in rows:
        artifact = db.row_to_dict(row)
        artifact["metadata"] = _json_loads(artifact.get("metadata_json"), {})
        artifacts[str(artifact["artifact_type"])] = artifact
    return artifacts


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _audit_date(audits: dict[str, dict[str, Any]]) -> str:
    for key in ("site", "visual_review", "lead_score", "screenshots"):
        value = audits.get(key, {}).get("audited_at")
        if value:
            return str(value).split("T", 1)[0]
    return ""


def _website_host(url: str | None) -> str:
    if not url:
        return ""
    candidate = url if "://" in url else f"https://{url}"
    host = urlparse(candidate).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _website_href(url: str | None) -> str:
    if not url:
        return ""
    candidate = str(url).strip()
    if not candidate:
        return ""
    return candidate if "://" in candidate else f"https://{candidate}"


def _visual_issues(audit: dict[str, Any] | None) -> list[PacketIssue]:
    if not audit:
        return []
    findings = audit.get("findings")
    if not isinstance(findings, dict):
        return []
    raw_items = findings.get("top_issues") if isinstance(findings.get("top_issues"), list) else []
    output: list[PacketIssue] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "Website presentation").strip()
        severity = _int_or_none(item.get("severity"))
        evidence_bits = []
        if severity is not None:
            evidence_bits.append(f"manual visual review severity {severity}/5")
        if item.get("evidence_area"):
            evidence_bits.append(f"area: {item['evidence_area']}")
        claim = str(item.get("email_safe_claim") or f"{label} appears worth reviewing.").strip()
        output.append(
            PacketIssue(
                title=label,
                evidence="; ".join(evidence_bits) or "manual visual review",
                recommendation=claim,
                source="visual review",
            )
        )
    return output


def _site_issues(audit: dict[str, Any] | None) -> list[PacketIssue]:
    if not audit:
        return []
    findings = audit.get("findings")
    if not isinstance(findings, dict):
        return []
    issues: list[PacketIssue] = []
    if "tel_links" in findings and not _list_value(findings.get("tel_links")):
        issues.append(
            PacketIssue(
                title="Tap-to-call path",
                evidence="site audit did not find a tel: link",
                recommendation="Add a clear tap-to-call option in the header and key service sections.",
                source="site audit",
            )
        )
    if "forms" in findings and not _list_value(findings.get("forms")):
        issues.append(
            PacketIssue(
                title="Quote request form",
                evidence="site audit did not verify an embedded form",
                recommendation="Make the quote request path easy to find and quick to complete.",
                source="site audit",
            )
        )
    booking_links = _list_value(findings.get("booking_links"))
    contact_links = _list_value(findings.get("contact_page_links"))
    if "booking_links" in findings and "contact_page_links" in findings and not booking_links and not contact_links:
        issues.append(
            PacketIssue(
                title="Contact path clarity",
                evidence="site audit did not verify a clear contact or booking link",
                recommendation="Use a prominent estimate/contact call-to-action that leads to a working request path.",
                source="site audit",
            )
        )
    service_links = _list_value(findings.get("service_page_links"))
    if "service_page_links" in findings and not service_links:
        issues.append(
            PacketIssue(
                title="Service page visibility",
                evidence="site audit did not find obvious service page links",
                recommendation="Make primary services easier to scan from the homepage and navigation.",
                source="site audit",
            )
        )
    tracking = findings.get("tracking") if isinstance(findings.get("tracking"), dict) else {}
    if tracking and not any(
        tracking.get(key) for key in ("has_ga4_or_gtag", "has_gtm", "has_facebook_pixel")
    ):
        issues.append(
            PacketIssue(
                title="Measurement setup",
                evidence="site audit did not detect common analytics tags",
                recommendation="Add basic conversion measurement for calls, forms, and high-intent pages.",
                source="site audit",
            )
        )
    schema = findings.get("schema") if isinstance(findings.get("schema"), dict) else {}
    if schema and not _list_value(schema.get("types")):
        issues.append(
            PacketIssue(
                title="Structured business markup",
                evidence="site audit did not detect JSON-LD schema types",
                recommendation="Add basic local business/service schema after verifying business details.",
                source="site audit",
            )
        )
    if not findings.get("title") or not findings.get("meta_description"):
        issues.append(
            PacketIssue(
                title="Search snippet clarity",
                evidence="site audit found a missing title or meta description",
                recommendation="Clarify the page title and description around the main service, location, and call action.",
                source="site audit",
            )
        )
    return issues


def _pagespeed_issues(audits: dict[str, dict[str, Any]]) -> list[PacketIssue]:
    issues: list[PacketIssue] = []
    for key, label, threshold in (
        ("pagespeed_mobile", "Mobile page speed", 50),
        ("pagespeed_desktop", "Desktop page speed", 60),
    ):
        audit = audits.get(key)
        if not audit or audit.get("status") not in {"succeeded", "fallback_succeeded"}:
            continue
        score = _int_or_none(audit.get("score"))
        if score is None and isinstance(audit.get("findings"), dict):
            score = _int_or_none(audit["findings"].get("performance_score"))
        if score is not None and score < threshold:
            issues.append(
                PacketIssue(
                    title=label,
                    evidence=f"stored PageSpeed performance score {score}",
                    recommendation="Review image weight, scripts, and above-the-fold loading behavior.",
                    source="PageSpeed",
                )
            )
    return issues


def _score_reason_issues(prospect: dict[str, Any]) -> list[PacketIssue]:
    explanation = _json_loads(prospect.get("score_explanation_json"), {})
    reasons = explanation.get("top_reasons") if isinstance(explanation, dict) else []
    output: list[PacketIssue] = []
    if not isinstance(reasons, list):
        return output
    public_map = {
        "no tel link found": (
            "Tap-to-call path",
            "stored lead score reason",
            "Add a clear tap-to-call option in the header and key service sections.",
        ),
        "no contact form found": (
            "Quote request form",
            "stored lead score reason",
            "Make the quote request path easy to find and quick to complete.",
        ),
        "no clear quote/contact path found": (
            "Contact path clarity",
            "stored lead score reason",
            "Use a prominent estimate/contact call-to-action that leads to a working request path.",
        ),
        "no obvious service pages found": (
            "Service page visibility",
            "stored lead score reason",
            "Make primary services easier to scan from the homepage and navigation.",
        ),
        "no analytics/GTM/GA4 detected": (
            "Measurement setup",
            "stored lead score reason",
            "Add basic conversion measurement for calls, forms, and high-intent pages.",
        ),
        "no schema detected": (
            "Structured business markup",
            "stored lead score reason",
            "Add basic local business/service schema after verifying business details.",
        ),
    }
    for item in reasons:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if reason not in public_map:
            continue
        title, evidence, recommendation = public_map[reason]
        output.append(
            PacketIssue(
                title=title,
                evidence=evidence,
                recommendation=recommendation,
                source="stored score",
            )
        )
    return output


def _selected_issues(prospect: dict[str, Any], audits: dict[str, dict[str, Any]]) -> list[PacketIssue]:
    combined = [
        *_visual_issues(audits.get("visual_review")),
        *_site_issues(audits.get("site")),
        *_pagespeed_issues(audits),
        *_score_reason_issues(prospect),
    ]
    seen: set[str] = set()
    output: list[PacketIssue] = []
    for issue in combined:
        key = issue.title.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(issue)
        if len(output) >= MAX_ISSUES:
            break
    if output:
        return output
    return [
        PacketIssue(
            title="Website review packet",
            evidence="stored audit data is available for discussion",
            recommendation="Use the screenshots and public-facing observations as a starting point for a short walkthrough.",
            source="stored audit",
        )
    ]


def _metadata(prospect: dict[str, Any]) -> dict[str, Any]:
    metadata = _json_loads(prospect.get("metadata_json"), {})
    return metadata if isinstance(metadata, dict) else {}


def _packet_metadata(prospect: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(prospect)
    packet = metadata.get("public_packet")
    return packet if isinstance(packet, dict) else {}


def _token_for(
    prospect: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    *,
    rotate: bool,
) -> str:
    if not rotate:
        existing = str(_packet_metadata(prospect).get("public_token") or "").strip()
        if existing:
            return existing
        packet_artifact_metadata = artifacts.get("public_packet", {}).get("metadata")
        if isinstance(packet_artifact_metadata, dict):
            existing = str(packet_artifact_metadata.get("token") or "").strip()
            if existing:
                return existing
    return secrets.token_urlsafe(TOKEN_BYTES)


def _copy_screenshot(artifact: dict[str, Any] | None, destination: Path, filename: str) -> str | None:
    if not artifact or not artifact.get("path"):
        return None
    source = project_path(str(artifact["path"]))
    if not source.is_file():
        return None
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename
    shutil.copy2(source, target)
    return filename


def _source_screenshot_available(artifact: dict[str, Any] | None) -> bool:
    if not artifact or not artifact.get("path"):
        return False
    return project_path(str(artifact["path"])).is_file()


def _write_static_assets() -> None:
    asset_dir = project_path(ASSET_ROOT)
    asset_dir.mkdir(parents=True, exist_ok=True)
    css_source = project_path(PUBLIC_CSS_SOURCE)
    css_target = project_path(PUBLIC_CSS_OUTPUT)
    if css_source.is_file():
        shutil.copy2(css_source, css_target)
    project_path(ROBOTS_PATH).write_text(
        "User-agent: *\nDisallow: /\n",
        encoding="utf-8",
    )
    project_path(HEADERS_PATH).write_text(
        "\n".join(
            [
                "/*",
                "  X-Robots-Tag: noindex, nofollow",
                "  X-Content-Type-Options: nosniff",
                "  Cache-Control: private, no-cache",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _issue_payload(issue: PacketIssue) -> dict[str, str]:
    return {
        "title": issue.title,
        "evidence": issue.evidence,
        "recommendation": issue.recommendation,
        "source": issue.source,
    }


def _update_prospect_packet_metadata(
    connection: Any,
    prospect: dict[str, Any],
    *,
    token: str,
    local_path: str,
    relative_url: str,
) -> None:
    metadata = _metadata(prospect)
    packet = metadata.get("public_packet")
    if not isinstance(packet, dict):
        packet = {}
    packet.update(
        {
            "public_token": token,
            "local_path": local_path,
            "relative_url": relative_url,
            "generated_at": db.utc_now(),
        }
    )
    metadata["public_packet"] = packet
    connection.execute(
        """
        UPDATE prospects
        SET metadata_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, sort_keys=True), db.utc_now(), prospect["id"]),
    )


def _render_packet(
    template: Any,
    *,
    prospect: dict[str, Any],
    audits: dict[str, dict[str, Any]],
    screenshots: dict[str, str | None],
) -> tuple[str, list[PacketIssue], dict[str, str | None]]:
    issues = _selected_issues(prospect, audits)
    context = {
        "business_name": prospect.get("business_name") or "This business",
        "website_url": prospect.get("website_url") or "",
        "website_href": _website_href(prospect.get("website_url")),
        "website_host": _website_host(prospect.get("website_url")),
        "audit_date": _audit_date(audits),
        "issues": [_issue_payload(issue) for issue in issues],
        "screenshots": screenshots,
        "css_href": "../../assets/public_packet.css",
    }
    return template.render(**context), issues, screenshots


def _generate_for_prospect(
    connection: Any,
    template: Any,
    prospect: dict[str, Any],
    *,
    rotate_token: bool,
    dry_run: bool,
) -> dict[str, Any]:
    audits = _load_audits(connection, prospect["id"])
    artifacts = _load_artifacts(connection, prospect["id"])
    token = _token_for(prospect, artifacts, rotate=rotate_token)
    packet_dir_relative = f"{PACKET_ROOT}/{token}"
    index_relative = f"{packet_dir_relative}/index.html"
    relative_url = f"/p/{token}/"
    packet_dir = project_path(packet_dir_relative)

    desktop_copied = None
    mobile_copied = None
    screenshots = {
        "desktop": "desktop.png"
        if _source_screenshot_available(artifacts.get("screenshot_desktop"))
        else None,
        "mobile": "mobile.png"
        if _source_screenshot_available(artifacts.get("screenshot_mobile"))
        else None,
    }
    if not dry_run:
        _write_static_assets()
        packet_dir.mkdir(parents=True, exist_ok=True)
        desktop_copied = _copy_screenshot(artifacts.get("screenshot_desktop"), packet_dir, "desktop.png")
        mobile_copied = _copy_screenshot(artifacts.get("screenshot_mobile"), packet_dir, "mobile.png")
        screenshots = {
            "desktop": desktop_copied,
            "mobile": mobile_copied,
        }

    html, selected_issues, _ = _render_packet(
        template,
        prospect=prospect,
        audits=audits,
        screenshots=screenshots,
    )

    if not dry_run:
        project_path(index_relative).write_text(html, encoding="utf-8")
        metadata = {
            "token": token,
            "relative_url": relative_url,
            "selected_issues": [_issue_payload(issue) for issue in selected_issues],
            "desktop_screenshot": desktop_copied,
            "mobile_screenshot": mobile_copied,
        }
        db.upsert_artifact(
            connection,
            artifact_key=f"{prospect['id']}:public_packet",
            prospect_id=prospect["id"],
            artifact_type="public_packet",
            path=index_relative,
            artifact_url=relative_url,
            content_hash=db.stable_hash(html),
            status="ready",
            metadata=metadata,
        )
        _update_prospect_packet_metadata(
            connection,
            prospect,
            token=token,
            local_path=index_relative,
            relative_url=relative_url,
        )

    return {
        "prospect_id": prospect["id"],
        "business_name": prospect.get("business_name"),
        "path": index_relative,
        "relative_url": relative_url,
        "selected_issues": len(selected_issues),
        "desktop_copied": bool(desktop_copied),
        "mobile_copied": bool(mobile_copied),
    }


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    context = setup_command(args, COMMAND)
    template = _load_templates()

    connection = db.init_db(args.db_path)
    prospects = _select_candidates(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        prospect_id=args.prospect_id,
    )

    generated = 0
    for prospect in prospects:
        result = _generate_for_prospect(
            connection,
            template,
            prospect,
            rotate_token=args.rotate_token,
            dry_run=args.dry_run,
        )
        generated += 1
        context.logger.info(
            "public_packet_prepared" if args.dry_run else "public_packet_generated",
            extra={
                "event": "public_packet_prepared" if args.dry_run else "public_packet_generated",
                "business_name": result["business_name"],
                "path": f"{PACKET_ROOT}/[token]/index.html",
                "relative_url": "/p/[token]/",
                "selected_issues": result["selected_issues"],
                "desktop_copied": result["desktop_copied"],
                "mobile_copied": result["mobile_copied"],
            },
        )

    if args.dry_run:
        connection.rollback()
    else:
        connection.commit()
    connection.close()
    finish_command(
        context,
        selected=len(prospects),
        generated=generated,
        output_root=OUTPUT_ROOT,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
