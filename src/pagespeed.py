"""Run lightweight PageSpeed Insights checks for prospect websites."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from . import db
from .cli_utils import build_parser, finish_command, setup_command


COMMAND = "pagespeed"
PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PAGESPEED_TIMEOUT_SECONDS = 30
PAGESPEED_RETRIES = 0
PAGESPEED_RETRY_DELAY_SECONDS = 10
PAGESPEED_STRATEGIES = ("mobile", "desktop")
PAGESPEED_CATEGORIES = ("performance", "accessibility", "seo")
PAGESPEED_SUCCESS_STATUSES = {"succeeded", "fallback_succeeded"}
METRIC_KEYS = (
    "first-contentful-paint",
    "largest-contentful-paint",
    "total-blocking-time",
    "cumulative-layout-shift",
    "speed-index",
    "interactive",
)
USER_AGENTS = {
    "mobile": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
}


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


def _select_pagespeed_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    strategies: tuple[str, ...],
    missing_only: bool = False,
    failed_only: bool = False,
    prospect_id: int | None = None,
) -> list[dict[str, Any]]:
    if prospect_id is not None:
        clauses = ["id = ?", "website_url IS NOT NULL", "website_url <> ''"]
        params: list[Any] = [prospect_id]
    else:
        clauses = [
            "qualification_status IN ('DISCOVERED', 'QUALIFIED', 'AUDITED')",
            "website_url IS NOT NULL",
            "website_url <> ''",
        ]
        params = []
        if market:
            clauses.append("market = ?")
            params.append(market)
        if niche:
            clauses.append("niche = ?")
            params.append(niche)

    strategy_audit_types = [f"pagespeed_{strategy}" for strategy in strategies]
    placeholders = ",".join("?" for _ in strategy_audit_types)
    if missing_only:
        clauses.append(
            f"""
            NOT EXISTS (
                SELECT 1 FROM website_audits
                WHERE website_audits.prospect_id = prospects.id
                  AND website_audits.audit_type IN ({placeholders})
            )
            """
        )
        params.extend(strategy_audit_types)
    elif failed_only:
        clauses.append(
            f"""
            EXISTS (
                SELECT 1 FROM website_audits
                WHERE website_audits.prospect_id = prospects.id
                  AND website_audits.audit_type IN ({placeholders})
                  AND website_audits.status = 'failed'
            )
            """
        )
        params.extend(strategy_audit_types)

    sql = f"SELECT * FROM prospects WHERE {' AND '.join(clauses)} ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [db.row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def _score(category: dict[str, Any] | None) -> int | None:
    if not category or category.get("score") is None:
        return None
    return round(float(category["score"]) * 100)


def _extract_pagespeed_findings(payload: dict[str, Any], strategy: str) -> dict[str, Any]:
    lighthouse = payload.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})
    metric_findings: dict[str, Any] = {}

    for key in METRIC_KEYS:
        audit = audits.get(key, {})
        metric_findings[key] = {
            "display_value": audit.get("displayValue"),
            "numeric_value": audit.get("numericValue"),
            "score": audit.get("score"),
        }

    return {
        "source": "pagespeed_insights",
        "is_fallback": False,
        "strategy": strategy,
        "performance_score": _score(categories.get("performance")),
        "accessibility_score": _score(categories.get("accessibility")),
        "seo_score": _score(categories.get("seo")),
        "metrics": metric_findings,
        "fetch_time": lighthouse.get("fetchTime"),
        "final_url": lighthouse.get("finalUrl"),
    }


def _classify_pagespeed_error(error: Exception) -> dict[str, Any]:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    message = str(error)[:500]
    code = "unknown"
    retryable = False

    if status_code == 429:
        code = "rate_limited_or_quota_exceeded"
        retryable = True
    elif status_code == 403:
        code = "forbidden_or_api_not_enabled"
    elif status_code == 400:
        code = "bad_request"
    elif isinstance(status_code, int) and status_code >= 500:
        code = "pagespeed_server_error"
        retryable = True
    elif "timed out" in message.lower() or "timeout" in message.lower():
        code = "timeout"
        retryable = True
    elif "failed to establish a new connection" in message.lower():
        code = "connection_error"
        retryable = True
    elif status_code is not None:
        code = f"http_{status_code}"

    return {
        "code": code,
        "message": message,
        "http_status": status_code,
        "retryable": retryable,
    }


def _local_speed_score(
    *,
    status_code: int,
    total_ms: int,
    ttfb_ms: int,
    html_bytes: int,
    redirect_count: int,
) -> int:
    score = 100
    if status_code >= 400:
        score -= 50
    if total_ms > 8000:
        score -= 40
    elif total_ms > 5000:
        score -= 30
    elif total_ms > 3000:
        score -= 20
    elif total_ms > 1500:
        score -= 10

    if ttfb_ms > 2000:
        score -= 20
    elif ttfb_ms > 1000:
        score -= 10
    elif ttfb_ms > 600:
        score -= 5

    if html_bytes > 2_000_000:
        score -= 20
    elif html_bytes > 1_000_000:
        score -= 10
    elif html_bytes > 500_000:
        score -= 5

    if redirect_count > 2:
        score -= 5
    return max(0, min(100, score))


def _run_local_speed_fallback(
    url: str,
    strategy: str,
    *,
    timeout: int,
    original_error: dict[str, Any],
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before running pagespeed fallback.") from exc

    headers = {
        "User-Agent": USER_AGENTS.get(strategy, USER_AGENTS["desktop"]),
        "Accept": "text/html,application/xhtml+xml",
    }
    started = time.perf_counter()
    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    total_ms = round((time.perf_counter() - started) * 1000)
    ttfb_ms = round(response.elapsed.total_seconds() * 1000)
    html_bytes = len(response.content or b"")
    score = _local_speed_score(
        status_code=response.status_code,
        total_ms=total_ms,
        ttfb_ms=ttfb_ms,
        html_bytes=html_bytes,
        redirect_count=len(response.history),
    )
    return {
        "source": "local_speed_probe",
        "is_fallback": True,
        "strategy": strategy,
        "performance_score": score,
        "accessibility_score": None,
        "seo_score": None,
        "metrics": {
            "total-load-time": {
                "display_value": f"{total_ms / 1000:.2f} s",
                "numeric_value": total_ms,
                "score": None,
            },
            "time-to-first-byte": {
                "display_value": f"{ttfb_ms / 1000:.2f} s",
                "numeric_value": ttfb_ms,
                "score": None,
            },
            "html-transfer-size": {
                "display_value": f"{html_bytes:,} bytes",
                "numeric_value": html_bytes,
                "score": None,
            },
            "redirect-count": {
                "display_value": str(len(response.history)),
                "numeric_value": len(response.history),
                "score": None,
            },
        },
        "fetch_time": db.utc_now(),
        "final_url": response.url,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "pagespeed_failure": original_error,
    }


def _store_pagespeed_result(
    connection: Any,
    prospect: dict[str, Any],
    *,
    strategy: str,
    status: str,
    findings: dict[str, Any],
    summary: str,
) -> None:
    db.upsert_audit(
        connection,
        prospect_id=prospect["id"],
        audit_type=f"pagespeed_{strategy}",
        url=prospect["website_url"],
        status=status,
        score=findings.get("performance_score"),
        summary=summary,
        findings=findings,
        raw={},
        audited_at=db.utc_now(),
    )


def _run_pagespeed_request(url: str, strategy: str, *, timeout: int) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before running pagespeed.") from exc

    params: list[tuple[str, str]] = [
        ("url", url),
        ("strategy", strategy),
    ]
    params.extend(("category", category) for category in PAGESPEED_CATEGORIES)
    api_key = os.environ.get("PAGESPEED_API_KEY")
    if api_key:
        params.append(("key", api_key))

    response = requests.get(
        PAGESPEED_ENDPOINT,
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def run_pagespeed_for_prospect(
    connection: Any,
    prospect: dict[str, Any],
    *,
    dry_run: bool,
    logger: Any | None = None,
    strategies: tuple[str, ...] = PAGESPEED_STRATEGIES,
    timeout: int = PAGESPEED_TIMEOUT_SECONDS,
    retries: int = PAGESPEED_RETRIES,
    retry_delay: int = PAGESPEED_RETRY_DELAY_SECONDS,
    allow_fallback: bool = True,
) -> list[dict[str, Any]]:
    url = _normalize_url(prospect.get("website_url"))
    results: list[dict[str, Any]] = []

    for strategy in strategies:
        if dry_run:
            result = {"status": "dry_run", "strategy": strategy, "url": url}
            results.append(result)
            if logger:
                logger.info(
                    "pagespeed_would_run",
                    extra={
                        "event": "pagespeed_would_run",
                        "prospect_id": prospect["id"],
                        "strategy": strategy,
                        "url": url,
                    },
                )
            continue

        if not url:
            result = {"status": "failed", "strategy": strategy, "error": "invalid_website_url"}
            _store_pagespeed_result(
                connection,
                prospect,
                strategy=strategy,
                status="failed",
                findings=result,
                summary=f"PageSpeed {strategy} failed.",
            )
            results.append(result)
            continue

        try:
            payload = _run_pagespeed_with_retries(
                url,
                strategy,
                timeout=timeout,
                retries=retries,
                retry_delay=retry_delay,
                logger=logger,
                prospect_id=prospect["id"],
            )
            findings = _extract_pagespeed_findings(payload, strategy)
            summary = (
                f"PageSpeed {strategy}: performance "
                f"{findings.get('performance_score')}, accessibility "
                f"{findings.get('accessibility_score')}, SEO {findings.get('seo_score')}."
            )
            _store_pagespeed_result(
                connection,
                prospect,
                strategy=strategy,
                status="succeeded",
                findings=findings,
                summary=summary,
            )
            result = {"status": "succeeded", **findings}
            results.append(result)
            if logger:
                logger.info(
                    "pagespeed_succeeded",
                    extra={
                        "event": "pagespeed_succeeded",
                        "prospect_id": prospect["id"],
                        "strategy": strategy,
                        "performance_score": findings.get("performance_score"),
                    },
                )
        except Exception as exc:
            failure = _classify_pagespeed_error(exc)
            if allow_fallback and url:
                try:
                    findings = _run_local_speed_fallback(
                        url,
                        strategy,
                        timeout=min(timeout, 20),
                        original_error=failure,
                    )
                    summary = (
                        f"PageSpeed {strategy} fallback: local speed score "
                        f"{findings.get('performance_score')} "
                        f"(PSI failed: {failure['code']})."
                    )
                    _store_pagespeed_result(
                        connection,
                        prospect,
                        strategy=strategy,
                        status="fallback_succeeded",
                        findings=findings,
                        summary=summary,
                    )
                    result = {"status": "fallback_succeeded", **findings}
                    results.append(result)
                    if logger:
                        logger.warning(
                            "pagespeed_fallback_succeeded",
                            extra={
                                "event": "pagespeed_fallback_succeeded",
                                "prospect_id": prospect["id"],
                                "strategy": strategy,
                                "performance_score": findings.get("performance_score"),
                                "pagespeed_failure_code": failure["code"],
                            },
                        )
                    continue
                except Exception as fallback_exc:
                    result = {
                        "status": "failed",
                        "strategy": strategy,
                        "error": failure["message"],
                        "failure": failure,
                        "fallback_error": str(fallback_exc)[:500],
                    }
            else:
                result = {
                    "status": "failed",
                    "strategy": strategy,
                    "error": failure["message"],
                    "failure": failure,
                }
            _store_pagespeed_result(
                connection,
                prospect,
                strategy=strategy,
                status="failed",
                findings=result,
                summary=f"PageSpeed {strategy} failed.",
            )
            results.append(result)
            if logger:
                logger.warning(
                    "pagespeed_failed",
                    extra={
                        "event": "pagespeed_failed",
                        "prospect_id": prospect["id"],
                        "strategy": strategy,
                        "error": result.get("error"),
                        "failure_code": result.get("failure", {}).get("code"),
                        "fallback_error": result.get("fallback_error"),
                    },
                )

    return results


def _run_pagespeed_with_retries(
    url: str,
    strategy: str,
    *,
    timeout: int,
    retries: int,
    retry_delay: int,
    logger: Any | None,
    prospect_id: int,
) -> dict[str, Any]:
    attempts = max(1, retries + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_pagespeed_request(url, strategy, timeout=timeout)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            if logger:
                logger.warning(
                    "pagespeed_retrying",
                    extra={
                        "event": "pagespeed_retrying",
                        "prospect_id": prospect_id,
                        "strategy": strategy,
                        "attempt": attempt,
                        "attempts": attempts,
                        "timeout": timeout,
                        "retry_delay": retry_delay,
                        "error": str(exc)[:500],
                    },
                )
            time.sleep(retry_delay)
    if last_error is None:
        raise RuntimeError("PageSpeed failed without an exception.")
    raise last_error


def main() -> int:
    parser = build_parser("Run PageSpeed Insights checks for prospect websites.")
    parser.add_argument(
        "--strategy",
        choices=["mobile", "desktop", "both"],
        default="both",
        help="Run mobile, desktop, or both PageSpeed strategies.",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Only select prospects with no PageSpeed audit for the selected strategy.",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="Only select prospects with a failed PageSpeed audit for the selected strategy.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=PAGESPEED_TIMEOUT_SECONDS,
        help="Read timeout per PageSpeed request in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=PAGESPEED_RETRIES,
        help="Retry count per PageSpeed strategy after the first attempt.",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=PAGESPEED_RETRY_DELAY_SECONDS,
        help="Seconds to wait between PageSpeed retries.",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Do not run the local speed fallback if PageSpeed Insights fails.",
    )
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Run PageSpeed for one specific prospect.",
    )
    args = parser.parse_args()
    if args.missing_only and args.failed_only:
        parser.error("--missing-only and --failed-only cannot be used together")
    context = setup_command(args, COMMAND)

    connection = db.init_db(args.db_path)
    strategies = (
        PAGESPEED_STRATEGIES
        if args.strategy == "both"
        else (args.strategy,)
    )
    prospects = _select_pagespeed_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        strategies=strategies,
        missing_only=args.missing_only,
        failed_only=args.failed_only,
        prospect_id=args.prospect_id,
    )

    succeeded = 0
    failed = 0
    for prospect in prospects:
        results = run_pagespeed_for_prospect(
            connection,
            prospect,
            dry_run=args.dry_run,
            logger=context.logger,
            strategies=strategies,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
            allow_fallback=not args.no_fallback,
        )
        succeeded += sum(
            1 for result in results if result["status"] in PAGESPEED_SUCCESS_STATUSES
        )
        failed += sum(1 for result in results if result["status"] == "failed")
        if not args.dry_run:
            connection.commit()

    connection.close()
    finish_command(context, selected=len(prospects), succeeded=succeeded, failed=failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
