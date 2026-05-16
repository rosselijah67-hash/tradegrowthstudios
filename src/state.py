"""Canonical state constants and compatibility helpers."""

from __future__ import annotations

from typing import Any


class QualificationStatus:
    DISCOVERED = "DISCOVERED"
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"
    NO_WEBSITE = "NO_WEBSITE"


class AuditDataStatus:
    PENDING = "PENDING"
    NEEDS_SITE_AUDIT = "NEEDS_SITE_AUDIT"
    NEEDS_SCREENSHOTS = "NEEDS_SCREENSHOTS"
    NEEDS_PAGESPEED = "NEEDS_PAGESPEED"
    READY = "READY"


class HumanReviewStatus:
    NOT_READY = "NOT_READY"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class HumanReviewDecision:
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ProspectStatus:
    NEW = "NEW"
    NO_WEBSITE = "NO_WEBSITE"
    ELIGIBLE_FOR_AUDIT = "ELIGIBLE_FOR_AUDIT"
    INELIGIBLE = "INELIGIBLE"
    AUDIT_READY = "AUDIT_READY"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED_FOR_OUTREACH = "APPROVED_FOR_OUTREACH"
    REJECTED_REVIEW = "REJECTED_REVIEW"
    OUTREACH_DRAFTED = "OUTREACH_DRAFTED"
    OUTREACH_SENT = "OUTREACH_SENT"
    CONTACT_MADE = "CONTACT_MADE"
    CALL_BOOKED = "CALL_BOOKED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    CLOSED_WON = "CLOSED_WON"
    CLOSED_LOST = "CLOSED_LOST"
    PROJECT_ACTIVE = "PROJECT_ACTIVE"
    PROJECT_COMPLETE = "PROJECT_COMPLETE"
    DISCARDED = "DISCARDED"


class NextAction:
    COLD_CALL_WEBSITE = "COLD_CALL_WEBSITE"
    RUN_AUDIT = "RUN_AUDIT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    APPROVED_FOR_OUTREACH = "APPROVED_FOR_OUTREACH"
    SEND_OUTREACH = "SEND_OUTREACH"
    WAIT_FOR_REPLY = "WAIT_FOR_REPLY"
    SCHEDULE_CALL = "SCHEDULE_CALL"
    TAKE_CALL = "TAKE_CALL"
    FOLLOW_UP_PROPOSAL = "FOLLOW_UP_PROPOSAL"
    START_PROJECT = "START_PROJECT"
    FULFILL_PROJECT = "FULFILL_PROJECT"
    DISCARD = "DISCARD"
    NONE = "NONE"


class OutreachEventType:
    CRM_STAGE_CHANGE = "crm_stage_change"
    SENT = "sent"
    SEND_FAILED = "send_failed"


class OutreachEventStatus:
    RECORDED = "recorded"
    SENT = "sent"
    FAILED = "failed"
    QUEUED = "queued"


TERMINAL_CRM_STATUSES = {
    ProspectStatus.NO_WEBSITE,
    ProspectStatus.INELIGIBLE,
    ProspectStatus.REJECTED_REVIEW,
    ProspectStatus.CLOSED_WON,
    ProspectStatus.CLOSED_LOST,
    ProspectStatus.PROJECT_ACTIVE,
    ProspectStatus.PROJECT_COMPLETE,
    ProspectStatus.DISCARDED,
}

PIPELINE_STAGE_BUCKETS = [
    ProspectStatus.NEW,
    ProspectStatus.NO_WEBSITE,
    ProspectStatus.ELIGIBLE_FOR_AUDIT,
    ProspectStatus.INELIGIBLE,
    ProspectStatus.AUDIT_READY,
    ProspectStatus.PENDING_REVIEW,
    ProspectStatus.APPROVED_FOR_OUTREACH,
    ProspectStatus.REJECTED_REVIEW,
    ProspectStatus.OUTREACH_DRAFTED,
    ProspectStatus.OUTREACH_SENT,
    ProspectStatus.CONTACT_MADE,
    ProspectStatus.CALL_BOOKED,
    ProspectStatus.PROPOSAL_SENT,
    ProspectStatus.CLOSED_WON,
    ProspectStatus.CLOSED_LOST,
    ProspectStatus.PROJECT_ACTIVE,
    ProspectStatus.PROJECT_COMPLETE,
    ProspectStatus.DISCARDED,
]

CRM_STAGES = [
    (ProspectStatus.NO_WEBSITE, "No Website"),
    (ProspectStatus.DISCARDED, "Discarded"),
    (ProspectStatus.PENDING_REVIEW, "Pending Manual Review"),
    (ProspectStatus.APPROVED_FOR_OUTREACH, "Approved for Outreach"),
    (ProspectStatus.OUTREACH_DRAFTED, "Outreach Drafted"),
    (ProspectStatus.OUTREACH_SENT, "Outreach Sent"),
    (ProspectStatus.CONTACT_MADE, "Contact Made"),
    (ProspectStatus.CALL_BOOKED, "Call Booked"),
    (ProspectStatus.PROPOSAL_SENT, "Proposal Sent"),
    (ProspectStatus.CLOSED_WON, "Closed Won"),
    (ProspectStatus.CLOSED_LOST, "Closed Lost"),
    (ProspectStatus.PROJECT_ACTIVE, "Project Active"),
    (ProspectStatus.PROJECT_COMPLETE, "Project Complete"),
]

CRM_NEXT_ACTIONS = {
    ProspectStatus.NO_WEBSITE: NextAction.COLD_CALL_WEBSITE,
    ProspectStatus.DISCARDED: NextAction.NONE,
    ProspectStatus.PENDING_REVIEW: NextAction.HUMAN_REVIEW,
    ProspectStatus.APPROVED_FOR_OUTREACH: NextAction.APPROVED_FOR_OUTREACH,
    ProspectStatus.OUTREACH_DRAFTED: NextAction.SEND_OUTREACH,
    ProspectStatus.OUTREACH_SENT: NextAction.WAIT_FOR_REPLY,
    ProspectStatus.CONTACT_MADE: NextAction.SCHEDULE_CALL,
    ProspectStatus.CALL_BOOKED: NextAction.TAKE_CALL,
    ProspectStatus.PROPOSAL_SENT: NextAction.FOLLOW_UP_PROPOSAL,
    ProspectStatus.CLOSED_WON: NextAction.START_PROJECT,
    ProspectStatus.CLOSED_LOST: NextAction.NONE,
    ProspectStatus.PROJECT_ACTIVE: NextAction.FULFILL_PROJECT,
    ProspectStatus.PROJECT_COMPLETE: NextAction.NONE,
}

STATUS_ALIASES = {
    "NEW": ProspectStatus.NEW,
    "DISCOVERED": ProspectStatus.NEW,
    "NO_WEBSITE": ProspectStatus.NO_WEBSITE,
    "MISSING_WEBSITE": ProspectStatus.NO_WEBSITE,
    "COLD_CALL_WEBSITE": ProspectStatus.NO_WEBSITE,
    "ELIGIBLE_FOR_AUDIT": ProspectStatus.ELIGIBLE_FOR_AUDIT,
    "INELIGIBLE": ProspectStatus.INELIGIBLE,
    "DISQUALIFIED": ProspectStatus.INELIGIBLE,
    "NOT_QUALIFIED": ProspectStatus.INELIGIBLE,
    "AUDIT_READY": ProspectStatus.AUDIT_READY,
    "READY_FOR_AUDIT": ProspectStatus.ELIGIBLE_FOR_AUDIT,
    "PENDING_REVIEW": ProspectStatus.PENDING_REVIEW,
    "APPROVED_FOR_OUTREACH": ProspectStatus.APPROVED_FOR_OUTREACH,
    "APPROVE": ProspectStatus.APPROVED_FOR_OUTREACH,
    "APPROVED": ProspectStatus.APPROVED_FOR_OUTREACH,
    "REJECT": ProspectStatus.REJECTED_REVIEW,
    "REJECTED": ProspectStatus.REJECTED_REVIEW,
    "REJECTED_REVIEW": ProspectStatus.REJECTED_REVIEW,
    "OUTREACH_DRAFTED": ProspectStatus.OUTREACH_DRAFTED,
    "DRAFT_OUTREACH": ProspectStatus.OUTREACH_DRAFTED,
    "DRAFTED_OUTREACH": ProspectStatus.OUTREACH_DRAFTED,
    "EMAIL_DRAFTED": ProspectStatus.OUTREACH_DRAFTED,
    "SEND_OUTREACH": ProspectStatus.OUTREACH_DRAFTED,
    "OUTREACH_SENT": ProspectStatus.OUTREACH_SENT,
    "SENT_OUTREACH": ProspectStatus.OUTREACH_SENT,
    "EMAIL_SENT": ProspectStatus.OUTREACH_SENT,
    "CONTACT_MADE": ProspectStatus.CONTACT_MADE,
    "CALL_BOOKED": ProspectStatus.CALL_BOOKED,
    "BOOK_CALL": ProspectStatus.CALL_BOOKED,
    "PROPOSAL_SENT": ProspectStatus.PROPOSAL_SENT,
    "CLOSED_WON": ProspectStatus.CLOSED_WON,
    "WON": ProspectStatus.CLOSED_WON,
    "CLOSED_LOST": ProspectStatus.CLOSED_LOST,
    "LOST": ProspectStatus.CLOSED_LOST,
    "PROJECT_ACTIVE": ProspectStatus.PROJECT_ACTIVE,
    "PROJECT_COMPLETE": ProspectStatus.PROJECT_COMPLETE,
    "PROJECT_COMPLETED": ProspectStatus.PROJECT_COMPLETE,
    "DISCARDED": ProspectStatus.DISCARDED,
    "DISCARD": ProspectStatus.DISCARDED,
}


def _token(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else row
    return row[key] if key in keys else None


def normalize_status(value: Any) -> str:
    token = _token(value)
    return STATUS_ALIASES.get(token, token)


def canonical_next_action_for_status(status: Any) -> str | None:
    return CRM_NEXT_ACTIONS.get(normalize_status(status))


def compute_pipeline_stage(row: Any) -> str:
    status = normalize_status(_row_get(row, "status"))
    qualification_status = _token(_row_get(row, "qualification_status"))
    audit_data_status = _token(_row_get(row, "audit_data_status"))
    human_review_status = _token(_row_get(row, "human_review_status"))
    human_review_decision = _token(_row_get(row, "human_review_decision"))
    next_action = normalize_status(_row_get(row, "next_action"))

    for token in (status, next_action):
        if token in PIPELINE_STAGE_BUCKETS:
            return token

    if human_review_decision in STATUS_ALIASES:
        return normalize_status(human_review_decision)

    if human_review_status in STATUS_ALIASES:
        return normalize_status(human_review_status)

    if _token(_row_get(row, "next_action")) in {
        NextAction.HUMAN_REVIEW,
        "REVIEW",
        "MANUAL_REVIEW",
        ProspectStatus.PENDING_REVIEW,
    }:
        return ProspectStatus.PENDING_REVIEW

    if qualification_status in {
        ProspectStatus.INELIGIBLE,
        QualificationStatus.DISQUALIFIED,
        "NOT_QUALIFIED",
    }:
        return ProspectStatus.INELIGIBLE

    if audit_data_status in {
        AuditDataStatus.READY,
        ProspectStatus.AUDIT_READY,
        "COMPLETE",
        "COMPLETED",
        "SUCCEEDED",
    }:
        if human_review_status in {"PENDING", "QUEUED", "NEEDS_REVIEW", "REVIEW"}:
            return ProspectStatus.PENDING_REVIEW
        return ProspectStatus.AUDIT_READY

    if qualification_status in {"AUDITED", ProspectStatus.AUDIT_READY}:
        if human_review_status in {"PENDING", "QUEUED", "NEEDS_REVIEW", "REVIEW"}:
            return ProspectStatus.PENDING_REVIEW
        return ProspectStatus.AUDIT_READY

    if qualification_status in {
        "ELIGIBLE",
        QualificationStatus.QUALIFIED,
        "READY_FOR_AUDIT",
    }:
        return ProspectStatus.ELIGIBLE_FOR_AUDIT

    if status == ProspectStatus.NEW or qualification_status in {
        QualificationStatus.DISCOVERED,
        ProspectStatus.NEW,
        "PENDING",
    }:
        return ProspectStatus.NEW

    return ProspectStatus.NEW
