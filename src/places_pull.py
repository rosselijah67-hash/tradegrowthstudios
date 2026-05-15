"""Pull local business prospects from Google Places Text Search (New)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import db
from .cli_utils import build_parser, finish_command, setup_command
from .config import load_yaml_config
from .franchise_filter import check_franchise_exclusion
from .state import ProspectStatus


COMMAND = "places_pull"
GOOGLE_PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.primaryType",
        "places.types",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
    ]
)
SOURCE = "google_places"
DEFAULT_MIN_REVIEWS = 10
DEFAULT_LIMIT = 20
REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGE_SIZE = 20

PROTECTED_LIFECYCLE_STATUSES = {
    "CLOSED_WON",
    "CLOSED_LOST",
    "PROJECT_ACTIVE",
    "PROJECT_COMPLETE",
    "OUTREACH_SENT",
    "CONTACT_MADE",
    "CALL_BOOKED",
    "PROPOSAL_SENT",
}


@dataclass
class PullSummary:
    api_queries_made: int = 0
    raw_places_returned: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates_skipped: int = 0
    disqualified: int = 0
    processed: int = 0


def build_arg_parser():
    parser = build_parser("Pull local business prospects from Google Places Text Search (New).")
    for action in parser._actions:
        if action.dest == "limit":
            action.default = DEFAULT_LIMIT
            action.help = (
                "Maximum places to process. Defaults to 20 for paid API cost control."
            )
            break
    return parser


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _json_loads(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        import json

        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 10 else None


def _extract_display_name(place: dict[str, Any]) -> str | None:
    display_name = place.get("displayName")
    if isinstance(display_name, dict):
        return _clean(display_name.get("text"))
    return _clean(display_name)


def _guess_city_state(
    formatted_address: str | None, fallback_state: str | None
) -> tuple[str | None, str | None]:
    if not formatted_address:
        return None, fallback_state

    parts = [part.strip() for part in formatted_address.split(",") if part.strip()]
    if len(parts) >= 3:
        city = parts[-3]
        state_zip = parts[-2].split()
        state = state_zip[0] if state_zip else fallback_state
        return city, state

    return None, fallback_state


def _state_for_market(market_config: dict[str, Any]) -> str | None:
    configured = _clean(market_config.get("state"))
    if configured:
        return configured

    center_city = _clean(market_config.get("center_city"))
    if center_city and "," in center_city:
        maybe_state = center_city.rsplit(",", 1)[1].strip().split()
        if maybe_state:
            return maybe_state[0]

    label = _clean(market_config.get("label"))
    if label and "," in label:
        maybe_state = label.rsplit(",", 1)[1].strip().split()
        if maybe_state:
            return maybe_state[0]

    return None


def _included_cities(market_config: dict[str, Any]) -> list[str]:
    raw_cities = market_config.get("included_cities") or market_config.get("cities")
    if isinstance(raw_cities, list):
        cities = [_clean(city) for city in raw_cities]
        return [city for city in cities if city]

    center_city = _clean(market_config.get("center_city"))
    if center_city:
        return [center_city.split(",", 1)[0].strip()]

    return []


def _load_market_and_niche(market_key: str | None, niche_key: str | None) -> tuple[dict, dict]:
    if not market_key:
        raise SystemExit("--market is required for places_pull")
    if not niche_key:
        raise SystemExit("--niche is required for places_pull")

    markets = load_yaml_config("markets.yaml").get("markets", {})
    niches = load_yaml_config("niches.yaml").get("niches", {})

    market_config = markets.get(market_key)
    if not isinstance(market_config, dict):
        available = ", ".join(sorted(markets)) or "none"
        raise SystemExit(f"Unknown market '{market_key}'. Available markets: {available}")

    niche_config = niches.get(niche_key)
    if not isinstance(niche_config, dict):
        available = ", ".join(sorted(niches)) or "none"
        raise SystemExit(f"Unknown niche '{niche_key}'. Available niches: {available}")

    if not _included_cities(market_config):
        raise SystemExit(f"Market '{market_key}' needs included_cities or cities.")
    if not niche_config.get("search_terms"):
        raise SystemExit(f"Niche '{niche_key}' needs search_terms.")

    return market_config, niche_config


def _text_search(
    *,
    api_key: str,
    query: str,
    page_size: int,
    country: str,
) -> list[dict[str, Any]]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before running places_pull.") from exc

    response = requests.post(
        GOOGLE_PLACES_TEXT_SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": GOOGLE_PLACES_FIELD_MASK,
        },
        json={
            "textQuery": query,
            "pageSize": page_size,
            "regionCode": country,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    places = payload.get("places", [])
    return places if isinstance(places, list) else []


def _qualification_status(
    *,
    business_name: str | None,
    place_id: str | None,
    formatted_address: str | None,
    business_status: str | None,
    domain: str | None,
    user_rating_count: int,
    min_reviews: int,
    franchise_exclusion: dict[str, Any],
) -> tuple[str, str | None]:
    if franchise_exclusion.get("is_hard_exclude"):
        match = (
            franchise_exclusion.get("matched_name")
            or franchise_exclusion.get("matched_domain")
            or franchise_exclusion.get("matched_regex")
            or "configured_exclusion"
        )
        return "DISQUALIFIED", f"franchise_exclusion:{match}"
    if not business_name or not place_id or not formatted_address:
        return "DISQUALIFIED", "missing_minimum_fields"
    if business_status and business_status != "OPERATIONAL":
        return "DISQUALIFIED", "not_operational"
    if not domain:
        return "DISQUALIFIED", "missing_website"
    if user_rating_count < min_reviews:
        return "DISQUALIFIED", "too_few_reviews"

    return "DISCOVERED", None


def _normalize_place(
    *,
    place: dict[str, Any],
    market_key: str,
    niche_key: str,
    city: str,
    state: str | None,
    search_term: str,
    query: str,
    min_reviews: int,
) -> dict[str, Any]:
    place_id = _clean(place.get("id"))
    business_name = _extract_display_name(place)
    formatted_address = _clean(place.get("formattedAddress"))
    website_url = _clean(place.get("websiteUri"))
    domain = db.normalize_domain(website_url)
    phone = _normalize_phone(_clean(place.get("nationalPhoneNumber")))
    city_guess, state_guess = _guess_city_state(formatted_address, state)
    user_rating_count = int(place.get("userRatingCount") or 0)
    rating = place.get("rating")
    primary_type = _clean(place.get("primaryType"))
    types = place.get("types") if isinstance(place.get("types"), list) else []
    business_status = _clean(place.get("businessStatus"))
    franchise_exclusion = check_franchise_exclusion(
        {
            "business_name": business_name,
            "domain": domain,
            "website_url": website_url,
            "types_json": types,
        }
    )
    qualification_status, disqualification_reason = _qualification_status(
        business_name=business_name,
        place_id=place_id,
        formatted_address=formatted_address,
        business_status=business_status,
        domain=domain,
        user_rating_count=user_rating_count,
        min_reviews=min_reviews,
        franchise_exclusion=franchise_exclusion,
    )

    source_id = place_id or db.stable_hash(
        "|".join([business_name or "", domain or "", phone or "", formatted_address or ""])
    )[:24]

    return {
        "prospect_key": f"{SOURCE}:{source_id}",
        "source": SOURCE,
        "source_id": source_id,
        "place_id": place_id,
        "business_name": business_name or f"Unknown business {source_id}",
        "market": market_key,
        "niche": niche_key,
        "address": formatted_address,
        "formatted_address": formatted_address,
        "city": city_guess or city,
        "state": state_guess or state,
        "city_guess": city_guess or city,
        "state_guess": state_guess or state,
        "phone": phone,
        "website_url": website_url,
        "domain": domain,
        "rating": rating,
        "user_rating_count": user_rating_count,
        "primary_type": primary_type,
        "types": types,
        "business_status": business_status,
        "status": (
            ProspectStatus.INELIGIBLE
            if qualification_status == "DISQUALIFIED"
            else ProspectStatus.NEW
        ),
        "qualification_status": qualification_status,
        "next_action": "DISCARD" if qualification_status == "DISQUALIFIED" else None,
        "metadata": {
            "search_city": city,
            "search_term": search_term,
            "search_query": query,
            "disqualification_reason": disqualification_reason,
            "franchise_exclusion": franchise_exclusion,
        },
    }


def _dedupe_tokens(prospect: dict[str, Any]) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    if prospect.get("place_id"):
        tokens.append(("place_id", prospect["place_id"]))
    if prospect.get("domain"):
        tokens.append(("domain", prospect["domain"]))
    if prospect.get("phone"):
        tokens.append(("phone", prospect["phone"]))
    return tokens


def _already_seen(prospect: dict[str, Any], seen_tokens: set[tuple[str, str]]) -> bool:
    return any(token in seen_tokens for token in _dedupe_tokens(prospect))


def _remember(prospect: dict[str, Any], seen_tokens: set[tuple[str, str]]) -> None:
    seen_tokens.update(_dedupe_tokens(prospect))


def _find_existing_prospect(connection, prospect: dict[str, Any]) -> dict[str, Any] | None:
    place_id = prospect.get("place_id")
    if place_id:
        row = connection.execute(
            """
            SELECT * FROM prospects
            WHERE place_id = ?
               OR (source = ? AND source_id = ?)
            ORDER BY id
            LIMIT 1
            """,
            (place_id, SOURCE, place_id),
        ).fetchone()
        if row:
            return db.row_to_dict(row)

    domain = prospect.get("domain")
    if domain:
        row = connection.execute(
            """
            SELECT * FROM prospects
            WHERE domain = ?
            ORDER BY id
            LIMIT 1
            """,
            (domain,),
        ).fetchone()
        if row:
            return db.row_to_dict(row)

    phone = prospect.get("phone")
    if phone:
        row = connection.execute(
            """
            SELECT * FROM prospects
            WHERE phone = ?
            ORDER BY id
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
        if row:
            return db.row_to_dict(row)

    return None


def _save_prospect(connection, prospect: dict[str, Any], existing: dict[str, Any] | None) -> None:
    if existing:
        existing_metadata = _json_loads(existing.get("metadata_json"), {})
        if not isinstance(existing_metadata, dict):
            existing_metadata = {}
        prospect_metadata = prospect.get("metadata") if isinstance(prospect.get("metadata"), dict) else {}
        merged_metadata = dict(existing_metadata)
        merged_metadata.update(prospect_metadata)
        if existing_metadata.get("franchise_override"):
            exclusion = merged_metadata.get("franchise_exclusion")
            if isinstance(exclusion, dict):
                exclusion["manual_override"] = True
            prospect["status"] = existing.get("status")
            prospect["qualification_status"] = existing.get("qualification_status")
            prospect["next_action"] = None
        prospect["metadata"] = merged_metadata
        if str(existing.get("status") or "").upper() in PROTECTED_LIFECYCLE_STATUSES:
            prospect["status"] = existing.get("status")
            prospect["qualification_status"] = existing.get("qualification_status")
            prospect["next_action"] = None
        prospect = {**prospect, "prospect_key": existing["prospect_key"]}
    db.upsert_prospect(connection, prospect)


def _log_dry_run(context, prospect: dict[str, Any], existing: dict[str, Any] | None) -> None:
    action = "update" if existing else "insert"
    context.logger.info(
        f"prospect_would_{action}",
        extra={
            "event": f"prospect_would_{action}",
            "place_id": prospect.get("place_id"),
            "business_name": prospect.get("business_name"),
            "market": prospect.get("market"),
            "niche": prospect.get("niche"),
            "city": prospect.get("city_guess"),
            "website_url": prospect.get("website_url"),
            "phone": prospect.get("phone"),
            "rating": prospect.get("rating"),
            "user_rating_count": prospect.get("user_rating_count"),
            "qualification_status": prospect.get("qualification_status"),
            "disqualification_reason": prospect.get("metadata", {}).get(
                "disqualification_reason"
            ),
            "franchise_exclusion": prospect.get("metadata", {}).get("franchise_exclusion"),
        },
    )


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    context = setup_command(args, COMMAND)

    api_key = _clean(os.environ.get("GOOGLE_MAPS_API_KEY"))
    if not api_key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is required in .env for places_pull")

    market_config, niche_config = _load_market_and_niche(args.market, args.niche)
    cities = _included_cities(market_config)
    state = _state_for_market(market_config)
    country = _clean(market_config.get("country")) or "US"
    search_terms = [_clean(term) for term in niche_config.get("search_terms", [])]
    search_terms = [term for term in search_terms if term]
    min_reviews = int(niche_config.get("minimum_review_count") or DEFAULT_MIN_REVIEWS)
    summary = PullSummary()
    seen_tokens: set[tuple[str, str]] = set()

    connection = db.init_db(args.db_path)

    try:
        for city in cities:
            for search_term in search_terms:
                if args.limit is not None and summary.processed >= args.limit:
                    break

                query_location = f"{city} {state}" if state else city
                query = f"{search_term} in {query_location}"
                remaining = (
                    MAX_PAGE_SIZE
                    if args.limit is None
                    else min(MAX_PAGE_SIZE, args.limit - summary.processed)
                )
                if remaining <= 0:
                    break

                places = _text_search(
                    api_key=api_key,
                    query=query,
                    page_size=remaining,
                    country=country,
                )
                summary.api_queries_made += 1
                summary.raw_places_returned += len(places)
                context.logger.info(
                    "places_query_finished",
                    extra={
                        "event": "places_query_finished",
                        "query": query,
                        "places_returned": len(places),
                    },
                )

                for place in places:
                    if args.limit is not None and summary.processed >= args.limit:
                        break

                    summary.processed += 1
                    prospect = _normalize_place(
                        place=place,
                        market_key=args.market,
                        niche_key=args.niche,
                        city=city,
                        state=state,
                        search_term=search_term,
                        query=query,
                        min_reviews=min_reviews,
                    )

                    if prospect["qualification_status"] == "DISQUALIFIED":
                        summary.disqualified += 1

                    if _already_seen(prospect, seen_tokens):
                        summary.duplicates_skipped += 1
                        context.logger.info(
                            "duplicate_place_skipped",
                            extra={
                                "event": "duplicate_place_skipped",
                                "place_id": prospect.get("place_id"),
                                "business_name": prospect.get("business_name"),
                                "domain": prospect.get("domain"),
                                "phone": prospect.get("phone"),
                            },
                        )
                        continue

                    _remember(prospect, seen_tokens)
                    existing = _find_existing_prospect(connection, prospect)

                    if args.dry_run:
                        _log_dry_run(context, prospect, existing)
                    else:
                        _save_prospect(connection, prospect, existing)
                        connection.commit()

                    if existing:
                        summary.updated += 1
                    else:
                        summary.inserted += 1
    finally:
        connection.close()

    finish_command(
        context,
        api_queries_made=summary.api_queries_made,
        raw_places_returned=summary.raw_places_returned,
        inserted=summary.inserted,
        updated=summary.updated,
        duplicates_skipped=summary.duplicates_skipped,
        disqualified=summary.disqualified,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
