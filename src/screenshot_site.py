"""Capture desktop and mobile screenshots for prospect websites."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse, urlunparse

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import project_path


COMMAND = "screenshot_site"
SCREENSHOT_TIMEOUT_MS = 15_000
DESKTOP_VIEWPORT = {"width": 1440, "height": 1000}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
PROTECTED_SCREENSHOT_STATUSES = (
    "INELIGIBLE",
    "REJECTED_REVIEW",
    "DISCARDED",
    "CLOSED_WON",
    "CLOSED_LOST",
    "PROJECT_ACTIVE",
    "PROJECT_COMPLETE",
)
PROTECTED_SCREENSHOT_NEXT_ACTIONS = ("REJECTED_BY_REVIEW",)


def _normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _select_screenshot_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    blocked_statuses = ",".join("?" for _ in PROTECTED_SCREENSHOT_STATUSES)
    blocked_next_actions = ",".join("?" for _ in PROTECTED_SCREENSHOT_NEXT_ACTIONS)
    clauses = [
        "qualification_status IN ('DISCOVERED', 'QUALIFIED', 'AUDITED')",
        f"(status IS NULL OR status NOT IN ({blocked_statuses}))",
        f"(next_action IS NULL OR next_action NOT IN ({blocked_next_actions}))",
        "website_url IS NOT NULL",
        "website_url <> ''",
    ]
    params: list[Any] = [
        *PROTECTED_SCREENSHOT_STATUSES,
        *PROTECTED_SCREENSHOT_NEXT_ACTIONS,
    ]
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


def _screenshot_paths(prospect_id: int) -> tuple[str, str]:
    return (
        f"screenshots/desktop/{prospect_id}.png",
        f"screenshots/mobile/{prospect_id}.png",
    )


def _file_sha256(path: Any) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_screenshot_artifacts(
    connection: Any,
    prospect: dict[str, Any],
    *,
    desktop_path: str,
    mobile_path: str,
) -> dict[str, Any]:
    desktop_abs = project_path(desktop_path)
    mobile_abs = project_path(mobile_path)
    desktop_hash = _file_sha256(desktop_abs)
    mobile_hash = _file_sha256(mobile_abs)

    desktop_artifact_key = f"{prospect['id']}:screenshot:desktop"
    mobile_artifact_key = f"{prospect['id']}:screenshot:mobile"
    desktop_artifact_id = db.upsert_artifact(
        connection,
        artifact_key=desktop_artifact_key,
        prospect_id=prospect["id"],
        artifact_type="screenshot_desktop",
        path=desktop_path,
        content_hash=desktop_hash,
        status="ready" if desktop_hash else "missing",
        metadata={
            "url": prospect.get("website_url"),
            "viewport": DESKTOP_VIEWPORT,
            "usage": "review_dashboard",
        },
    )
    mobile_artifact_id = db.upsert_artifact(
        connection,
        artifact_key=mobile_artifact_key,
        prospect_id=prospect["id"],
        artifact_type="screenshot_mobile",
        path=mobile_path,
        content_hash=mobile_hash,
        status="ready" if mobile_hash else "missing",
        metadata={
            "url": prospect.get("website_url"),
            "viewport": MOBILE_VIEWPORT,
            "usage": "review_dashboard",
        },
    )
    return {
        "desktop": {
            "artifact_id": desktop_artifact_id,
            "artifact_key": desktop_artifact_key,
            "path": desktop_path,
            "content_hash": desktop_hash,
            "status": "ready" if desktop_hash else "missing",
        },
        "mobile": {
            "artifact_id": mobile_artifact_id,
            "artifact_key": mobile_artifact_key,
            "path": mobile_path,
            "content_hash": mobile_hash,
            "status": "ready" if mobile_hash else "missing",
        },
    }


def _store_screenshot_audit(
    connection: Any,
    prospect: dict[str, Any],
    *,
    status: str,
    findings: dict[str, Any],
    summary: str,
) -> None:
    db.upsert_audit(
        connection,
        prospect_id=prospect["id"],
        audit_type="screenshots",
        url=prospect["website_url"],
        status=status,
        summary=summary,
        findings=findings,
        raw={},
        audited_at=db.utc_now(),
    )


def capture_screenshots_for_prospect(
    connection: Any,
    prospect: dict[str, Any],
    *,
    dry_run: bool,
    timeout_ms: int = SCREENSHOT_TIMEOUT_MS,
    logger: Any | None = None,
) -> dict[str, Any]:
    url = _normalize_url(prospect.get("website_url"))
    desktop_path, mobile_path = _screenshot_paths(prospect["id"])

    if dry_run:
        if logger:
            logger.info(
                "screenshots_would_capture",
                extra={
                    "event": "screenshots_would_capture",
                    "prospect_id": prospect["id"],
                    "url": url,
                    "desktop_path": desktop_path,
                    "mobile_path": mobile_path,
                },
            )
        return {"status": "dry_run", "desktop_path": desktop_path, "mobile_path": mobile_path}

    if not url:
        result = {"status": "failed", "error": "invalid_website_url"}
        _store_screenshot_audit(
            connection,
            prospect,
            status="failed",
            summary="Screenshot capture failed.",
            findings=result,
        )
        return result

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        result = {"status": "failed", "error": "playwright_not_installed"}
        _store_screenshot_audit(
            connection,
            prospect,
            status="failed",
            summary="Playwright is not installed.",
            findings=result,
        )
        if logger:
            logger.warning(
                "screenshots_failed",
                extra={
                    "event": "screenshots_failed",
                    "prospect_id": prospect["id"],
                    "error": str(exc)[:500],
                },
            )
        return result

    desktop_abs = project_path(desktop_path)
    mobile_abs = project_path(mobile_path)
    desktop_abs.parent.mkdir(parents=True, exist_ok=True)
    mobile_abs.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                _capture_one(
                    browser,
                    url,
                    desktop_abs,
                    DESKTOP_VIEWPORT,
                    is_mobile=False,
                    timeout_ms=timeout_ms,
                )
                _capture_one(
                    browser,
                    url,
                    mobile_abs,
                    MOBILE_VIEWPORT,
                    is_mobile=True,
                    timeout_ms=timeout_ms,
                )
            finally:
                browser.close()
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)[:500]}
        _store_screenshot_audit(
            connection,
            prospect,
            status="failed",
            summary="Screenshot capture failed.",
            findings=result,
        )
        if logger:
            logger.warning(
                "screenshots_failed",
                extra={
                    "event": "screenshots_failed",
                    "prospect_id": prospect["id"],
                    "url": url,
                    "error": str(exc)[:500],
                },
            )
        return result

    result = {
        "status": "succeeded",
        "desktop_path": desktop_path,
        "mobile_path": mobile_path,
        "desktop_viewport": DESKTOP_VIEWPORT,
        "mobile_viewport": MOBILE_VIEWPORT,
    }
    artifacts = _store_screenshot_artifacts(
        connection,
        prospect,
        desktop_path=desktop_path,
        mobile_path=mobile_path,
    )
    result["artifacts"] = artifacts
    _store_screenshot_audit(
        connection,
        prospect,
        status="succeeded",
        summary="Captured desktop and mobile screenshots.",
        findings=result,
    )
    if logger:
        logger.info(
            "screenshots_succeeded",
            extra={
                "event": "screenshots_succeeded",
                "prospect_id": prospect["id"],
                "desktop_path": desktop_path,
                "mobile_path": mobile_path,
            },
        )
    return result


def _capture_one(
    browser: Any,
    url: str,
    path: Any,
    viewport: dict[str, int],
    *,
    is_mobile: bool,
    timeout_ms: int = SCREENSHOT_TIMEOUT_MS,
) -> None:
    context = browser.new_context(
        viewport=viewport,
        is_mobile=is_mobile,
        device_scale_factor=1,
        user_agent="ai-local-site-leads/0.1 screenshot audit",
    )
    try:
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1_000)
        page.screenshot(path=str(path), full_page=False)
    finally:
        context.close()


def main() -> int:
    parser = build_parser("Capture desktop and mobile screenshots for prospect websites.")
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    connection = db.init_db(args.db_path)
    prospects = _select_screenshot_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
    )

    succeeded = 0
    failed = 0
    for prospect in prospects:
        result = capture_screenshots_for_prospect(
            connection, prospect, dry_run=args.dry_run, logger=context.logger
        )
        if result["status"] == "succeeded":
            succeeded += 1
        elif result["status"] == "failed":
            failed += 1
        if not args.dry_run:
            connection.commit()

    connection.close()
    finish_command(context, selected=len(prospects), succeeded=succeeded, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
