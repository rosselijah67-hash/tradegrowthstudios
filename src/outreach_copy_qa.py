"""Local QA/lint command for generated outreach copy."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .cli_utils import build_parser
from .config import get_database_path, load_env, project_path
from .logging_config import configure_logging


COMMAND = "outreach_copy_qa"
CSV_OUTPUT = "runs/latest/outreach_copy_qa.csv"
JSON_OUTPUT = "runs/latest/outreach_copy_qa.json"
P0 = "P0"
P1 = "P1"
P2 = "P2"

BANNED_DECEPTIVE_PATTERNS = {
    "guaranteed": re.compile(r"\bguaranteed\b", re.I),
    "guaranteed_ranking": re.compile(r"\bguaranteed\s+(ranking|rankings|rank)\b", re.I),
    "guaranteed_leads": re.compile(r"\bguaranteed\s+leads?\b", re.I),
    "10x": re.compile(r"\b10x\b", re.I),
    "risk_free": re.compile(r"\brisk[-\s]?free\b", re.I),
    "no_obligation_unverified": re.compile(r"\bno obligation\b", re.I),
}
FAKE_RELATIONSHIP_PATTERNS = {
    "following_up_on_our_conversation": re.compile(
        r"\bfollowing up on our conversation\b", re.I
    ),
    "as_discussed": re.compile(r"\bas discussed\b", re.I),
    "per_our_call": re.compile(r"\bper our call\b", re.I),
    "fake_reply_subject": re.compile(r"^\s*(re|fw|fwd):", re.I),
}
INTERNAL_JARGON_PATTERNS = {
    "case_file": re.compile(r"\bcase file\b", re.I),
    "audit_notes": re.compile(r"\baudit notes\b", re.I),
    "audit_recorded": re.compile(r"\baudit recorded\b", re.I),
    "system_detected": re.compile(r"\bsystem detected\b|\bour system detected\b", re.I),
    "signal_console": re.compile(r"\bsignal console\b", re.I),
}
PLACEHOLDER_PATTERNS = {
    "template_braces": re.compile(r"({{.*?}}|{%.*?%}|{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*})"),
    "generic_square_placeholder": re.compile(
        r"\[(Business Name|Company|Company Name|Name|First Name|URL|Link|Website)\]",
        re.I,
    ),
}
EXACT_LOSS_PATTERN = re.compile(
    r"\b(losing|lose|costing|costs|miss(?:ing)? out on)\b[^.\n]{0,80}"
    r"(\$\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s?%|\d+\s+(?:leads?|calls?|jobs?|customers?))",
    re.I,
)
LICENSE_FACT_PATTERN = re.compile(
    r"\b(licensed|insured|bonded|warrant(?:y|ies)|license|insurance)\b", re.I
)
OPT_OUT_PATTERN = re.compile(
    r"\b(unsubscribe|opt[-\s]?out|not interested|do not follow up|no problem)\b", re.I
)
PUBLIC_PACKET_PATTERN = re.compile(
    r"\b(short version|private page|main points|screenshots and notes|marked up)\b|https?://|\n/",
    re.I,
)
WEAK_OPENING_PATTERNS = re.compile(
    r"\b(i hope this finds you well|just checking in|circle back|quick question|"
    r"following up)\b",
    re.I,
)
CTA_PATTERN = re.compile(
    r"(\?|reply|walkthrough|open to|worth|useful|talk|call|send over|take a look)",
    re.I,
)
TECHNICAL_SOURCES = {
    "pagespeed",
    "site_audit",
    "score_reason",
    "lead_score",
    "website_audit",
    "crawl",
}
TECHNICAL_CATEGORIES = {"pagespeed", "no_analytics", "no_schema", "tracking"}
VISUAL_SOURCES = {"visual_review", "manual_visual", "manual", "human_review"}


@dataclass(frozen=True)
class QaFlag:
    severity: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class DraftQaRecord:
    artifact_id: int
    artifact_key: str
    prospect_id: int
    business_name: str
    market: str
    niche: str
    step: int
    subject: str
    body: str
    draft_path: str
    metadata: dict[str, Any]
    prospect: dict[str, Any]
    contact_name: str
    packet_exists: bool
    packet_url: str
    flags: list[QaFlag]

    def count(self, severity: str) -> int:
        return sum(1 for flag in self.flags if flag.severity == severity)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = build_parser("QA generated outreach draft copy without sending email.")
    parser.add_argument("--prospect-id", type=int, default=None, help="QA one prospect.")
    parser.add_argument(
        "--fail-on-p0",
        action="store_true",
        help="Exit non-zero when any P0 blocker is found.",
    )
    parser.add_argument(
        "--write-metadata",
        action="store_true",
        help="Store copy_qa results on email_draft artifact metadata_json.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    load_env()
    configure_logging(args.log_level or "INFO")
    logger = logging.getLogger(COMMAND)

    connection = open_database(args.db_path, write=bool(args.write_metadata and not args.dry_run))
    try:
        prospects = select_prospects(
            connection,
            market=args.market,
            niche=args.niche,
            prospect_id=args.prospect_id,
            limit=args.limit,
        )
        records = load_draft_records(connection, prospects)
        for record in records:
            record.flags.extend(check_record(record))
        add_repeated_phrase_flags(records)

        csv_path = project_path(CSV_OUTPUT)
        json_path = project_path(JSON_OUTPUT)
        write_csv(records, csv_path)
        write_json(records, json_path)

        if args.write_metadata and not args.dry_run:
            write_metadata(connection, records)
            connection.commit()

        summary = summarize(records, prospects)
        print_summary(summary, csv_path, json_path, metadata_written=bool(args.write_metadata and not args.dry_run))
        logger.info(
            "outreach_copy_qa_finished",
            extra={"event": "outreach_copy_qa_finished", **summary},
        )
        return 1 if args.fail_on_p0 and summary["p0_count"] > 0 else 0
    finally:
        connection.close()


def open_database(db_path: str | None, *, write: bool) -> sqlite3.Connection:
    if write:
        return db.connect(db_path)
    path = Path(db_path) if db_path else get_database_path()
    if not path.is_absolute():
        path = project_path(path)
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def select_prospects(
    connection: sqlite3.Connection,
    *,
    market: str | None,
    niche: str | None,
    prospect_id: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    clauses = [
        """
        EXISTS (
            SELECT 1
            FROM artifacts a
            WHERE a.prospect_id = p.id
              AND a.artifact_type = 'email_draft'
        )
        """
    ]
    params: list[Any] = []
    if prospect_id is not None:
        clauses.append("p.id = ?")
        params.append(prospect_id)
    if market:
        clauses.append("p.market = ?")
        params.append(market)
    if niche:
        clauses.append("p.niche = ?")
        params.append(niche)

    sql = f"""
        SELECT p.*
        FROM prospects p
        WHERE {' AND '.join(clauses)}
        ORDER BY p.updated_at DESC, p.id DESC
    """
    if prospect_id is None and limit:
        sql += " LIMIT ?"
        params.append(limit)

    return [row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def load_draft_records(
    connection: sqlite3.Connection,
    prospects: list[dict[str, Any]],
) -> list[DraftQaRecord]:
    records: list[DraftQaRecord] = []
    for prospect in prospects:
        prospect["metadata"] = json_loads(prospect.get("metadata_json"), {})
        artifacts = load_artifacts(connection, int(prospect["id"]))
        contacts = load_contacts(connection, int(prospect["id"]))
        contact_name = first_contact_name(contacts)
        packet_exists, packet_url = public_packet_status(artifacts)
        for artifact in artifacts:
            if artifact.get("artifact_type") != "email_draft":
                continue
            metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
            step = email_draft_step(artifact)
            path = str(artifact.get("path") or draft_fallback_path(prospect["id"], step))
            text = read_draft_text(path)
            subject, body = split_email_draft_text(text, metadata.get("subject"))
            records.append(
                DraftQaRecord(
                    artifact_id=int(artifact["id"]),
                    artifact_key=str(artifact.get("artifact_key") or ""),
                    prospect_id=int(prospect["id"]),
                    business_name=str(prospect.get("business_name") or ""),
                    market=str(prospect.get("market") or ""),
                    niche=str(prospect.get("niche") or ""),
                    step=step,
                    subject=subject,
                    body=body,
                    draft_path=path,
                    metadata=metadata,
                    prospect=prospect,
                    contact_name=contact_name,
                    packet_exists=packet_exists,
                    packet_url=packet_url,
                    flags=[],
                )
            )
    records.sort(key=lambda item: (item.prospect_id, item.step, item.artifact_key))
    return records


def load_artifacts(connection: sqlite3.Connection, prospect_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM artifacts
        WHERE prospect_id = ?
          AND artifact_type IN ('email_draft', 'public_packet')
        ORDER BY artifact_type, id
        """,
        (prospect_id,),
    ).fetchall()
    artifacts = [row_to_dict(row) for row in rows]
    for artifact in artifacts:
        artifact["metadata"] = json_loads(artifact.get("metadata_json"), {})
    return artifacts


def load_contacts(connection: sqlite3.Connection, prospect_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM contacts
        WHERE prospect_id = ?
        ORDER BY
            CASE WHEN email IS NOT NULL AND TRIM(email) <> '' THEN 0 ELSE 1 END,
            confidence DESC,
            id
        """,
        (prospect_id,),
    ).fetchall()
    contacts = [row_to_dict(row) for row in rows]
    for contact in contacts:
        contact["metadata"] = json_loads(contact.get("metadata_json"), {})
    return contacts


def check_record(record: DraftQaRecord) -> list[QaFlag]:
    flags: list[QaFlag] = []
    text = f"{record.subject}\n{record.body}"
    lowered = text.lower()
    body_lowered = record.body.lower()
    business_name = record.business_name.strip()

    if "[your name]" in lowered:
        add_flag(flags, P0, "placeholder_your_name", "Contains [Your Name].")
    if not has_sender_name(record.body):
        add_flag(flags, P0, "missing_sender_name", "Missing a real sender name in the signature.")
    if not OPT_OUT_PATTERN.search(record.body):
        add_flag(flags, P0, "missing_opt_out_line", "Missing opt-out line.")
    if (
        record.step == 1
        and record.packet_exists
        and not public_packet_url_in_draft(record)
    ):
        add_flag(
            flags,
            P0,
            "missing_public_packet_url_step_1",
            "Step 1 is missing the public packet URL even though a packet artifact exists.",
        )
    if business_name and normalize_for_match(business_name) not in normalize_for_match(text):
        add_flag(flags, P0, "no_business_name", "Business name is not present.")
    if bullet_count(record.body) == 0:
        add_flag(flags, P0, "no_issue_bullets", "No issue bullets found.")

    for code, pattern in BANNED_DECEPTIVE_PATTERNS.items():
        if pattern.search(text):
            add_flag(flags, P0, f"deceptive_phrase_{code}", f"Deceptive phrase present: {code}.")

    for code, pattern in FAKE_RELATIONSHIP_PATTERNS.items():
        target = record.subject if code == "fake_reply_subject" else text
        if pattern.search(target):
            add_flag(flags, P0, f"fake_relationship_{code}", f"Fake relationship phrase present: {code}.")

    if EXACT_LOSS_PATTERN.search(text):
        add_flag(flags, P0, "claims_exact_loss", "Claims exact traffic, revenue, calls, or lead loss.")
    if claims_unverified_license_fact(record):
        add_flag(
            flags,
            P0,
            "claims_unverified_license_insurance_warranty",
            "Claims licensing, insurance, or warranty facts not found in stored data.",
        )
    if not record.body.strip():
        add_flag(flags, P0, "body_empty", "Draft body is empty.")
    for code, pattern in PLACEHOLDER_PATTERNS.items():
        if pattern.search(text):
            add_flag(flags, P0, f"unreplaced_placeholder_{code}", f"Unreplaced placeholder found: {code}.")

    if record.step == 1 and word_count(record.body) > 220:
        add_flag(flags, P1, "step_1_over_220_words", "Step 1 is over 220 words.")
    if record.step == 1 and bullet_count(record.body) > 4:
        add_flag(flags, P1, "step_1_more_than_4_issues", "Step 1 has more than 4 issue bullets.")
    if count_word("audit", text) > 2:
        add_flag(flags, P1, "audit_used_more_than_twice", "Uses 'audit' more than twice.")
    if count_word("conversion", text) > 2:
        add_flag(flags, P1, "conversion_used_more_than_twice", "Uses 'conversion' more than twice.")
    for code, pattern in INTERNAL_JARGON_PATTERNS.items():
        if pattern.search(text):
            add_flag(flags, P1, f"internal_jargon_{code}", f"Internal jargon present: {code}.")
    if record.step == 1 and not has_public_packet_line(record):
        add_flag(flags, P1, "no_public_packet_line", "No public packet line found.")
    if not has_human_opening_line(record):
        add_flag(flags, P1, "no_human_opening_line", "Opening line is missing or generic.")
    if not has_specific_detail(record):
        add_flag(flags, P1, "no_specific_detail", "No niche, market, business, URL, or domain detail found in the body.")
    if semicolon_count(record.body) > 2 or bullet_count(record.body) > 5:
        add_flag(flags, P1, "too_many_semicolons_or_bullets", "Too many semicolons or bullets.")
    if all_selected_issues_technical(record):
        add_flag(
            flags,
            P1,
            "technical_only_selected_issues",
            "All selected issues are technical; no visual/manual issue source is represented.",
        )

    if len(record.subject) > 70:
        add_flag(flags, P2, "subject_over_70_chars", "Subject is longer than 70 characters.")
    if not record.contact_name:
        add_flag(flags, P2, "contact_name_missing", "No contact name is stored.")
    if not CTA_PATTERN.search(record.body):
        add_flag(flags, P2, "weak_cta", "CTA is weak or missing.")
    if is_generic_subject(record):
        add_flag(flags, P2, "generic_subject", "Subject is generic.")

    return flags


def add_repeated_phrase_flags(records: list[DraftQaRecord]) -> None:
    if len(records) < 5:
        return
    phrase_records: dict[str, set[int]] = {}
    phrase_text: dict[str, str] = {}
    for index, record in enumerate(records):
        for phrase in repeated_phrase_candidates(record.body):
            key = normalize_phrase_key(phrase)
            if not key:
                continue
            phrase_records.setdefault(key, set()).add(index)
            phrase_text.setdefault(key, phrase)

    threshold = min(5, max(3, len(records) // 3))
    repeated_keys = {
        key for key, indexes in phrase_records.items() if len(indexes) >= threshold
    }
    for index, record in enumerate(records):
        for key in repeated_keys:
            if index in phrase_records[key]:
                snippet = phrase_text[key][:80]
                add_flag(
                    record.flags,
                    P2,
                    "repeated_phrase_across_drafts",
                    f"Repeated phrase across many drafts: {snippet}",
                )
                break


def write_csv(records: list[DraftQaRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "prospect_id",
                "business_name",
                "step",
                "subject",
                "p0_count",
                "p1_count",
                "p2_count",
                "flags",
                "draft_path",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "prospect_id": record.prospect_id,
                    "business_name": record.business_name,
                    "step": record.step,
                    "subject": record.subject,
                    "p0_count": record.count(P0),
                    "p1_count": record.count(P1),
                    "p2_count": record.count(P2),
                    "flags": "; ".join(
                        f"{flag.severity}:{flag.code}" for flag in record.flags
                    ),
                    "draft_path": record.draft_path,
                }
            )


def write_json(records: list[DraftQaRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summarize(records, []),
        "drafts": [
            {
                "prospect_id": record.prospect_id,
                "business_name": record.business_name,
                "market": record.market,
                "niche": record.niche,
                "step": record.step,
                "subject": record.subject,
                "draft_path": record.draft_path,
                "public_packet_url": record.packet_url,
                "contact_name": record.contact_name,
                "p0_count": record.count(P0),
                "p1_count": record.count(P1),
                "p2_count": record.count(P2),
                "flags": [flag.as_dict() for flag in record.flags],
            }
            for record in records
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_metadata(connection: sqlite3.Connection, records: list[DraftQaRecord]) -> None:
    checked_at = db.utc_now()
    for record in records:
        metadata = dict(record.metadata)
        metadata["copy_qa"] = {
            "checked_at": checked_at,
            "p0_count": record.count(P0),
            "p1_count": record.count(P1),
            "p2_count": record.count(P2),
            "flags": [flag.as_dict() for flag in record.flags],
        }
        connection.execute(
            """
            UPDATE artifacts
            SET metadata_json = ?,
                updated_at = ?
            WHERE id = ?
              AND artifact_type = 'email_draft'
            """,
            (json.dumps(metadata, sort_keys=True), checked_at, record.artifact_id),
        )


def summarize(records: list[DraftQaRecord], prospects: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "prospect_count": len(prospects) if prospects else len({record.prospect_id for record in records}),
        "draft_count": len(records),
        "p0_count": sum(record.count(P0) for record in records),
        "p1_count": sum(record.count(P1) for record in records),
        "p2_count": sum(record.count(P2) for record in records),
    }


def print_summary(
    summary: dict[str, int],
    csv_path: Path,
    json_path: Path,
    *,
    metadata_written: bool,
) -> None:
    print("Outbound copy QA")
    print(f"Prospects checked: {summary['prospect_count']}")
    print(f"Drafts checked: {summary['draft_count']}")
    print(f"P0 blockers: {summary['p0_count']}")
    print(f"P1 warnings: {summary['p1_count']}")
    print(f"P2 suggestions: {summary['p2_count']}")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")
    print(f"Metadata written: {'yes' if metadata_written else 'no'}")


def public_packet_status(artifacts: list[dict[str, Any]]) -> tuple[bool, str]:
    packets = [artifact for artifact in artifacts if artifact.get("artifact_type") == "public_packet"]
    if not packets:
        return False, ""
    ready = [packet for packet in packets if str(packet.get("status") or "").lower() == "ready"]
    packet = (ready or packets)[-1]
    metadata = packet.get("metadata") if isinstance(packet.get("metadata"), dict) else {}
    url = str(
        packet.get("artifact_url")
        or metadata.get("public_packet_url")
        or metadata.get("public_url")
        or metadata.get("relative_url")
        or ""
    ).strip()
    return True, url


def public_packet_url_in_draft(record: DraftQaRecord) -> bool:
    metadata_url = str(record.metadata.get("public_packet_url") or "").strip()
    text = f"{record.subject}\n{record.body}"
    if metadata_url and metadata_url in text:
        return True
    if record.packet_url and record.packet_url in text:
        return True
    return bool(metadata_url and not record.packet_url)


def has_public_packet_line(record: DraftQaRecord) -> bool:
    if public_packet_url_in_draft(record):
        return True
    return bool(PUBLIC_PACKET_PATTERN.search(record.body))


def has_sender_name(body: str) -> bool:
    lines = [line.strip() for line in body.splitlines()]
    try:
        signature_index = max(index for index, line in enumerate(lines) if line in {"--", "-- "})
    except ValueError:
        signature_index = -1
    signature_lines = [line for line in lines[signature_index + 1 :] if line] if signature_index >= 0 else lines[-3:]
    if not signature_lines:
        return False
    signoffs = {"best", "best,", "thanks", "thanks,", "thank you", "sincerely", "cheers"}
    for candidate in signature_lines:
        name = candidate.strip()
        lowered = name.lower()
        if lowered in signoffs:
            continue
        if not name or "[" in name or "]" in name:
            return False
        if lowered in {"trade growth studio", "local growth audit", "your name", "none", "null"}:
            return False
        return bool(re.search(r"[A-Za-z]", name))
    return False


def has_human_opening_line(record: DraftQaRecord) -> bool:
    opening = first_content_line(record.body)
    if not opening or WEAK_OPENING_PATTERNS.search(opening):
        return False
    detail_terms = [
        record.business_name,
        "public site",
        "mobile",
        "website",
        "call/request",
        "looked through",
        "checked",
    ]
    opening_lower = opening.lower()
    return any(str(term or "").strip().lower() in opening_lower for term in detail_terms if str(term or "").strip())


def first_content_line(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].lower().startswith(("hi ", "hello ", "hey ")):
        return lines[1] if len(lines) > 1 else ""
    return lines[0]


def has_specific_detail(record: DraftQaRecord) -> bool:
    body = normalize_for_match(record.body)
    candidates = [
        record.business_name,
        record.niche.replace("_", " "),
        record.market.replace("_", " "),
        market_city(record.market),
        str(record.prospect.get("domain") or ""),
    ]
    url = str(record.prospect.get("website_url") or "")
    if url:
        candidates.append(url.replace("https://", "").replace("http://", "").strip("/"))
    return any(normalize_for_match(candidate) in body for candidate in candidates if candidate)


def all_selected_issues_technical(record: DraftQaRecord) -> bool:
    selected = record.metadata.get("selected_issues")
    if not isinstance(selected, list) or not selected:
        return False
    has_visual = False
    has_technical = False
    for issue in selected:
        if not isinstance(issue, dict):
            continue
        source = str(issue.get("source") or "").strip().lower()
        category = str(issue.get("category") or "").strip().lower()
        key = str(issue.get("key") or "").strip().lower()
        manual_visual = bool(issue.get("manual_visual"))
        if manual_visual or source in VISUAL_SOURCES or key.startswith("visual:"):
            has_visual = True
        if source in TECHNICAL_SOURCES or category in TECHNICAL_CATEGORIES or key.startswith("pagespeed:") or key.startswith("site:"):
            has_technical = True
    return has_technical and not has_visual


def claims_unverified_license_fact(record: DraftQaRecord) -> bool:
    matches = {match.group(1).lower() for match in LICENSE_FACT_PATTERN.finditer(record.body)}
    if not matches:
        return False
    data_text = normalize_for_match(
        json.dumps(
            {
                "prospect": record.prospect,
                "metadata": record.metadata,
            },
            default=str,
            sort_keys=True,
        )
    )
    for term in matches:
        if normalize_for_match(term) not in data_text:
            return True
    return False


def is_generic_subject(record: DraftQaRecord) -> bool:
    subject = normalize_for_match(record.subject)
    if not subject:
        return True
    generic_exact = {
        "website notes",
        "quick website audit",
        "website audit",
        "quick question",
        "website help",
        "site notes",
    }
    if subject in generic_exact:
        return True
    business = normalize_for_match(record.business_name)
    issue_terms = ("mobile", "call", "service", "roof", "hvac", "plumb", "packet", "private")
    return business not in subject and not any(term in subject for term in issue_terms)


def repeated_phrase_candidates(body: str) -> list[str]:
    candidates: list[str] = []
    for raw_line in body.splitlines():
        line = " ".join(raw_line.strip().lstrip("-").strip().split())
        lowered = line.lower()
        if len(line) < 28 or len(line) > 140:
            continue
        if "http" in lowered or lowered.startswith(("hi ", "hello ")):
            continue
        if any(skip in lowered for skip in ("not relevant", "not interested", "trade growth studio")):
            continue
        candidates.append(line)

    sentences = re.split(r"(?<=[.!?])\s+", " ".join(body.split()))
    for sentence in sentences:
        text = " ".join(sentence.strip().split())
        lowered = text.lower()
        if 36 <= len(text) <= 140 and "http" not in lowered:
            candidates.append(text)
    return unique_strings(candidates)


def add_flag(flags: list[QaFlag], severity: str, code: str, message: str) -> None:
    if any(flag.severity == severity and flag.code == code for flag in flags):
        return
    flags.append(QaFlag(severity=severity, code=code, message=message))


def split_email_draft_text(text: str, fallback_subject: Any = None) -> tuple[str, str]:
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body_lines = lines[1:]
        if body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]
        return subject or str(fallback_subject or ""), "\n".join(body_lines).strip()
    return str(fallback_subject or ""), text.strip()


def email_draft_step(artifact: dict[str, Any]) -> int:
    metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    try:
        return int(metadata.get("step"))
    except (TypeError, ValueError):
        pass
    marker = ":email_"
    artifact_key = str(artifact.get("artifact_key") or "")
    if marker in artifact_key:
        try:
            return int(artifact_key.rsplit(marker, 1)[-1])
        except ValueError:
            pass
    path = str(artifact.get("path") or "")
    match = re.search(r"email_(\d+)\.txt$", path)
    return int(match.group(1)) if match else 0


def draft_fallback_path(prospect_id: Any, step: int) -> str:
    return f"runs/latest/outreach_drafts/{prospect_id}/email_{step}.txt"


def read_draft_text(path: str) -> str:
    resolved = project_path(path)
    if not resolved.is_file():
        return ""
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def first_contact_name(contacts: list[dict[str, Any]]) -> str:
    for contact in contacts:
        name = str(contact.get("name") or "").strip()
        if name and "@" not in name and name.lower() not in {"none", "null", "n/a"}:
            return name
    return ""


def bullet_count(body: str) -> int:
    return sum(1 for line in body.splitlines() if line.strip().startswith("- "))


def semicolon_count(body: str) -> int:
    return body.count(";")


def word_count(value: str) -> int:
    return len(re.findall(r"\b[\w']+\b", value))


def count_word(word: str, value: str) -> int:
    return len(re.findall(rf"\b{re.escape(word)}\b", value, flags=re.I))


def normalize_for_match(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def normalize_phrase_key(value: str) -> str:
    text = normalize_for_match(value)
    text = re.sub(r"[^a-z0-9\s']", "", text)
    return re.sub(r"\s+", " ", text).strip()


def market_city(market: str) -> str:
    tokens = re.split(r"[_\-\s]+", str(market or "").strip())
    if len(tokens) > 1 and len(tokens[-1]) == 2:
        tokens = tokens[:-1]
    return " ".join(tokens)


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output


def json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


if __name__ == "__main__":
    raise SystemExit(main())
