"""SQLite persistence for the local lead-generation pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import get_database_path, load_env, project_path
from .state import ProspectStatus, QualificationStatus


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    place_id TEXT,
    business_name TEXT NOT NULL,
    market TEXT,
    niche TEXT,
    address TEXT,
    formatted_address TEXT,
    city TEXT,
    state TEXT,
    city_guess TEXT,
    state_guess TEXT,
    postal_code TEXT,
    phone TEXT,
    website_url TEXT,
    domain TEXT,
    rating REAL,
    user_rating_count INTEGER,
    primary_type TEXT,
    types_json TEXT NOT NULL DEFAULT '[]',
    business_status TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    qualification_status TEXT NOT NULL DEFAULT 'DISCOVERED',
    business_viability_score INTEGER NOT NULL DEFAULT 0,
    business_eligibility_score INTEGER NOT NULL DEFAULT 0,
    website_pain_score INTEGER NOT NULL DEFAULT 0,
    contactability_score INTEGER NOT NULL DEFAULT 0,
    data_availability_score INTEGER NOT NULL DEFAULT 0,
    market_fit_score INTEGER NOT NULL DEFAULT 0,
    expected_close_score INTEGER NOT NULL DEFAULT 0,
    score_explanation_json TEXT NOT NULL DEFAULT '{}',
    audit_data_status TEXT NOT NULL DEFAULT 'PENDING',
    human_review_status TEXT NOT NULL DEFAULT 'PENDING',
    human_review_decision TEXT,
    human_review_score INTEGER,
    human_review_notes TEXT,
    human_reviewed_at TEXT,
    next_action TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_prospects_market_niche
    ON prospects(market, niche);

CREATE INDEX IF NOT EXISTS idx_prospects_domain
    ON prospects(domain);

CREATE TABLE IF NOT EXISTS website_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_key TEXT NOT NULL UNIQUE,
    prospect_id INTEGER NOT NULL,
    audit_type TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    score INTEGER,
    summary TEXT,
    findings_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    audited_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (prospect_id) REFERENCES prospects(id) ON DELETE CASCADE,
    UNIQUE(prospect_id, audit_type)
);

CREATE INDEX IF NOT EXISTS idx_website_audits_prospect
    ON website_audits(prospect_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_key TEXT NOT NULL UNIQUE,
    prospect_id INTEGER,
    artifact_type TEXT NOT NULL,
    path TEXT,
    artifact_url TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (prospect_id) REFERENCES prospects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_prospect
    ON artifacts(prospect_id);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_key TEXT NOT NULL UNIQUE,
    prospect_id INTEGER NOT NULL,
    contact_type TEXT NOT NULL DEFAULT 'business',
    name TEXT,
    role TEXT,
    email TEXT,
    phone TEXT,
    source TEXT,
    confidence REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (prospect_id) REFERENCES prospects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contacts_prospect
    ON contacts(prospect_id);

CREATE TABLE IF NOT EXISTS outreach_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    prospect_id INTEGER NOT NULL,
    contact_id INTEGER,
    campaign_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    subject TEXT,
    body_path TEXT,
    provider_message_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    scheduled_for TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (prospect_id) REFERENCES prospects(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_outreach_events_prospect
    ON outreach_events(prospect_id);

CREATE TABLE IF NOT EXISTS outreach_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_key TEXT NOT NULL UNIQUE,
    prospect_id INTEGER NOT NULL,
    contact_id INTEGER,
    email TEXT NOT NULL,
    campaign TEXT NOT NULL,
    step INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    send_after TEXT,
    subject TEXT,
    draft_artifact_id INTEGER,
    public_packet_artifact_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (prospect_id) REFERENCES prospects(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
    FOREIGN KEY (draft_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL,
    FOREIGN KEY (public_packet_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_outreach_queue_status
    ON outreach_queue(status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_queue_active_unique
    ON outreach_queue(prospect_id, email, campaign, step)
    WHERE status <> 'cancelled';

CREATE TABLE IF NOT EXISTS suppression_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suppression_type TEXT NOT NULL,
    value TEXT NOT NULL,
    reason TEXT,
    source TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(suppression_type, value)
);

CREATE INDEX IF NOT EXISTS idx_suppression_list_value
    ON suppression_list(value);

CREATE TABLE IF NOT EXISTS dashboard_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT UNIQUE NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    market TEXT,
    niche TEXT,
    limit_count INTEGER,
    dry_run INTEGER NOT NULL DEFAULT 1,
    command_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    log_path TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dashboard_jobs_status
    ON dashboard_jobs(status);

CREATE INDEX IF NOT EXISTS idx_dashboard_jobs_created
    ON dashboard_jobs(created_at);
"""


PROSPECT_COLUMN_MIGRATIONS = {
    "place_id": "TEXT",
    "formatted_address": "TEXT",
    "city_guess": "TEXT",
    "state_guess": "TEXT",
    "rating": "REAL",
    "user_rating_count": "INTEGER",
    "primary_type": "TEXT",
    "types_json": "TEXT NOT NULL DEFAULT '[]'",
    "business_status": "TEXT",
    "qualification_status": "TEXT NOT NULL DEFAULT 'DISCOVERED'",
    "business_viability_score": "INTEGER NOT NULL DEFAULT 0",
    "business_eligibility_score": "INTEGER NOT NULL DEFAULT 0",
    "website_pain_score": "INTEGER NOT NULL DEFAULT 0",
    "contactability_score": "INTEGER NOT NULL DEFAULT 0",
    "data_availability_score": "INTEGER NOT NULL DEFAULT 0",
    "market_fit_score": "INTEGER NOT NULL DEFAULT 0",
    "expected_close_score": "INTEGER NOT NULL DEFAULT 0",
    "score_explanation_json": "TEXT NOT NULL DEFAULT '{}'",
    "audit_data_status": "TEXT NOT NULL DEFAULT 'PENDING'",
    "human_review_status": "TEXT NOT NULL DEFAULT 'PENDING'",
    "human_review_decision": "TEXT",
    "human_review_score": "INTEGER",
    "human_review_notes": "TEXT",
    "human_reviewed_at": "TEXT",
    "next_action": "TEXT",
}


ARTIFACT_COLUMN_MIGRATIONS = {
    "artifact_url": "TEXT",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True)


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_domain(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    hostname = parsed.hostname
    if not hostname:
        return None
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_database_path()
    if not path.is_absolute():
        path = project_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    connection = connect(db_path)
    connection.executescript(SCHEMA_SQL)
    ensure_prospect_place_columns(connection)
    ensure_artifact_columns(connection)
    connection.commit()
    return connection


def ensure_prospect_place_columns(connection: sqlite3.Connection) -> None:
    """Add Places puller columns to older local databases."""

    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(prospects)").fetchall()
    }
    for column_name, column_definition in PROSPECT_COLUMN_MIGRATIONS.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE prospects ADD COLUMN {column_name} {column_definition}"
            )

    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_prospects_place_id
        ON prospects(place_id)
        WHERE place_id IS NOT NULL AND place_id <> ''
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_prospects_phone ON prospects(phone)")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prospects_qualification
        ON prospects(qualification_status)
        """
    )


def ensure_artifact_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
    }
    for column_name, column_definition in ARTIFACT_COLUMN_MIGRATIONS.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE artifacts ADD COLUMN {column_name} {column_definition}"
            )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def upsert_prospect(connection: sqlite3.Connection, data: dict[str, Any]) -> int:
    now = utc_now()
    source = data.get("source") or "local"
    source_id = data.get("source_id") or stable_hash(
        "|".join(
            [
                data.get("business_name") or "",
                data.get("website_url") or "",
                data.get("phone") or "",
                data.get("address") or "",
            ]
        )
    )[:24]
    prospect_key = data.get("prospect_key") or f"{source}:{source_id}"
    domain = data.get("domain") or normalize_domain(data.get("website_url"))

    params = {
        "prospect_key": prospect_key,
        "source": source,
        "source_id": source_id,
        "place_id": data.get("place_id"),
        "business_name": data["business_name"],
        "market": data.get("market"),
        "niche": data.get("niche"),
        "address": data.get("address"),
        "formatted_address": data.get("formatted_address"),
        "city": data.get("city"),
        "state": data.get("state"),
        "city_guess": data.get("city_guess"),
        "state_guess": data.get("state_guess"),
        "postal_code": data.get("postal_code"),
        "phone": data.get("phone"),
        "website_url": data.get("website_url"),
        "domain": domain,
        "rating": data.get("rating"),
        "user_rating_count": data.get("user_rating_count"),
        "primary_type": data.get("primary_type"),
        "types_json": json.dumps(data.get("types") or [], sort_keys=True),
        "business_status": data.get("business_status"),
        "status": data.get("status") or ProspectStatus.NEW,
        "qualification_status": data.get("qualification_status") or QualificationStatus.DISCOVERED,
        "next_action": data.get("next_action"),
        "metadata_json": json_dumps(data.get("metadata")),
        "created_at": now,
        "updated_at": now,
    }

    connection.execute(
        """
        INSERT INTO prospects (
            prospect_key, source, source_id, place_id, business_name, market, niche,
            address, formatted_address, city, state, city_guess, state_guess,
            postal_code, phone, website_url, domain, rating, user_rating_count,
            primary_type, types_json, business_status, status, qualification_status,
            next_action, metadata_json, created_at, updated_at
        ) VALUES (
            :prospect_key, :source, :source_id, :place_id, :business_name, :market, :niche,
            :address, :formatted_address, :city, :state, :city_guess, :state_guess,
            :postal_code, :phone, :website_url, :domain, :rating, :user_rating_count,
            :primary_type, :types_json, :business_status, :status, :qualification_status,
            :next_action, :metadata_json, :created_at, :updated_at
        )
        ON CONFLICT(prospect_key) DO UPDATE SET
            source = excluded.source,
            source_id = excluded.source_id,
            place_id = COALESCE(excluded.place_id, prospects.place_id),
            business_name = excluded.business_name,
            market = COALESCE(excluded.market, prospects.market),
            niche = COALESCE(excluded.niche, prospects.niche),
            address = COALESCE(excluded.address, prospects.address),
            formatted_address = COALESCE(excluded.formatted_address, prospects.formatted_address),
            city = COALESCE(excluded.city, prospects.city),
            state = COALESCE(excluded.state, prospects.state),
            city_guess = COALESCE(excluded.city_guess, prospects.city_guess),
            state_guess = COALESCE(excluded.state_guess, prospects.state_guess),
            postal_code = COALESCE(excluded.postal_code, prospects.postal_code),
            phone = COALESCE(excluded.phone, prospects.phone),
            website_url = COALESCE(excluded.website_url, prospects.website_url),
            domain = COALESCE(excluded.domain, prospects.domain),
            rating = COALESCE(excluded.rating, prospects.rating),
            user_rating_count = COALESCE(excluded.user_rating_count, prospects.user_rating_count),
            primary_type = COALESCE(excluded.primary_type, prospects.primary_type),
            types_json = excluded.types_json,
            business_status = COALESCE(excluded.business_status, prospects.business_status),
            status = excluded.status,
            qualification_status = excluded.qualification_status,
            next_action = COALESCE(excluded.next_action, prospects.next_action),
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        params,
    )
    row = connection.execute(
        "SELECT id FROM prospects WHERE prospect_key = ?", (prospect_key,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Prospect upsert failed for {prospect_key}")
    return int(row["id"])


def upsert_audit(
    connection: sqlite3.Connection,
    *,
    prospect_id: int,
    audit_type: str,
    url: str | None = None,
    status: str = "pending",
    score: int | None = None,
    summary: str | None = None,
    findings: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    audited_at: str | None = None,
) -> int:
    now = utc_now()
    audit_key = f"{prospect_id}:{audit_type}"
    connection.execute(
        """
        INSERT INTO website_audits (
            audit_key, prospect_id, audit_type, url, status, score, summary,
            findings_json, raw_json, audited_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(audit_key) DO UPDATE SET
            url = COALESCE(excluded.url, website_audits.url),
            status = excluded.status,
            score = excluded.score,
            summary = excluded.summary,
            findings_json = excluded.findings_json,
            raw_json = excluded.raw_json,
            audited_at = excluded.audited_at,
            updated_at = excluded.updated_at
        """,
        (
            audit_key,
            prospect_id,
            audit_type,
            url,
            status,
            score,
            summary,
            json_dumps(findings),
            json_dumps(raw),
            audited_at,
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM website_audits WHERE audit_key = ?", (audit_key,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Audit upsert failed for {audit_key}")
    return int(row["id"])


def upsert_artifact(
    connection: sqlite3.Connection,
    *,
    artifact_key: str,
    artifact_type: str,
    prospect_id: int | None = None,
    path: str | None = None,
    artifact_url: str | None = None,
    content_hash: str | None = None,
    status: str = "pending",
    metadata: dict[str, Any] | None = None,
) -> int:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO artifacts (
            artifact_key, prospect_id, artifact_type, path, artifact_url, content_hash,
            status, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_key) DO UPDATE SET
            prospect_id = excluded.prospect_id,
            artifact_type = excluded.artifact_type,
            path = excluded.path,
            artifact_url = excluded.artifact_url,
            content_hash = excluded.content_hash,
            status = excluded.status,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            artifact_key,
            prospect_id,
            artifact_type,
            path,
            artifact_url,
            content_hash,
            status,
            json_dumps(metadata),
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM artifacts WHERE artifact_key = ?", (artifact_key,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Artifact upsert failed for {artifact_key}")
    return int(row["id"])


def upsert_outreach_event(
    connection: sqlite3.Connection,
    *,
    event_key: str,
    prospect_id: int,
    contact_id: int | None = None,
    campaign_key: str,
    channel: str,
    event_type: str,
    status: str,
    subject: str | None = None,
    body_path: str | None = None,
    provider_message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    scheduled_for: str | None = None,
    sent_at: str | None = None,
) -> int:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO outreach_events (
            event_key, prospect_id, contact_id, campaign_key, channel,
            event_type, status, subject, body_path, provider_message_id,
            metadata_json, scheduled_for, sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            event_key,
            prospect_id,
            contact_id,
            campaign_key,
            channel,
            event_type,
            status,
            subject,
            body_path,
            provider_message_id,
            json_dumps(metadata),
            scheduled_for,
            sent_at,
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM outreach_events WHERE event_key = ?", (event_key,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Outreach event upsert failed for {event_key}")
    return int(row["id"])


def fetch_prospects(
    connection: sqlite3.Connection,
    *,
    market: str | None = None,
    niche: str | None = None,
    limit: int | None = None,
    require_website: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []

    if market:
        clauses.append("market = ?")
        params.append(market)
    if niche:
        clauses.append("niche = ?")
        params.append(niche)
    if require_website:
        clauses.append("website_url IS NOT NULL")
        clauses.append("website_url <> ''")

    sql = f"SELECT * FROM prospects WHERE {' AND '.join(clauses)} ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def count_rows(connection: sqlite3.Connection, table_names: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name in table_names:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        counts[table_name] = int(row["count"])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize the SQLite database.")
    parser.add_argument("--db-path", default=None, help="Override DATABASE_PATH.")
    args = parser.parse_args()

    load_env()
    connection = init_db(args.db_path)
    counts = count_rows(
        connection,
        [
            "prospects",
            "website_audits",
            "artifacts",
            "contacts",
            "outreach_events",
            "outreach_queue",
            "suppression_list",
        ],
    )
    print(json.dumps({"database_ready": True, "counts": counts}, sort_keys=True))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
