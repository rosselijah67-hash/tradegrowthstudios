"""Deterministic owner-facing outbound copy helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from .config import load_yaml_config


DEFAULT_STYLE = "owner_friendly"
VALID_STYLES = {"owner_friendly", "direct", "technical"}
DEFAULT_SENDER_COMPANY = "Trade Growth Studio"
DEFAULT_BANNED_PHRASES = [
    "case file",
    "audit notes",
    "audit recorded",
    "our system detected",
    "[Your Name]",
]
TECHNICAL_CATEGORIES = {"pagespeed", "no_analytics", "no_schema"}
CATEGORY_ALIASES = {
    "call_to_action": "cta_clarity",
    "form_or_booking_path": "form_booking_path",
}
MOBILE_CTA_CATEGORIES = {
    "mobile_layout",
    "cta_clarity",
    "form_booking_path",
    "conversion_path",
    "no_tel_link",
    "no_form",
    "pagespeed",
}
STEP_2_CATEGORIES = {
    "mobile_layout",
    "hero_section",
    "cta_clarity",
    "header_navigation",
    "form_booking_path",
    "conversion_path",
    "performance_perception",
    "no_tel_link",
    "no_form",
    "pagespeed",
}
STEP_3_CATEGORIES = {
    "service_clarity",
    "trust_signals",
    "content_depth",
    "seo_structure",
    "no_service_pages",
    "no_analytics",
    "no_schema",
}
SUBJECT_SPECIFIC_LABELS = {
    "mobile_layout": "mobile site note",
    "performance_perception": "mobile site note",
    "pagespeed": "mobile site note",
    "cta_clarity": "call path note",
    "header_navigation": "call path note",
    "form_booking_path": "call path note",
    "conversion_path": "call path note",
    "no_tel_link": "call path note",
    "no_form": "call path note",
    "service_clarity": "service page note",
    "content_depth": "service page note",
    "seo_structure": "service page note",
    "no_service_pages": "service page note",
    "trust_signals": "trust note",
    "no_analytics": "tracking note",
    "no_schema": "service page note",
}
OPENING_THEME_LABELS = {
    "mobile_layout": "mobile version",
    "performance_perception": "mobile version",
    "pagespeed": "mobile version",
    "hero_section": "first screen",
    "cta_clarity": "call/request path",
    "header_navigation": "call/request path",
    "form_booking_path": "call/request path",
    "conversion_path": "call/request path",
    "no_tel_link": "call/request path",
    "no_form": "call/request path",
    "visual_clutter": "page clarity",
    "readability": "page clarity",
    "layout_consistency": "page clarity",
    "design_age": "first impression",
    "service_clarity": "service-page structure",
    "content_depth": "service-page structure",
    "seo_structure": "service-page structure",
    "no_service_pages": "service-page structure",
    "trust_signals": "trust path",
    "no_analytics": "tracking setup",
    "no_schema": "local service structure",
}
NICHE_OPENING_HINTS = {
    "roofing": "For a roofing site, I would want the phone/request path and service pages to be obvious immediately.",
    "HVAC": "For HVAC, the emergency/service call path matters more than decorative sections.",
    "plumbing": "For plumbing, I would want the mobile call path and service-specific pages much clearer.",
    "restoration": "For restoration, speed and emergency routing matter more than a long general brochure.",
}

NICHE_LABELS = {
    "roofing": "roofing",
    "hvac": "HVAC",
    "plumbing": "plumbing",
    "electrical": "electrical",
    "garage_doors": "garage doors",
    "garage-door": "garage doors",
    "garage_doors_service": "garage doors",
    "pest_control": "pest control",
    "tree_service": "tree service",
    "restoration": "restoration",
    "remodeling": "remodeling",
    "siding": "siding",
    "exterior_renovation": "exterior renovation",
}

GENERIC_FIRST_NAMES = {
    "admin",
    "contact",
    "estimate",
    "estimates",
    "hello",
    "info",
    "office",
    "sales",
    "service",
    "support",
    "team",
}

ISSUE_COPY = {
    "mobile_layout": {
        "theme": "mobile",
        "short": "The mobile version makes the visitor work too hard before they can call or request service.",
        "expanded": "On mobile, the next step is harder to find than it should be.",
        "evidence": "The saved mobile view shows friction around the call/request path.",
    },
    "hero_section": {
        "theme": "first_screen",
        "short": "The first screen does not quickly answer service, area, and next step.",
        "expanded": "The first screen could say what the business does, where it works, and what to do next faster.",
        "evidence": "The visual review flagged the first screen as a clarity issue.",
    },
    "cta_clarity": {
        "theme": "cta",
        "short": "The call/request action could be easier to spot.",
        "expanded": "The primary action should be more obvious for someone ready to call or request help.",
        "evidence": "The saved review shows the primary call/request action is not prominent enough.",
    },
    "header_navigation": {
        "theme": "cta",
        "short": "The header could do a cleaner job pointing people to call or request service.",
        "expanded": "The header/navigation should make the next action easier to choose.",
        "evidence": "The visual review flagged the header/navigation around the primary action.",
    },
    "visual_clutter": {
        "theme": "clarity",
        "short": "There are competing elements on the page that make the next step less obvious.",
        "expanded": "The page has a lot competing for attention before the visitor gets a clear next step.",
        "evidence": "The visual review flagged competing page elements.",
    },
    "readability": {
        "theme": "clarity",
        "short": "Some sections take more reading than a phone visitor is likely to give them.",
        "expanded": "The page could be easier to scan for someone deciding whether to call.",
        "evidence": "The visual review flagged readability or scanning friction.",
    },
    "design_age": {
        "theme": "trust",
        "short": "The site looks like it may not match the quality of the work people should expect.",
        "expanded": "The visual presentation could make the first impression stronger.",
        "evidence": "The saved screenshot and visual review flagged dated presentation.",
    },
    "form_booking_path": {
        "theme": "cta",
        "short": "The quote/request path could be easier to find and complete.",
        "expanded": "Someone who does not want to call immediately should have a clearer request path.",
        "evidence": "The reviewed pages did not show a clear request or booking route near the main action areas.",
    },
    "service_clarity": {
        "theme": "service",
        "short": "A visitor should be able to tell the main services faster.",
        "expanded": "The site could clarify the core services sooner.",
        "evidence": "The visual review flagged service clarity.",
    },
    "trust_signals": {
        "theme": "trust",
        "short": "Reviews, proof, and trust points could sit closer to the request path.",
        "expanded": "Trust signals should support the parts of the page where visitors decide to call or request service.",
        "evidence": "The visual review flagged trust signals as weak, buried, or separated from the CTA path.",
    },
    "content_depth": {
        "theme": "service",
        "short": "The service pages could answer more of the questions a serious buyer would have.",
        "expanded": "The service content could do more of the pre-call selling.",
        "evidence": "The review flagged limited service detail.",
    },
    "seo_structure": {
        "theme": "service_structure",
        "short": "The site could use clearer pages for the main services and service areas.",
        "expanded": "A cleaner service/page structure would make the site easier for buyers to navigate.",
        "evidence": "The review flagged service or local-search structure.",
    },
    "performance_perception": {
        "theme": "mobile",
        "short": "The mobile page feels heavier than it needs to before someone can call or request service.",
        "expanded": "The first mobile experience should feel lighter and faster.",
        "evidence": "The visual review flagged performance perception on mobile.",
    },
    "layout_consistency": {
        "theme": "clarity",
        "short": "The sections do not quite feel like one clean path.",
        "expanded": "More consistent sections would make the page feel easier to follow.",
        "evidence": "The visual review flagged layout consistency.",
    },
    "conversion_path": {
        "theme": "cta",
        "short": "The path from landing on the site to calling or requesting help could be shorter.",
        "expanded": "The site could make the next step more direct.",
        "evidence": "The review flagged the call/request path.",
    },
    "pagespeed": {
        "theme": "performance",
        "short": "The performance signals suggest the page may feel heavier than it should on mobile.",
        "expanded": "I would check images, scripts, and the first screen before redesigning around it.",
        "evidence": "Stored speed data was below the target range.",
    },
    "no_tel_link": {
        "theme": "cta",
        "short": "I did not see a clean click-to-call path where a mobile visitor would expect it.",
        "expanded": "A clear tap-to-call path should be easy to find from a phone.",
        "evidence": "The crawl did not find a tel: link on the reviewed pages.",
    },
    "no_form": {
        "theme": "cta",
        "short": "I did not see a straightforward request/quote path for someone who does not want to call immediately.",
        "expanded": "The site should support visitors who prefer to request help without calling first.",
        "evidence": "The crawl did not find a form on the reviewed pages.",
    },
    "no_service_pages": {
        "theme": "service_structure",
        "short": "The service structure could be clearer for buyers looking for a specific job type.",
        "expanded": "The main services could use clearer dedicated pages.",
        "evidence": "The crawl did not find obvious service-page links.",
    },
    "no_analytics": {
        "theme": "tracking",
        "short": "I did not see obvious tracking/tag structure, which makes calls/forms harder to attribute later.",
        "expanded": "Tracking looks light from the public site, so measurement may be harder to trust later.",
        "evidence": "The public crawl did not find common analytics tags.",
    },
    "no_schema": {
        "theme": "service_structure",
        "short": "I did not see basic local business/service markup in the public page source.",
        "expanded": "Basic structured markup is worth checking after the business details are verified.",
        "evidence": "The crawl did not find JSON-LD schema types.",
    },
    "weak_title_meta": {
        "theme": "service_structure",
        "short": "The title and search snippet could make the service and location clearer.",
        "expanded": "Search and shared-link text should quickly explain the service, area, and next step.",
        "evidence": "The stored title or meta description was short or missing.",
    },
    "legacy_builder": {
        "theme": "operations",
        "short": "The current build may make mobile and CTA fixes more fragile than they need to be.",
        "expanded": "Older builder signals can make simple website fixes take more effort.",
        "evidence": "The public crawl found older page-builder signals.",
    },
    "locked_platform": {
        "theme": "operations",
        "short": "The current platform may limit how quickly the offer and request path can be adjusted.",
        "expanded": "A locked hosted platform can make conversion-path changes harder to manage.",
        "evidence": "The public crawl found hosted-platform signals.",
    },
}


def load_outbound_voice_config() -> dict[str, Any]:
    """Load outbound voice defaults without requiring new dependencies."""

    try:
        config = load_yaml_config("outreach.yaml")
    except FileNotFoundError:
        config = {}

    defaults = _mapping(config.get("defaults"))
    sender_config = _mapping(config.get("sender"))
    copy_config = _mapping(config.get("copy"))

    sender_name = (
        _env_value("OUTREACH_FROM_NAME")
        or _string(sender_config.get("name"))
        or _string(defaults.get("from_name"))
    )
    sender_company = (
        _env_value("OUTREACH_BUSINESS_NAME")
        or _string(sender_config.get("company"))
        or _string(defaults.get("business_name"))
        or DEFAULT_SENDER_COMPANY
    )
    style = _string(copy_config.get("default_style")) or DEFAULT_STYLE
    if style not in VALID_STYLES:
        style = DEFAULT_STYLE

    banned = [*DEFAULT_BANNED_PHRASES]
    raw_banned = copy_config.get("banned_phrases")
    if isinstance(raw_banned, list):
        banned.extend(_string(item) for item in raw_banned if _string(item))

    return {
        "sender": {
            "name": sender_name,
            "company": sender_company,
            "title": _string(sender_config.get("title")),
        },
        "copy": {
            "default_style": style,
            "default_variant_index": _int_value(copy_config.get("default_variant_index"), 0),
            "include_public_packet_in_draft": _truthy(
                copy_config.get("include_public_packet_in_draft"),
                default=True,
            ),
            "step1_max_issues": max(3, min(4, _int_value(copy_config.get("step1_max_issues"), 4))),
            "banned_phrases": _unique_text(banned),
        },
        "public_packet_base_url": (
            _env_value("PUBLIC_PACKET_BASE_URL")
            or _string(defaults.get("public_packet_base_url"))
            or _string(config.get("PUBLIC_PACKET_BASE_URL"))
            or _string(config.get("public_packet_base_url"))
        ).rstrip("/"),
    }


def stable_choice(options: list[Any] | tuple[Any, ...], seed_parts: list[Any] | tuple[Any, ...]) -> Any:
    """Pick a stable item from options using a content hash."""

    if not options:
        return ""
    seed = "|".join(str(part) for part in seed_parts)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    index = int(digest[:12], 16) % len(options)
    return options[index]


def clean_business_name(name: Any) -> str:
    value = " ".join(str(name or "").replace("\n", " ").split()).strip()
    if not value:
        return "your business"
    value = re.sub(r"\s+[|:-]\s+(home|official site|homepage)$", "", value, flags=re.I)
    return value[:120].strip() or "your business"


def infer_primary_city(prospect: dict[str, Any]) -> str:
    for key in ("city", "city_guess"):
        value = _string(prospect.get(key))
        if value:
            return _title_place(value)

    metadata = _json_value(prospect.get("metadata_json"), {})
    if isinstance(metadata, dict):
        for key in ("city", "primary_city", "market_city"):
            value = _string(metadata.get(key))
            if value:
                return _title_place(value)

    market = _string(prospect.get("market"))
    if market:
        tokens = re.split(r"[_\-\s]+", market)
        if len(tokens) >= 2 and len(tokens[-1]) == 2:
            tokens = tokens[:-1]
        city = " ".join(token for token in tokens if token)
        if city:
            return _title_place(city)

    return ""


def niche_label(niche: Any) -> str:
    raw = _string(niche).lower()
    if not raw:
        return "home-service"
    normalized = raw.replace(" ", "_").replace("/", "_")
    if raw in NICHE_LABELS:
        return NICHE_LABELS[raw]
    if normalized in NICHE_LABELS:
        return NICHE_LABELS[normalized]
    return raw.replace("_", " ").replace("-", " ").strip()


def build_business_context(
    prospect: dict[str, Any],
    audits: dict[str, dict[str, Any]],
    artifacts: Any,
    contacts: Any,
) -> dict[str, Any]:
    voice_config = load_outbound_voice_config()
    normalized_artifacts = _normalize_artifacts(artifacts)
    contact = _first_contact(contacts)
    business_name = clean_business_name(prospect.get("business_name"))
    public_packet_url = build_public_packet_url(prospect, normalized_artifacts)
    visual_review = audits.get("visual_review") or {}
    visual_findings = visual_review.get("findings")
    if not isinstance(visual_findings, dict):
        visual_findings = {}

    return {
        "prospect_id": prospect.get("id"),
        "business_name": business_name,
        "contact_name": _string(contact.get("name") if contact else ""),
        "contact_first_name": _contact_first_name(contact.get("name") if contact else ""),
        "recipient_email": _string(contact.get("email") if contact else ""),
        "market": _string(prospect.get("market")),
        "niche": _string(prospect.get("niche")),
        "niche_label": niche_label(prospect.get("niche")),
        "website_url": _string(prospect.get("website_url")),
        "domain": _domain_for(prospect),
        "primary_city": infer_primary_city(prospect),
        "public_packet_url": public_packet_url,
        "has_visual_review": bool(visual_review),
        "has_mobile_screenshot": _artifact_ready(normalized_artifacts, "screenshot_mobile"),
        "has_desktop_screenshot": _artifact_ready(normalized_artifacts, "screenshot_desktop"),
        "has_pagespeed": any(
            _audit_has_score(audits.get(key))
            for key in ("pagespeed_mobile", "pagespeed_desktop")
        ),
        "has_public_packet": bool(public_packet_url),
        "has_manual_notes": bool(
            _string(prospect.get("human_review_notes"))
            or any(_string(item.get("note")) for item in _list_value(visual_findings.get("top_issues")))
        ),
        "best_contact_email": _string(contact.get("email") if contact else ""),
        "sender_name": voice_config["sender"]["name"],
        "sender_company": voice_config["sender"]["company"],
        "sender_title": voice_config["sender"]["title"],
        "voice_config": voice_config,
    }


def build_public_packet_url(prospect: dict[str, Any], artifacts: Any) -> str:
    normalized_artifacts = _normalize_artifacts(artifacts)
    packets = normalized_artifacts.get("public_packet") or []
    ready_packets = [
        item for item in packets if _string(item.get("status")).lower() == "ready"
    ]
    packet = ready_packets[-1] if ready_packets else None
    metadata = _mapping(packet.get("metadata") if packet else None)
    if not metadata and packet:
        metadata = _json_value(packet.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}

    relative = ""
    if packet:
        relative = _string(packet.get("artifact_url")) or _string(metadata.get("relative_url"))
    if not relative:
        prospect_metadata = _json_value(prospect.get("metadata_json"), {})
        if isinstance(prospect_metadata, dict):
            public_packet = _mapping(prospect_metadata.get("public_packet"))
            relative = _string(public_packet.get("relative_url"))

    if not relative:
        return ""
    if relative.startswith(("http://", "https://")):
        return relative

    base_url = load_outbound_voice_config().get("public_packet_base_url") or ""
    if base_url:
        return f"{base_url.rstrip('/')}/{relative.lstrip('/')}"
    return relative


def classify_issue_for_copy(issue: Any) -> dict[str, Any]:
    key = _issue_value(issue, "key")
    source = _issue_value(issue, "source")
    themes = _issue_value(issue, "themes") or []
    if isinstance(themes, tuple):
        themes = list(themes)

    category = key
    if key.startswith("visual:"):
        category = key.split(":", 1)[1]
    elif key.startswith("pagespeed:"):
        category = "pagespeed"
    elif key == "site:no_tel_link":
        category = "no_tel_link"
    elif key == "site:no_form":
        category = "no_form"
    elif key == "site:no_conversion_path":
        category = "conversion_path"
    elif key == "site:no_service_pages":
        category = "no_service_pages"
    elif key == "site:no_tracking":
        category = "no_analytics"
    elif key == "site:no_schema":
        category = "no_schema"
    elif key == "site:weak_title_meta":
        category = "weak_title_meta"
    elif key == "site:legacy_builder":
        category = "legacy_builder"
    elif key == "site:locked_platform":
        category = "locked_platform"
    category = CATEGORY_ALIASES.get(category, category)

    copy = ISSUE_COPY.get(category, {})
    theme = copy.get("theme") or (themes[0] if themes else "website")
    severity = _issue_value(issue, "severity")
    priority = _int_value(_issue_value(issue, "priority"), 0)
    technical_only = category in TECHNICAL_CATEGORIES

    return {
        "key": key,
        "category": category,
        "source": source,
        "theme": theme,
        "themes": themes,
        "severity": severity,
        "priority": priority,
        "technical_only": technical_only,
        "mobile_cta": category in MOBILE_CTA_CATEGORIES or theme in {"mobile", "cta"},
        "manual_visual": source == "visual_review" or key.startswith("visual:"),
    }


def humanize_issue(issue: Any, business_context: dict[str, Any], step: int) -> dict[str, Any]:
    classified = classify_issue_for_copy(issue)
    category = classified["category"]
    copy = ISSUE_COPY.get(category, {})
    style = _style_for_context(business_context)
    short_bullet = copy.get("short") or _fallback_issue_sentence(issue)
    expanded = copy.get("expanded") or short_bullet
    evidence = copy.get("evidence") or _string(_issue_value(issue, "evidence"))

    if style == "direct":
        short_bullet = _direct_sentence(short_bullet)
    elif style == "technical":
        short_bullet = _technical_sentence(short_bullet, classified)

    niche = _string(business_context.get("niche_label"))
    if niche and category in {"service_clarity", "content_depth", "seo_structure", "no_service_pages"}:
        short_bullet = _add_niche_hint(short_bullet, niche)

    severity = classified.get("severity")
    confidence = _confidence_for_issue(classified, severity)

    return {
        "key": classified["key"],
        "category": category,
        "short_bullet": short_bullet,
        "expanded_sentence": expanded,
        "evidence_sentence": evidence,
        "theme": classified["theme"],
        "themes": classified["themes"],
        "source": classified["source"],
        "severity": severity,
        "confidence": confidence,
        "priority": classified["priority"],
        "technical_only": classified["technical_only"],
        "mobile_cta": classified["mobile_cta"],
        "manual_visual": classified["manual_visual"],
        "reason": _issue_value(issue, "reason"),
        "points": _issue_value(issue, "points"),
        "step": step,
    }


def select_email_issues(
    issues: list[Any],
    step: int,
    business_context: dict[str, Any],
) -> list[dict[str, Any]]:
    humanized = [humanize_issue(issue, business_context, step) for issue in issues]
    humanized.sort(
        key=lambda item: (
            -int(bool(item.get("manual_visual"))),
            -_int_value(item.get("severity"), 0),
            -_int_value(item.get("priority"), 0),
            str(item.get("key") or ""),
        )
    )

    if step == 1:
        return _select_step_one(humanized, business_context)
    if step == 2:
        return _select_by_categories(humanized, STEP_2_CATEGORIES, limit=3)
    if step == 3:
        return _select_by_categories(humanized, STEP_3_CATEGORIES, limit=3)
    return _select_with_technical_limit(humanized, limit=3)[:3]


def build_opening_line(
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
    step: int,
) -> str:
    return _pick_opening_candidate(business_context, selected_issues, step)["line"]


def build_packet_line(public_packet_url: str, step: int) -> str:
    if not public_packet_url:
        return ""
    options = [
        f"I put the short version here:\n{public_packet_url}",
        f"I marked up the main points here:\n{public_packet_url}",
        f"I made a private page with the screenshots and notes here:\n{public_packet_url}",
    ]
    if step > 1:
        options = [
            f"The short page is here:\n{public_packet_url}",
            f"The screenshots and main points are still here:\n{public_packet_url}",
        ]
    return stable_choice(options, [public_packet_url, step, "packet_line"])


def build_issue_bullets(selected_issues: list[dict[str, Any]], step: int) -> list[str]:
    _ = step
    return [_string(issue.get("short_bullet")) for issue in selected_issues if _string(issue.get("short_bullet"))]


def build_walkthrough_ask(step: int, business_context: dict[str, Any]) -> str:
    seed = _seed_parts(business_context, step)
    if step == 4:
        return stable_choice(
            [
                "If tightening this path is useful, reply and I can walk through it.",
                "If this is worth a look, I can walk through the fixes.",
                "If it would help, I can send the short walkthrough.",
            ],
            seed + ["ask"],
        )
    return stable_choice(
        [
            "Worth a short walkthrough?",
            "Open to a short walkthrough?",
            "Would a short walkthrough be useful?",
        ],
        seed + ["ask"],
    )


def build_subject_options(
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
    step: int,
) -> list[str]:
    return [candidate["subject"] for candidate in _subject_candidates(business_context, selected_issues, step)]


def _subject_candidates(
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
    step: int,
) -> list[dict[str, str]]:
    business_name = business_context.get("business_name") or "your website"
    candidates: list[dict[str, str]] = [
        {"category": "audit_packet", "subject": f"Website notes for {business_name}"},
        {
            "category": "direct",
            "subject": f"A few notes on {business_name}'s website",
        },
        {"category": "soft", "subject": f"Private website notes for {business_name}"},
    ]

    for label in _specific_subject_labels(selected_issues, step):
        candidates.insert(
            1,
            {
                "category": "specific_issue",
                "subject": f"{business_name}: {label}",
            },
        )

    niche = _specific_niche_for_subject(business_context)
    if niche:
        candidates.append(
            {
                "category": "local_niche",
                "subject": f"{niche} website note for {business_name}",
            }
        )

    if len(candidates) < 4:
        candidates.append(
            {
                "category": "specific_issue",
                "subject": f"{business_name}: website path note",
            }
        )

    return _dedupe_subject_candidates(candidates)[:6]


def _specific_subject_labels(selected_issues: list[dict[str, Any]], step: int) -> list[str]:
    labels: list[str] = []
    preferred_by_step = {
        2: ["mobile site note", "call path note"],
        3: ["service page note", "tracking note", "trust note"],
        4: ["call path note", "service page note"],
    }

    for issue in selected_issues:
        label = SUBJECT_SPECIFIC_LABELS.get(_string(issue.get("category")))
        if label:
            labels.append(label)
        elif issue.get("mobile_cta"):
            labels.append("call path note")

    preferred = preferred_by_step.get(step, [])
    ordered = [label for label in preferred if label in labels]
    ordered.extend(label for label in labels if label not in ordered)
    if not ordered and step == 2:
        ordered.append("mobile site note")
    elif not ordered and step == 3:
        ordered.append("service page note")

    return _unique_text(ordered)[:2]


def _specific_niche_for_subject(business_context: dict[str, Any]) -> str:
    niche = _string(business_context.get("niche_label"))
    if not niche or niche == "home-service":
        return ""
    if niche == "HVAC":
        return niche
    return niche[:1].upper() + niche[1:]


def _dedupe_subject_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for candidate in candidates:
        subject = _clean_subject(candidate.get("subject", ""))
        key = subject.lower()
        if not subject or key in seen or _deceptive_subject(subject):
            continue
        seen.add(key)
        output.append({"category": candidate.get("category", "direct"), "subject": subject})
    return output


def _clean_subject(subject: str) -> str:
    clean = " ".join(_string(subject).split())
    clean = re.sub(r"^(re|fwd?):\s*", "", clean, flags=re.I)
    return clean if len(clean) <= 72 else clean[:69].rstrip() + "..."


def _deceptive_subject(subject: str) -> bool:
    lowered = subject.lower()
    blocked = {"urgent", "guaranteed", "audit draft", "rank you", "10x"}
    if lowered.startswith(("re:", "fw:", "fwd:")):
        return True
    if any(term in lowered for term in blocked):
        return True
    if re.search(r"\bfree\b", lowered):
        return True
    return any(ord(char) > 0xFFFF for char in subject)


def _pick_subject_candidate(
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
    step: int,
    rotate_index: int = 0,
) -> dict[str, str]:
    candidates = _subject_candidates(business_context, selected_issues, step)
    if not candidates:
        return {"category": "fallback", "subject": "Website notes"}
    return stable_choice(
        candidates,
        [
            business_context.get("prospect_id"),
            business_context.get("business_name"),
            step,
            rotate_index,
            "subject",
        ],
    )


def _opening_candidates(
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
    step: int,
) -> list[dict[str, str]]:
    business_name = business_context.get("business_name") or "your business"
    visual_theme = _top_visual_theme(selected_issues)
    has_visual = bool(business_context.get("has_visual_review")) or any(
        issue.get("manual_visual") for issue in selected_issues
    )
    has_mobile = any(issue.get("mobile_cta") for issue in selected_issues)
    has_mobile_evidence = has_mobile or bool(business_context.get("has_mobile_screenshot"))
    has_packet = bool(business_context.get("public_packet_url"))
    screenshot_word = (
        "screenshots and notes"
        if business_context.get("has_mobile_screenshot") or business_context.get("has_desktop_screenshot")
        else "main points"
    )
    niche_line = _niche_opening_hint(business_context)

    if step == 1:
        candidates: list[dict[str, str]] = []
        if has_visual and visual_theme:
            candidates.append(
                {
                    "category": "visual_review",
                    "line": f"I looked through {business_name}'s public site, and the {visual_theme} stood out first.",
                }
            )
        if has_mobile:
            candidates.append(
                {
                    "category": "mobile_issue",
                    "line": f"I checked the mobile version of {business_name}'s site and noticed the call/request path could be easier to reach.",
                }
            )
        if niche_line:
            candidates.append(
                {
                    "category": "niche_context",
                    "line": f"I looked through {business_name}'s public site. {niche_line}",
                }
            )
        if has_packet:
            candidates.append(
                {
                    "category": "public_packet",
                    "line": f"I put the short version for {business_name} on a private page so you can see the {screenshot_word} without digging through a long email.",
                }
            )
        candidates.append(
            {
                "category": "safe_fallback",
                "line": f"I looked through {business_name}'s public site and wrote down a few specific changes I would make before rebuilding it.",
            }
        )
        return candidates

    if step == 2:
        candidates = []
        if has_mobile_evidence:
            candidates.append(
                {
                    "category": "mobile_issue",
                    "line": f"I checked the mobile version of {business_name}'s site again, and the call/request path is still where I would start.",
                }
            )
        if has_visual or visual_theme:
            candidates.append(
                {
                    "category": "visual_review",
                    "line": f"For {business_name}, the first screen should make the next step easier to reach from a phone.",
                }
            )
        if niche_line:
            candidates.append(
                {
                    "category": "niche_context",
                    "line": f"I kept looking at {business_name} from a mobile buyer's point of view. {niche_line}",
                }
            )
        candidates.append(
            {
                "category": "safe_fallback",
                "line": f"I looked through {business_name}'s site again, and the next-step path is where I would start.",
            }
        )
        return candidates

    if step == 3:
        return [
            {
                "category": "service_path",
                "line": f"The other place I would tighten {business_name}'s site is the service-page and buyer path after the first screen.",
            },
            {
                "category": "trust_tracking",
                "line": f"For {business_name}, the service, proof, and request path could work harder before someone calls.",
            },
            {
                "category": "safe_fallback",
                "line": f"Beyond the first screen, I would make {business_name}'s service and trust path easier to scan.",
            },
        ]

    return [
        {
            "category": "close_file",
            "line": f"I am going to close the loop on the site notes for {business_name}.",
        },
        {
            "category": "close_file",
            "line": f"Last note from me on {business_name}'s website path.",
        },
        {
            "category": "close_file",
            "line": f"I will keep this short and close out my notes on {business_name}.",
        },
    ]


def _pick_opening_candidate(
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
    step: int,
) -> dict[str, str]:
    candidates = _opening_candidates(business_context, selected_issues, step)
    return stable_choice(candidates, _seed_parts(business_context, step) + ["opening"])


def _top_visual_theme(selected_issues: list[dict[str, Any]]) -> str:
    for issue in selected_issues:
        if not issue.get("manual_visual"):
            continue
        theme = OPENING_THEME_LABELS.get(_string(issue.get("category")))
        if theme:
            return theme
    for issue in selected_issues:
        theme = OPENING_THEME_LABELS.get(_string(issue.get("category")))
        if theme:
            return theme
    return ""


def _niche_opening_hint(business_context: dict[str, Any]) -> str:
    niche = _string(business_context.get("niche_label"))
    if not niche:
        return ""
    hint = NICHE_OPENING_HINTS.get(niche)
    if not hint:
        return ""
    city = _string(business_context.get("primary_city"))
    if city:
        return f"In {city}, {hint[:1].lower()}{hint[1:]}"
    return hint


def pick_subject(
    subject_options: list[str],
    prospect_id: int,
    step: int,
    rotate_index: int = 0,
) -> str:
    if not subject_options:
        return "Website notes"
    return stable_choice(subject_options, [prospect_id, step, rotate_index, "subject"])


def build_outreach_render_context(
    *,
    prospect: dict[str, Any],
    contact: dict[str, Any] | None,
    audits: dict[str, dict[str, Any]],
    artifacts: Any,
    issues: list[Any],
    step: int,
    style: str = DEFAULT_STYLE,
    variant_index: int = 0,
    contacts: Any = None,
) -> dict[str, Any]:
    contact_list = contacts if contacts is not None else ([contact] if contact else [])
    business_context = build_business_context(prospect, audits, artifacts, contact_list)
    if style not in VALID_STYLES:
        style = DEFAULT_STYLE
    business_context["style"] = style
    business_context["variant_index"] = variant_index

    selected_issues = select_email_issues(issues, step, business_context)
    subject_options = build_subject_options(business_context, selected_issues, step)
    subject_candidate = _pick_subject_candidate(
        business_context,
        selected_issues,
        step,
        variant_index,
    )
    subject = subject_candidate["subject"]
    opening_candidate = _pick_opening_candidate(business_context, selected_issues, step)
    public_packet_url = business_context.get("public_packet_url") or ""
    include_packet = _truthy(
        business_context["voice_config"]["copy"].get("include_public_packet_in_draft"),
        default=True,
    )
    packet_line = build_packet_line(public_packet_url, step) if include_packet else ""
    if packet_line and opening_candidate.get("category") == "public_packet":
        packet_line = f"Here it is:\n{public_packet_url}"

    context = {
        "subject": subject,
        "subject_options": subject_options,
        "selected_subject_category": subject_candidate["category"],
        "selected_opening_category": opening_candidate["category"],
        "business_name": business_context["business_name"],
        "contact_name": business_context["contact_name"],
        "contact_first_name": business_context["contact_first_name"],
        "recipient_email": business_context["recipient_email"],
        "website_url": business_context["website_url"],
        "domain": business_context["domain"],
        "market": business_context["market"],
        "niche": business_context["niche"],
        "niche_label": business_context["niche_label"],
        "primary_city": business_context["primary_city"],
        "public_packet_url": public_packet_url,
        "packet_line": packet_line,
        "issues": selected_issues,
        "selected_issues": selected_issues,
        "issue_bullets": build_issue_bullets(selected_issues, step),
        "opening_line": opening_candidate["line"],
        "issue_intro_line": _issue_intro_line(step),
        "interpretation_line": _interpretation_line(step, business_context, selected_issues),
        "walkthrough_ask": build_walkthrough_ask(step, business_context),
        "opt_out_line": 'P.S. Not relevant, no problem. Reply "not interested" and I will not follow up.',
        "sender_name": business_context["sender_name"],
        "sender_company": business_context["sender_company"],
        "sender_title": business_context["sender_title"],
        "style": style,
        "variant_index": variant_index,
        "step": step,
        "all_issue_count": len(issues),
        "issue_count_total": len(issues),
        "missing_public_packet_url": not bool(public_packet_url),
        "business_context": business_context,
        "copy_quality_flags": [],
    }
    return context


def detect_copy_quality_flags(body: str, context: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    lowered = body.lower()
    banned = context.get("business_context", {}).get("voice_config", {}).get("copy", {}).get(
        "banned_phrases",
        DEFAULT_BANNED_PHRASES,
    )
    for phrase in banned:
        phrase_text = _string(phrase)
        if phrase_text and phrase_text.lower() in lowered:
            flags.append(f"contains_banned_phrase:{phrase_text}")

    if "guaranteed" in lowered:
        flags.append("contains_guaranteed")
    if int(context.get("step") or 0) == 1 and not context.get("public_packet_url"):
        flags.append("public_packet_missing_step_1")
    if int(context.get("step") or 0) == 1 and _word_count(body) > 220:
        flags.append("step_1_over_220_words")

    bullet_count = sum(1 for line in body.splitlines() if line.strip().startswith("- "))
    if bullet_count > 5:
        flags.append("more_than_5_issue_bullets")
    if context.get("business_name") and _string(context.get("business_name")).lower() not in lowered:
        flags.append("no_business_name")
    if bullet_count == 0:
        flags.append("no_specific_issue_bullets")
    opt_out_line = _string(context.get("opt_out_line"))
    if opt_out_line and opt_out_line.lower() not in lowered:
        flags.append("no_opt_out_line_variable")
    if not _string(context.get("sender_name")):
        flags.append("no_sender_name")
    if "{{" in body or "}}" in body:
        flags.append("unresolved_template_variable")

    return _unique_text(flags)


def _select_step_one(humanized: list[dict[str, Any]], business_context: dict[str, Any]) -> list[dict[str, Any]]:
    limit = business_context.get("voice_config", {}).get("copy", {}).get("step1_max_issues", 4)
    limit = max(3, min(4, _int_value(limit, 4)))
    selected: list[dict[str, Any]] = []

    mobile = next((issue for issue in humanized if issue.get("mobile_cta")), None)
    if mobile:
        _append_issue(selected, mobile)

    for issue in humanized:
        if issue.get("manual_visual"):
            _append_issue(selected, issue)
        if len(selected) >= limit:
            break

    for issue in humanized:
        if len(selected) >= limit:
            break
        _append_issue(selected, issue)

    return _select_with_technical_limit(selected, limit=limit)


def _select_by_categories(
    humanized: list[dict[str, Any]],
    categories: set[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    selected = [issue for issue in humanized if issue.get("category") in categories]
    if len(selected) < min(limit, len(humanized)):
        for issue in humanized:
            if len(selected) >= limit:
                break
            _append_issue(selected, issue)
    return _select_with_technical_limit(selected, limit=limit)


def _select_with_technical_limit(humanized: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    technical_count = 0
    for issue in humanized:
        if len(selected) >= limit:
            break
        technical = bool(issue.get("technical_only"))
        severe = _int_value(issue.get("severity"), 0) >= 4 or issue.get("category") == "pagespeed"
        if technical and technical_count >= 1 and not severe:
            continue
        if _append_issue(selected, issue) and technical:
            technical_count += 1
    return selected[:limit]


def _append_issue(selected: list[dict[str, Any]], issue: dict[str, Any]) -> bool:
    key = issue.get("key")
    category = issue.get("category")
    if any(existing.get("key") == key or existing.get("category") == category for existing in selected):
        return False
    selected.append(issue)
    return True


def _issue_intro_line(step: int) -> str:
    if step == 1:
        return "I would start with:"
    if step == 2:
        return "The mobile fixes I would start with:"
    if step == 3:
        return "The service-path fixes I would start with:"
    return "The short version:"


def _interpretation_line(
    step: int,
    business_context: dict[str, Any],
    selected_issues: list[dict[str, Any]],
) -> str:
    niche = business_context.get("niche_label") or "home-service"
    business_name = business_context.get("business_name") or "the site"
    if step == 1:
        if niche in {"roofing", "HVAC", "plumbing", "electrical", "garage doors", "pest control", "tree service", "restoration"}:
            return (
                f"For {_article_for_niche(niche)} {niche} site, the main thing is making it easier for someone on a phone "
                "to understand the service, trust the company, and request help."
            )
        return (
            "The site does not need to sound fancier. It needs a cleaner path from "
            '"what do you do?" to "how do I request help?"'
        )
    if step == 2:
        return "A phone visitor should not have to hunt for the next step."
    if step == 3:
        return "Those pages can do more of the sorting before someone calls."
    return f"These are the few items I would tighten first on {business_name}."


def _style_for_context(context: dict[str, Any]) -> str:
    style = _string(context.get("style"))
    return style if style in VALID_STYLES else DEFAULT_STYLE


def _fallback_issue_sentence(issue: Any) -> str:
    claim = _string(_issue_value(issue, "claim"))
    if claim:
        return _sanitize_internal_language(claim)
    return "The site has a fixable website path issue worth tightening."


def _sanitize_internal_language(value: str) -> str:
    lead_score_phrase = "The " + "lead-score audit recorded"
    replacements = {
        "The site audit did not find": "I did not see",
        "The site audit did not verify": "I did not see",
        "The site audit did not detect": "I did not see",
        "The audit shows": "The stored signal shows",
        lead_score_phrase: "The stored review flagged",
        "detected": "found",
        "conversion issue": "website path issue",
    }
    output = value
    for old, new in replacements.items():
        output = re.sub(re.escape(old), new, output, flags=re.I)
    return output


def _direct_sentence(value: str) -> str:
    return value.replace("could be", "should be").replace("may feel", "feels")


def _technical_sentence(value: str, classified: dict[str, Any]) -> str:
    category = classified.get("category")
    if category == "pagespeed":
        return "The speed data points to image/script and first-screen performance work."
    if category == "no_analytics":
        return "The public page source did not show common analytics tags for call/form attribution."
    if category == "no_schema":
        return "The public page source did not show basic local business/service schema."
    return value


def _add_niche_hint(value: str, niche: str) -> str:
    if niche in {"home-service", "service"}:
        return value
    if niche.lower() in value.lower():
        return value
    return f"{value} For {niche}, that path matters."


def _confidence_for_issue(classified: dict[str, Any], severity: Any) -> str:
    if classified.get("manual_visual") and _int_value(severity, 0) >= 4:
        return "high"
    if classified.get("category") in {"no_tel_link", "no_form", "no_service_pages", "pagespeed"}:
        return "medium"
    if classified.get("technical_only"):
        return "medium"
    return "medium" if classified.get("manual_visual") else "low"


def _subject_length_guard(options: list[str]) -> list[str]:
    output = []
    for option in options:
        clean = " ".join(option.split())
        output.append(clean if len(clean) <= 72 else clean[:69].rstrip() + "...")
    return _unique_text(output)


def _seed_parts(context: dict[str, Any], step: int) -> list[Any]:
    return [
        context.get("prospect_id"),
        context.get("business_name"),
        step,
        context.get("variant_index", 0),
        context.get("style", DEFAULT_STYLE),
    ]


def _first_contact(contacts: Any) -> dict[str, Any] | None:
    if isinstance(contacts, dict):
        return contacts
    if isinstance(contacts, list) and contacts:
        first = contacts[0]
        return first if isinstance(first, dict) else None
    return None


def _contact_first_name(value: Any) -> str:
    name = _string(value)
    if not name or "@" in name:
        return ""
    token = re.split(r"[\s,]+", name.strip(), maxsplit=1)[0].strip(" .")
    if not token or token.lower() in GENERIC_FIRST_NAMES:
        return ""
    if not re.search(r"[A-Za-z]", token):
        return ""
    return token[:40]


def _domain_for(prospect: dict[str, Any]) -> str:
    domain = _string(prospect.get("domain")).lower()
    if domain:
        return domain[4:] if domain.startswith("www.") else domain
    url = _string(prospect.get("website_url"))
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _normalize_artifacts(artifacts: Any) -> dict[str, list[dict[str, Any]]]:
    if not artifacts:
        return {}
    if isinstance(artifacts, dict):
        normalized: dict[str, list[dict[str, Any]]] = {}
        for key, value in artifacts.items():
            if isinstance(value, list):
                normalized[str(key)] = [item for item in value if isinstance(item, dict)]
            elif isinstance(value, dict):
                normalized[str(key)] = [value]
        return normalized
    if isinstance(artifacts, list):
        normalized = {}
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_type = _string(artifact.get("artifact_type"))
            if artifact_type:
                normalized.setdefault(artifact_type, []).append(artifact)
        return normalized
    return {}


def _latest_ready_artifact(
    artifacts: dict[str, list[dict[str, Any]]],
    artifact_type: str,
) -> dict[str, Any] | None:
    candidates = artifacts.get(artifact_type) or []
    ready = [item for item in candidates if _string(item.get("status")).lower() == "ready"]
    return (ready or candidates)[-1] if (ready or candidates) else None


def _artifact_ready(artifacts: dict[str, list[dict[str, Any]]], artifact_type: str) -> bool:
    artifact = _latest_ready_artifact(artifacts, artifact_type)
    return bool(artifact and _string(artifact.get("status")).lower() == "ready")


def _audit_has_score(audit: dict[str, Any] | None) -> bool:
    if not audit:
        return False
    if audit.get("score") not in (None, ""):
        return True
    findings = audit.get("findings")
    return isinstance(findings, dict) and findings.get("performance_score") not in (None, "")


def _issue_value(issue: Any, key: str) -> Any:
    if isinstance(issue, dict):
        return issue.get(key)
    return getattr(issue, key, None)


def _json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    return str(value).strip()


def _env_value(key: str) -> str:
    return _string(os.environ.get(key))


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_value(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = _string(value)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _title_place(value: str) -> str:
    return " ".join(part.capitalize() if not part.isupper() else part for part in value.replace("_", " ").split())


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w']+\b", value))


def _article_for_niche(niche: str) -> str:
    return "an" if niche[:1].lower() in {"a", "e", "i", "o", "u"} or niche == "HVAC" else "a"
