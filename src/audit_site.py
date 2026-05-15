"""Audit prospect websites with lightweight HTML parsing and helper checks."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .pagespeed import run_pagespeed_for_prospect
from .pagespeed import PAGESPEED_RETRIES, PAGESPEED_RETRY_DELAY_SECONDS, PAGESPEED_TIMEOUT_SECONDS
from .pagespeed import PAGESPEED_SUCCESS_STATUSES
from .screenshot_site import SCREENSHOT_TIMEOUT_MS, capture_screenshots_for_prospect


COMMAND = "audit_site"
MAX_PAGES_PER_PROSPECT = 5
REQUEST_TIMEOUT_SECONDS = 8
FAST_MAX_PAGES_PER_PROSPECT = 2
FAST_REQUEST_TIMEOUT_MS = 5_000
FAST_SCREENSHOT_TIMEOUT_MS = 8_000
MAX_HTML_CHARS = 750_000
MAX_VISIBLE_TEXT_CHARS = 80_000
BLOCKED_AUDIT_STATUSES = (
    "INELIGIBLE",
    "DISQUALIFIED",
    "DISCARDED",
    "REJECTED",
    "REJECTED_REVIEW",
    "CLOSED_LOST",
    "CLOSED_WON",
    "PROJECT_ACTIVE",
    "PROJECT_COMPLETE",
)
BLOCKED_AUDIT_NEXT_ACTIONS = ("DISCARD", "DISQUALIFIED")
CRAWL_KEYWORDS = ("contact", "services", "service", "about", "areas", "locations")
BOOKING_INTENT_RE = re.compile(
    r"\b(book|booking|schedule|appointment|calendar|estimate|quote|calendly|acuity)\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}"
)


def _load_html_deps():
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before running audit_site.") from exc
    return requests, BeautifulSoup


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


def _domain(value: str) -> str:
    hostname = urlparse(value).hostname or ""
    hostname = hostname.lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def _same_site(base_url: str, candidate_url: str) -> bool:
    return _domain(base_url) == _domain(candidate_url)


def _canonical_internal_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    raw = href.strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None

    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    normalized = urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
    if not _same_site(base_url, normalized):
        return None
    return normalized.rstrip("/") if normalized.endswith("/") and parsed.path != "/" else normalized


def _fetch_html(
    session: Any,
    url: str,
    user_agent: str,
    *,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    response = session.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout_seconds,
        allow_redirects=True,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type:
        raise ValueError(f"Non-HTML response: {content_type}")
    html = response.text[:MAX_HTML_CHARS]
    return response.url, html


def _visible_text(soup: Any) -> str:
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    return soup.get_text(" ", strip=True)[:MAX_VISIBLE_TEXT_CHARS]


def _unique(values: list[str], limit: int = 30) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= limit:
            break
    return output


def _link_text(anchor: Any) -> str:
    return " ".join(anchor.get_text(" ", strip=True).split())


def _href_text(anchor: Any) -> str:
    return f"{anchor.get('href') or ''} {_link_text(anchor)}".lower()


def _matches_booking_intent(text: str) -> bool:
    return BOOKING_INTENT_RE.search(text) is not None


def _limited_link(url: str, label: str) -> dict[str, str]:
    return {"url": url, "text": label[:120]}


def _extract_links(page_url: str, soup: Any, root_url: str) -> dict[str, Any]:
    internal_candidates: list[tuple[int, str]] = []
    contact_links: list[dict[str, str]] = []
    service_links: list[dict[str, str]] = []
    booking_links: list[dict[str, str]] = []
    tel_links: list[str] = []
    mailto_emails: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        text = _link_text(anchor)
        haystack = _href_text(anchor)

        if href.startswith("tel:"):
            tel_links.append(href.replace("tel:", "", 1).strip())
            continue
        if href.startswith("mailto:"):
            mailto_emails.append(href.replace("mailto:", "", 1).split("?", 1)[0].strip())
            continue

        absolute = urljoin(page_url, href)
        if _matches_booking_intent(haystack):
            booking_links.append(_limited_link(absolute, text))

        internal_url = _canonical_internal_url(root_url, href)
        if not internal_url:
            continue

        if "contact" in haystack:
            contact_links.append(_limited_link(internal_url, text))
        if "service" in haystack or "services" in haystack:
            service_links.append(_limited_link(internal_url, text))

        priority = _crawl_priority(haystack)
        if priority is not None:
            internal_candidates.append((priority, internal_url))

    internal_candidates.sort(key=lambda item: item[0])
    return {
        "internal_candidates": [url for _, url in internal_candidates],
        "contact_page_links": _dedupe_link_dicts(contact_links),
        "service_page_links": _dedupe_link_dicts(service_links),
        "booking_links": _dedupe_link_dicts(booking_links),
        "tel_links": _unique(tel_links),
        "mailto_emails": _unique(mailto_emails),
    }


def _crawl_priority(text: str) -> int | None:
    for index, keyword in enumerate(CRAWL_KEYWORDS):
        if keyword in text:
            return index
    return None


def _dedupe_link_dicts(values: list[dict[str, str]], limit: int = 20) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for value in values:
        url = value["url"]
        if url in seen:
            continue
        seen.add(url)
        output.append(value)
        if len(output) >= limit:
            break
    return output


def _extract_forms(page_url: str, soup: Any) -> list[dict[str, Any]]:
    forms: list[dict[str, Any]] = []
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        forms.append(
            {
                "page_url": page_url,
                "method": (form.get("method") or "get").lower(),
                "action_path": urlparse(urljoin(page_url, action)).path[:160],
                "input_count": len(form.find_all(["input", "textarea", "select"])),
                "has_textarea": form.find("textarea") is not None,
                "has_select": form.find("select") is not None,
            }
        )
    return forms[:20]


def _extract_tracking(soup: Any, html: str) -> dict[str, Any]:
    script_srcs = [
        script.get("src", "")
        for script in soup.find_all("script")
        if script.get("src")
    ]
    script_hosts = _unique(
        [urlparse(src).hostname or src.split("/", 1)[0] for src in script_srcs],
        limit=20,
    )
    lower_html = html.lower()
    joined_scripts = " ".join(script_srcs).lower() + " " + lower_html[:120_000]
    return {
        "has_ga4_or_gtag": "gtag(" in joined_scripts
        or "googletagmanager.com/gtag/js" in joined_scripts
        or re.search(r"\bG-[A-Z0-9]{6,}\b", html) is not None,
        "has_gtm": "googletagmanager.com/gtm.js" in joined_scripts
        or re.search(r"\bGTM-[A-Z0-9]{4,}\b", html) is not None,
        "has_facebook_pixel": "connect.facebook.net" in joined_scripts
        or "fbq(" in joined_scripts
        or "facebook pixel" in joined_scripts,
        "script_src_hosts": script_hosts,
    }


def _extract_schema(soup: Any) -> dict[str, Any]:
    schema_types: list[str] = []
    scripts = soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)})
    for script in scripts:
        payload = (script.string or script.get_text() or "").strip()
        if not payload or len(payload) > 200_000:
            continue
        try:
            import json

            parsed = json.loads(payload)
        except Exception:
            continue
        schema_types.extend(_schema_types(parsed))
    return {"json_ld_count": len(scripts), "types": _unique(schema_types, limit=30)}


def _schema_types(value: Any) -> list[str]:
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_schema_types(item))
        return output
    if not isinstance(value, dict):
        return []

    output: list[str] = []
    raw_type = value.get("@type")
    if isinstance(raw_type, str):
        output.append(raw_type)
    elif isinstance(raw_type, list):
        output.extend([item for item in raw_type if isinstance(item, str)])

    graph = value.get("@graph")
    if graph is not None:
        output.extend(_schema_types(graph))
    return output


def _extract_technology(soup: Any, html: str) -> dict[str, bool]:
    lower_html = html.lower()
    generator = ""
    generator_meta = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    if generator_meta:
        generator = (generator_meta.get("content") or "").lower()

    return {
        "wordpress": "wordpress" in generator
        or "wp-content" in lower_html
        or "wp-includes" in lower_html
        or "/wp-json" in lower_html,
        "elementor": "elementor" in lower_html,
        "divi": "et_pb" in lower_html or "divi" in generator,
        "beaver_builder": "fl-builder" in lower_html,
        "wpbakery": "wpb_" in lower_html or "js_composer" in lower_html,
        "oxygen": "oxygen-" in lower_html or "ct-section" in lower_html,
        "wix": "wixstatic" in lower_html or "x-wix" in lower_html,
        "squarespace": "squarespace" in lower_html,
    }


def _merge_page_findings(target: dict[str, Any], page: dict[str, Any]) -> None:
    for key in ("visible_phone_numbers", "tel_links", "mailto_emails", "visible_emails"):
        target[key] = _unique(target[key] + page[key])

    for key in ("contact_page_links", "service_page_links", "booking_links"):
        target[key] = _dedupe_link_dicts(target[key] + page[key])

    target["forms"].extend(page["forms"])
    target["forms"] = target["forms"][:20]
    target["schema"]["json_ld_count"] += page["schema"]["json_ld_count"]
    target["schema"]["types"] = _unique(target["schema"]["types"] + page["schema"]["types"])

    for key, value in page["tracking"].items():
        if key == "script_src_hosts":
            target["tracking"][key] = _unique(target["tracking"][key] + value, limit=20)
        else:
            target["tracking"][key] = bool(target["tracking"].get(key) or value)

    for key, value in page["technology"].items():
        target["technology"][key] = bool(target["technology"].get(key) or value)


def _analyze_page(page_url: str, root_url: str, html: str, BeautifulSoup: Any) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    links = _extract_links(page_url, soup, root_url)
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    description_meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta_description = (
        description_meta.get("content", "").strip()[:320] if description_meta else None
    )
    forms = _extract_forms(page_url, soup)
    tracking = _extract_tracking(soup, html)
    schema = _extract_schema(soup)
    technology = _extract_technology(soup, html)
    text = _visible_text(soup)

    return {
        "title": title[:180] if title else None,
        "meta_description": meta_description,
        "visible_phone_numbers": _unique(PHONE_RE.findall(text)),
        "tel_links": links["tel_links"],
        "mailto_emails": links["mailto_emails"],
        "visible_emails": _unique(EMAIL_RE.findall(text)),
        "contact_page_links": links["contact_page_links"],
        "service_page_links": links["service_page_links"],
        "booking_links": links["booking_links"],
        "forms": forms,
        "tracking": tracking,
        "schema": schema,
        "technology": technology,
        "internal_candidates": links["internal_candidates"],
    }


def audit_website(
    prospect: dict[str, Any],
    *,
    audit_mode: str = "deep",
    max_pages: int = MAX_PAGES_PER_PROSPECT,
    page_timeout_ms: int = REQUEST_TIMEOUT_SECONDS * 1000,
    logger: Any | None = None,
) -> dict[str, Any]:
    requests, BeautifulSoup = _load_html_deps()
    homepage_url = _normalize_url(prospect.get("website_url"))
    if not homepage_url:
        raise ValueError("Prospect has no valid website_url")

    max_pages = max(1, int(max_pages))
    timeout_seconds = max(1, int(page_timeout_ms)) / 1000
    user_agent = os.environ.get("USER_AGENT") or "ai-local-site-leads/0.1"
    session = requests.Session()
    queued_urls = [homepage_url]
    visited: set[str] = set()
    page_urls: list[str] = []
    crawl_errors: list[dict[str, str]] = []
    findings: dict[str, Any] = {
        "audit_mode": audit_mode,
        "max_pages": max_pages,
        "page_timeout_ms": page_timeout_ms,
        "homepage_url": homepage_url,
        "final_homepage_url": None,
        "pages_crawled": 0,
        "page_urls": [],
        "title": None,
        "meta_description": None,
        "visible_phone_numbers": [],
        "tel_links": [],
        "mailto_emails": [],
        "visible_emails": [],
        "contact_page_links": [],
        "service_page_links": [],
        "booking_links": [],
        "forms": [],
        "tracking": {
            "has_ga4_or_gtag": False,
            "has_gtm": False,
            "has_facebook_pixel": False,
            "script_src_hosts": [],
        },
        "schema": {"json_ld_count": 0, "types": []},
        "technology": {
            "wordpress": False,
            "elementor": False,
            "divi": False,
            "beaver_builder": False,
            "wpbakery": False,
            "oxygen": False,
            "wix": False,
            "squarespace": False,
        },
        "crawl_errors": crawl_errors,
    }

    while queued_urls and len(visited) < max_pages:
        current_url = queued_urls.pop(0)
        if current_url in visited:
            continue
        visited.add(current_url)

        try:
            final_url, html = _fetch_html(
                session,
                current_url,
                user_agent,
                timeout_seconds=timeout_seconds,
            )
            analyzed = _analyze_page(final_url, homepage_url, html, BeautifulSoup)
        except Exception as exc:
            crawl_errors.append({"url": current_url, "error": str(exc)[:240]})
            if not page_urls:
                raise
            continue

        if not findings["final_homepage_url"]:
            findings["final_homepage_url"] = final_url
            findings["title"] = analyzed["title"]
            findings["meta_description"] = analyzed["meta_description"]

        page_urls.append(final_url)
        _merge_page_findings(findings, analyzed)

        for candidate in analyzed["internal_candidates"]:
            if candidate not in visited and candidate not in queued_urls:
                queued_urls.append(candidate)

        if logger:
            logger.info(
                "site_page_audited",
                extra={
                    "event": "site_page_audited",
                    "prospect_id": prospect["id"],
                    "url": final_url,
                    "pages_crawled": len(page_urls),
                    "audit_mode": audit_mode,
                },
            )

    findings["pages_crawled"] = len(page_urls)
    findings["page_urls"] = page_urls[:max_pages]
    return findings


def _select_audit_prospects(
    connection: Any,
    *,
    market: str | None,
    niche: str | None,
    limit: int | None,
    prospect_id: int | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    blocked_statuses = ",".join("?" for _ in BLOCKED_AUDIT_STATUSES)
    blocked_next_actions = ",".join("?" for _ in BLOCKED_AUDIT_NEXT_ACTIONS)
    if prospect_id is not None:
        clauses = [
            "id = ?",
            "website_url IS NOT NULL",
            "website_url <> ''",
            "(qualification_status IS NULL OR qualification_status <> 'DISQUALIFIED')",
            f"(status IS NULL OR status NOT IN ({blocked_statuses}))",
            f"(next_action IS NULL OR next_action NOT IN ({blocked_next_actions}))",
            "("
            "qualification_status = 'QUALIFIED' "
            "OR status IN ('ELIGIBLE_FOR_AUDIT', 'AUDIT_READY', 'PENDING_REVIEW') "
            "OR next_action IN ('RUN_AUDIT', 'NEEDS_SITE_AUDIT', 'HUMAN_REVIEW')"
            ")",
        ]
        params: list[Any] = [
            prospect_id,
            *BLOCKED_AUDIT_STATUSES,
            *BLOCKED_AUDIT_NEXT_ACTIONS,
        ]
        if not force:
            clauses.append("(audit_data_status IS NULL OR audit_data_status <> 'READY')")
    else:
        clauses = [
            "qualification_status = 'QUALIFIED'",
            f"(status IS NULL OR status NOT IN ({blocked_statuses}))",
            f"(next_action IS NULL OR next_action NOT IN ({blocked_next_actions}))",
            "("
            "status IN ('ELIGIBLE_FOR_AUDIT', 'AUDIT_READY', 'PENDING_REVIEW') "
            "OR next_action IN ('RUN_AUDIT', 'NEEDS_SITE_AUDIT', 'HUMAN_REVIEW')"
            ")",
            "website_url IS NOT NULL",
            "website_url <> ''",
        ]
        if not force:
            clauses.append("(audit_data_status IS NULL OR audit_data_status <> 'READY')")
        params = [*BLOCKED_AUDIT_STATUSES, *BLOCKED_AUDIT_NEXT_ACTIONS]
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


def _update_qualification_status(connection: Any, prospect_id: int, status: str) -> None:
    connection.execute(
        "UPDATE prospects SET qualification_status = ?, updated_at = ? WHERE id = ?",
        (status, db.utc_now(), prospect_id),
    )


def _store_site_audit(connection: Any, prospect: dict[str, Any], findings: dict[str, Any]) -> None:
    summary = (
        f"Audited {findings['pages_crawled']} page(s); "
        f"{len(findings['visible_phone_numbers'])} visible phone(s), "
        f"{len(findings['forms'])} form(s)."
    )
    db.upsert_audit(
        connection,
        prospect_id=prospect["id"],
        audit_type="site",
        url=prospect["website_url"],
        status="succeeded",
        summary=summary,
        findings=findings,
        raw={},
        audited_at=db.utc_now(),
    )


def _store_failed_site_audit(
    connection: Any,
    prospect: dict[str, Any],
    error: Exception,
    *,
    audit_mode: str = "deep",
) -> None:
    db.upsert_audit(
        connection,
        prospect_id=prospect["id"],
        audit_type="site",
        url=prospect["website_url"],
        status="failed",
        summary="Homepage audit failed.",
        findings={"error": str(error)[:500], "audit_mode": audit_mode},
        raw={},
        audited_at=db.utc_now(),
    )


def main() -> int:
    parser = build_parser("Audit prospect websites and orchestrate screenshots/PageSpeed.")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use fast batch triage mode: lighter crawl, screenshots, and no PageSpeed by default.",
    )
    parser.add_argument(
        "--skip-pagespeed",
        action="store_true",
        help="Skip PageSpeed calls during orchestrated audits.",
    )
    parser.add_argument(
        "--include-pagespeed",
        action="store_true",
        help="Run PageSpeed even when --fast is set.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum same-site pages to crawl per prospect.",
    )
    parser.add_argument(
        "--screenshot-timeout-ms",
        type=int,
        default=None,
        help="Timeout per screenshot page navigation in milliseconds.",
    )
    parser.add_argument(
        "--page-timeout-ms",
        type=int,
        default=None,
        help="Timeout per lightweight HTML page request in milliseconds.",
    )
    parser.add_argument(
        "--pagespeed-timeout",
        type=int,
        default=PAGESPEED_TIMEOUT_SECONDS,
        help="Read timeout per PageSpeed request in seconds.",
    )
    parser.add_argument(
        "--pagespeed-retries",
        type=int,
        default=PAGESPEED_RETRIES,
        help="Retry count per PageSpeed strategy after the first attempt.",
    )
    parser.add_argument(
        "--pagespeed-retry-delay",
        type=int,
        default=PAGESPEED_RETRY_DELAY_SECONDS,
        help="Seconds to wait between PageSpeed retries.",
    )
    parser.add_argument(
        "--prospect-id",
        type=int,
        default=None,
        help="Audit one specific prospect, still skipping READY audit data unless --force is set.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow auditing prospects whose audit_data_status is already READY.",
    )
    args = parser.parse_args()
    context = setup_command(args, COMMAND)
    audit_mode = "fast" if args.fast else "deep"
    max_pages = args.max_pages
    if max_pages is None:
        max_pages = FAST_MAX_PAGES_PER_PROSPECT if args.fast else MAX_PAGES_PER_PROSPECT
    if max_pages < 1:
        parser.error("--max-pages must be a positive integer")

    page_timeout_ms = args.page_timeout_ms
    if page_timeout_ms is None:
        page_timeout_ms = FAST_REQUEST_TIMEOUT_MS if args.fast else REQUEST_TIMEOUT_SECONDS * 1000
    if page_timeout_ms < 1:
        parser.error("--page-timeout-ms must be a positive integer")

    screenshot_timeout_ms = args.screenshot_timeout_ms
    if screenshot_timeout_ms is None:
        screenshot_timeout_ms = FAST_SCREENSHOT_TIMEOUT_MS if args.fast else SCREENSHOT_TIMEOUT_MS
    if screenshot_timeout_ms < 1:
        parser.error("--screenshot-timeout-ms must be a positive integer")

    skip_pagespeed = bool(args.skip_pagespeed or (args.fast and not args.include_pagespeed))
    context.logger.info(
        "audit_mode_configured",
        extra={
            "event": "audit_mode_configured",
            "audit_mode": audit_mode,
            "max_pages": max_pages,
            "page_timeout_ms": page_timeout_ms,
            "screenshot_timeout_ms": screenshot_timeout_ms,
            "skip_pagespeed": skip_pagespeed,
        },
    )

    connection = db.init_db(args.db_path)
    prospects = _select_audit_prospects(
        connection,
        market=args.market,
        niche=args.niche,
        limit=args.limit,
        prospect_id=args.prospect_id,
        force=args.force,
    )

    audited = 0
    failed = 0
    screenshots_succeeded = 0
    pagespeed_succeeded = 0
    pagespeed_skipped = 0

    for prospect in prospects:
        if args.dry_run:
            context.logger.info(
                "site_audit_would_run",
                extra={
                    "event": "site_audit_would_run",
                    "prospect_id": prospect["id"],
                    "business_name": prospect["business_name"],
                    "url": prospect["website_url"],
                    "audit_mode": audit_mode,
                },
            )
            continue

        try:
            findings = audit_website(
                prospect,
                audit_mode=audit_mode,
                max_pages=max_pages,
                page_timeout_ms=page_timeout_ms,
                logger=context.logger,
            )
            _store_site_audit(connection, prospect, findings)
            screenshot_result = capture_screenshots_for_prospect(
                connection,
                prospect,
                dry_run=False,
                timeout_ms=screenshot_timeout_ms,
                logger=context.logger,
            )
            pagespeed_results = []
            if skip_pagespeed:
                pagespeed_skipped += 1
                context.logger.info(
                    "pagespeed_skipped",
                    extra={
                        "event": "pagespeed_skipped",
                        "prospect_id": prospect["id"],
                        "reason": (
                            "fast_audit"
                            if args.fast and not args.include_pagespeed
                            else "skip_pagespeed_flag"
                        ),
                        "audit_mode": audit_mode,
                    },
                )
            else:
                pagespeed_results = run_pagespeed_for_prospect(
                    connection,
                    prospect,
                    dry_run=False,
                    logger=context.logger,
                    timeout=args.pagespeed_timeout,
                    retries=args.pagespeed_retries,
                    retry_delay=args.pagespeed_retry_delay,
                )
            _update_qualification_status(connection, prospect["id"], "AUDITED")
            connection.commit()
            audited += 1
            screenshots_succeeded += int(screenshot_result.get("status") == "succeeded")
            pagespeed_succeeded += sum(
                1 for result in pagespeed_results if result.get("status") in PAGESPEED_SUCCESS_STATUSES
            )
            context.logger.info(
                "site_audit_succeeded",
                extra={
                    "event": "site_audit_succeeded",
                    "prospect_id": prospect["id"],
                    "pages_crawled": findings["pages_crawled"],
                    "audit_mode": audit_mode,
                },
            )
        except Exception as exc:
            _store_failed_site_audit(connection, prospect, exc, audit_mode=audit_mode)
            _update_qualification_status(connection, prospect["id"], "AUDIT_FAILED")
            connection.commit()
            failed += 1
            context.logger.warning(
                "site_audit_failed",
                extra={
                    "event": "site_audit_failed",
                    "prospect_id": prospect["id"],
                    "url": prospect["website_url"],
                    "error": str(exc)[:500],
                    "audit_mode": audit_mode,
                },
            )

    connection.close()
    finish_command(
        context,
        audit_mode=audit_mode,
        selected=len(prospects),
        audited=audited,
        failed=failed,
        screenshots_succeeded=screenshots_succeeded,
        pagespeed_succeeded=pagespeed_succeeded,
        pagespeed_skipped=pagespeed_skipped,
    )
    if prospects and failed and audited == 0 and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
