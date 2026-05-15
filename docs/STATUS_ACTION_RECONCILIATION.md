# Status And Action Reconciliation

Report-only audit. No code, schema, sending, or external API behavior was changed.

## Scope

Fields inspected:

- `prospects.status`
- `prospects.qualification_status`
- `prospects.audit_data_status`
- `prospects.human_review_status`
- `prospects.human_review_decision`
- `prospects.next_action`
- `outreach_events.status`
- `outreach_events.event_type`

Local DB inspection used `data/leads.db` in SQLite read-only mode.

## Values Used In Code

### `prospects.status`

Set or accepted values:

- `new`
- `ELIGIBLE_FOR_AUDIT`
- `INELIGIBLE`
- `PENDING_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `REJECTED_REVIEW`
- `OUTREACH_DRAFTED`
- `OUTREACH_SENT`
- `DISCARDED`
- `CONTACT_MADE`
- `CALL_BOOKED`
- `PROPOSAL_SENT`
- `CLOSED_WON`
- `CLOSED_LOST`
- `PROJECT_ACTIVE`
- `PROJECT_COMPLETE`

Display/alias values recognized by `compute_pipeline_stage`:

- `NEW`
- `DISCOVERED`
- `AUDIT_READY`
- `DRAFT_OUTREACH`
- `DRAFTED_OUTREACH`
- `EMAIL_DRAFTED`
- `SEND_OUTREACH`
- `SENT_OUTREACH`
- `EMAIL_SENT`
- `BOOK_CALL`
- `WON`
- `LOST`
- `PROJECT_COMPLETED`

### `prospects.qualification_status`

Set values:

- `DISCOVERED`
- `DISQUALIFIED`
- `QUALIFIED`
- `AUDITED`
- `AUDIT_FAILED`

Display/alias values recognized:

- `INELIGIBLE`
- `NOT_QUALIFIED`
- `AUDIT_READY`
- `ELIGIBLE`
- `READY_FOR_AUDIT`
- `NEW`
- `PENDING`

### `prospects.audit_data_status`

Set values:

- `PENDING`
- `NEEDS_SITE_AUDIT`
- `NEEDS_PAGESPEED`
- `NEEDS_SCREENSHOTS`
- `READY`

Display/alias values recognized:

- `AUDIT_READY`
- `COMPLETE`
- `COMPLETED`
- `SUCCEEDED`

### `prospects.human_review_status`

Set values:

- `PENDING`
- `APPROVED`
- `REJECTED`
- `NOT_READY`

Display/alias values recognized:

- `APPROVE`
- `APPROVED_FOR_OUTREACH`
- `REJECT`
- `REJECTED_REVIEW`
- `DISCARD`
- `DISCARDED`
- `QUEUED`
- `NEEDS_REVIEW`
- `REVIEW`

### `prospects.human_review_decision`

Set values:

- `APPROVED`
- `REJECTED`
- `NULL`

Display/alias values recognized:

- `APPROVE`
- `APPROVED_FOR_OUTREACH`
- `REJECT`
- `REJECTED_REVIEW`
- `DISCARD`
- `DISCARDED`

### `prospects.next_action`

Set or filtered values:

- `RUN_AUDIT`
- `DISCARD`
- `NEEDS_SITE_AUDIT`
- `NEEDS_PAGESPEED`
- `NEEDS_SCREENSHOTS`
- `HUMAN_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `REJECTED_BY_REVIEW`
- `SEND_OUTREACH`
- `OUTREACH_DRAFTED`
- `WAIT_FOR_REPLY`
- `NONE`
- `SCHEDULE_CALL`
- `TAKE_CALL`
- `FOLLOW_UP_PROPOSAL`
- `START_PROJECT`
- `FULFILL_PROJECT`
- `AUDIT_ARTIFACT`
- `HOMEPAGE_PREVIEW`
- `PRIORITY_OUTREACH`

Display/alias values recognized:

- CRM/status-like values from `LIFECYCLE_STAGE_ALIASES`, including `OUTREACH_SENT`, `CONTACT_MADE`, `CALL_BOOKED`, `PROPOSAL_SENT`, `CLOSED_WON`, `CLOSED_LOST`, `PROJECT_ACTIVE`, `PROJECT_COMPLETE`.
- Review aliases: `REVIEW`, `MANUAL_REVIEW`, `PENDING_REVIEW`.

### `outreach_events.status`

Set or filtered values:

- `sent`
- `failed`
- `queued`
- Any CRM stage value when `event_type = 'crm_stage_change'`, because the dashboard stores the new CRM stage in `outreach_events.status`.

CRM stage values that may be written as event status:

- `DISCARDED`
- `PENDING_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `OUTREACH_DRAFTED`
- `OUTREACH_SENT`
- `CONTACT_MADE`
- `CALL_BOOKED`
- `PROPOSAL_SENT`
- `CLOSED_WON`
- `CLOSED_LOST`
- `PROJECT_ACTIVE`
- `PROJECT_COMPLETE`
- `REJECTED_REVIEW` via review decision logging

### `outreach_events.event_type`

Set values:

- `crm_stage_change`
- `sent`
- `send_failed`

## Values Observed In Local DB

### `prospects.status`

| Value | Count |
|---|---:|
| `new` | 182 |

### `prospects.qualification_status`

| Value | Count |
|---|---:|
| `DISCOVERED` | 154 |
| `DISQUALIFIED` | 21 |
| `AUDITED` | 7 |

### `prospects.audit_data_status`

| Value | Count |
|---|---:|
| `PENDING` | 175 |
| `READY` | 7 |

### `prospects.human_review_status`

| Value | Count |
|---|---:|
| `PENDING` | 182 |

### `prospects.human_review_decision`

| Value | Count |
|---|---:|
| `<blank/null>` | 182 |

### `prospects.next_action`

| Value | Count |
|---|---:|
| `<blank/null>` | 175 |
| `HUMAN_REVIEW` | 7 |

### `outreach_events.status`

No rows observed.

### `outreach_events.event_type`

No rows observed.

## Where Values Are Set

### Acquisition And Eligibility

- `src/db.py`
  - Defaults `prospects.status` to `new`.
  - Defaults `qualification_status` to `DISCOVERED`.
  - Defaults `audit_data_status` to `PENDING`.
  - Defaults `human_review_status` to `PENDING`.
  - `upsert_prospect` writes `status` and `qualification_status` from source data.

- `src/places_pull.py`
  - Creates prospects with `status = 'new'`.
  - Sets `qualification_status = 'DISCOVERED'` for initially usable Places leads.
  - Sets `qualification_status = 'DISQUALIFIED'` for missing required fields, non-operational businesses, missing websites, low review count, or franchise/chain detection.

- `src/eligibility.py`
  - Sets `qualification_status = 'QUALIFIED'` or `DISQUALIFIED`.
  - Sets `status = 'ELIGIBLE_FOR_AUDIT'` or `INELIGIBLE`.
  - Sets `next_action = 'RUN_AUDIT'` or `DISCARD`.

### Audit And Scoring

- `src/audit_site.py`
  - Selects prospects by `qualification_status IN ('DISCOVERED', 'QUALIFIED')`.
  - Sets `qualification_status = 'AUDITED'` after audit orchestration succeeds.
  - Sets `qualification_status = 'AUDIT_FAILED'` after site audit failure.

- `src/score_leads.py`
  - Sets `audit_data_status` to `NEEDS_SITE_AUDIT`, `NEEDS_PAGESPEED`, `NEEDS_SCREENSHOTS`, or `READY`.
  - Sets `human_review_status` to `NOT_READY`, `PENDING`, `APPROVED`, or `REJECTED`.
  - Sets `next_action` to the audit-data status when not ready, `DISCARD` when ineligible, `APPROVED_FOR_OUTREACH` when already approved, `REJECTED_BY_REVIEW` when rejected, or `HUMAN_REVIEW` when ready for review.
  - Does not update `prospects.status`.

### Human Review

- `src/review_leads.py`
  - Sets `human_review_status = 'APPROVED'`, `REJECTED`, or `PENDING`.
  - Sets `human_review_decision = 'APPROVED'`, `REJECTED`, or `NULL`.
  - If `audit_data_status = 'READY'`, sets `next_action` to `APPROVED_FOR_OUTREACH`, `REJECTED_BY_REVIEW`, or `HUMAN_REVIEW`.
  - If `audit_data_status = 'READY'`, sets `status` to `APPROVED_FOR_OUTREACH`, `REJECTED_REVIEW`, or `PENDING_REVIEW`.

- `src/dashboard_app.py`
  - Case review form mirrors the same review transitions as `review_leads.py`.
  - Dashboard review also logs `crm_stage_change` outreach events.

### Outreach Drafting And Sending

- `src/outreach_drafts.py`
  - Selects only `human_review_decision = 'APPROVED'`.
  - Normally requires `next_action = 'APPROVED_FOR_OUTREACH'`.
  - When regenerating a specific prospect, allows `next_action IN ('APPROVED_FOR_OUTREACH', 'SEND_OUTREACH')`.
  - Requires `status` to be null/blank, `APPROVED_FOR_OUTREACH`, or `OUTREACH_DRAFTED`.
  - After draft generation, sets `status = 'OUTREACH_DRAFTED'` and `next_action = 'SEND_OUTREACH'`.

- `src/send_outreach.py`
  - Step 1 candidate selection requires `human_review_decision = 'APPROVED'`, `next_action IN ('SEND_OUTREACH', 'OUTREACH_DRAFTED', 'APPROVED_FOR_OUTREACH')`, and `status IN ('OUTREACH_DRAFTED', 'APPROVED_FOR_OUTREACH')`.
  - Follow-up selection also allows `next_action = 'WAIT_FOR_REPLY'` and `status = 'OUTREACH_SENT'`.
  - Duplicate protection checks `outreach_events.status IN ('sent', 'queued')`.
  - Daily cap counts `outreach_events.status = 'sent'`.
  - Send failure writes `outreach_events.event_type = 'send_failed'` and `outreach_events.status = 'failed'`.
  - Successful send writes `outreach_events.event_type = 'sent'` and `outreach_events.status = 'sent'`.
  - After successful step 1 send, sets `status = 'OUTREACH_SENT'` and `next_action = 'WAIT_FOR_REPLY'`.

### CRM And Sales Packet

- `src/dashboard_app.py`
  - CRM stage form accepts `DISCARDED`, `PENDING_REVIEW`, `APPROVED_FOR_OUTREACH`, `OUTREACH_DRAFTED`, `OUTREACH_SENT`, `CONTACT_MADE`, `CALL_BOOKED`, `PROPOSAL_SENT`, `CLOSED_WON`, `CLOSED_LOST`, `PROJECT_ACTIVE`, `PROJECT_COMPLETE`.
  - CRM stage form sets `prospects.status` to that stage and maps `next_action` using `CRM_NEXT_ACTIONS`.
  - CRM stage changes write `outreach_events.event_type = 'crm_stage_change'`; the event `status` is set to the new CRM stage.
  - Sales packet buttons reuse the CRM stage form to set `PROPOSAL_SENT`, `CLOSED_WON`, or `CLOSED_LOST`.

## Where Values Are Selected Or Filtered

### Acquisition/Audit

- `src/eligibility.py` selects `qualification_status IN ('DISCOVERED', 'QUALIFIED', 'DISQUALIFIED')`.
- `src/audit_site.py` selects `qualification_status IN ('DISCOVERED', 'QUALIFIED')`.
- `src/screenshot_site.py` selects `qualification_status IN ('DISCOVERED', 'QUALIFIED', 'AUDITED')`.
- `src/pagespeed.py` selects `qualification_status IN ('DISCOVERED', 'QUALIFIED', 'AUDITED')`.

### Review/Artifacts

- `src/review_leads.py` selects `audit_data_status = 'READY'` and `human_review_status = 'PENDING'`.
- `src/generate_artifacts.py` selects `audit_data_status = 'READY'` and `next_action IN ('HUMAN_REVIEW', 'APPROVED_FOR_OUTREACH', 'AUDIT_ARTIFACT', 'HOMEPAGE_PREVIEW', 'PRIORITY_OUTREACH')`.
- `src/dashboard_app.py` review queue selects `audit_data_status = 'READY'`, `human_review_status = 'PENDING'`, and `next_action = 'HUMAN_REVIEW'`.

### Outreach

- `src/outreach_drafts.py` filters by `human_review_decision = 'APPROVED'`, `next_action`, and `status`.
- `src/send_outreach.py` filters by `human_review_decision = 'APPROVED'`, allowed `next_action`, allowed `status`, ready email-draft artifact, contact email, suppression list, duplicate event status, and daily sent count.

### Dashboard/CRM

- `src/dashboard_app.py` computes display pipeline stage from `status`, `next_action`, `human_review_decision`, `human_review_status`, `qualification_status`, and `audit_data_status`.
- `/leads?stage=...`, `/crm`, and `/crm/stage/...` filter in Python after computing `pipeline_stage`.
- Stage history selects `outreach_events` where `event_type = 'crm_stage_change' OR channel = 'email'`.

## Inconsistencies

1. `prospects.status` mixes acquisition state, audit pipeline state, outreach state, and CRM stage.
   - Initial value is lowercase `new`, while most later lifecycle values are uppercase.
   - The dashboard normalizes this, but raw DB values are not consistent.

2. `qualification_status` includes audit execution results.
   - `AUDITED` and `AUDIT_FAILED` are not qualification outcomes.
   - They are currently stored in `qualification_status` by `audit_site.py`.

3. `audit_site.py` and `score_leads.py` split audit readiness across different fields.
   - `audit_site.py` sets `qualification_status = 'AUDITED'`.
   - `score_leads.py` later computes `audit_data_status`.
   - Until scoring runs, a successfully audited prospect can have an audit-like `qualification_status` but stale/default `audit_data_status`.

4. `human_review_status` and `human_review_decision` overlap.
   - Both can store `APPROVED` or `REJECTED`.
   - This is workable, but it makes it unclear whether `human_review_status` is a queue state or a decision state.

5. `next_action` mixes actions, readiness blockers, and stage aliases.
   - Examples: `RUN_AUDIT`, `HUMAN_REVIEW`, `SEND_OUTREACH`, `WAIT_FOR_REPLY` are actions.
   - `NEEDS_SITE_AUDIT`, `NEEDS_PAGESPEED`, `NEEDS_SCREENSHOTS` are readiness/blocker states.
   - `APPROVED_FOR_OUTREACH` and `OUTREACH_DRAFTED` are stage-like values.

6. `outreach_events.status` mixes event outcome with CRM stage.
   - Email events use `sent` and `failed`.
   - CRM stage-change events use the new CRM stage as `status`.
   - The new stage is also in `metadata_json.new_status`, so storing it in `status` is redundant and semantically different from email event status.

7. `REJECTED_REVIEW` appears in pipeline stages but not in `CRM_STAGES`.
   - Manual review can set `prospects.status = 'REJECTED_REVIEW'`.
   - The CRM board has `DISCARDED`, but not `REJECTED_REVIEW`, so review rejections are pipeline-visible but not in a CRM column.

8. `send_outreach.py` accepts `APPROVED_FOR_OUTREACH` as sendable when a matching draft artifact exists.
   - The intended flow is approval -> draft generation -> `OUTREACH_DRAFTED`/`SEND_OUTREACH` -> send.
   - The draft artifact requirement prevents most accidental sends, but the status/action filter is looser than the canonical flow.

9. `queued` is treated as a duplicate-send blocking event status, but current code does not appear to create queued outreach events.
   - This may be legacy-safe, but it is not currently part of the active sender path.

## Recommended Canonical Values

### Acquisition

Use `qualification_status` only for acquisition/pre-audit eligibility:

- `DISCOVERED`
- `QUALIFIED`
- `DISQUALIFIED`

Avoid adding audit results such as `AUDITED` or `AUDIT_FAILED` to `qualification_status`.

### Audit Readiness

Use `audit_data_status` for audit completeness/readiness:

- `PENDING`
- `NEEDS_SITE_AUDIT`
- `NEEDS_SCREENSHOTS`
- `NEEDS_PAGESPEED`
- `READY`

If a separate failure state is needed, prefer `NEEDS_SITE_AUDIT` plus the failed `website_audits.status` row rather than adding another prospect-level audit state.

### Human Review

Use `human_review_status` for queue state:

- `NOT_READY`
- `PENDING`
- `APPROVED`
- `REJECTED`

Use `human_review_decision` only for the decision:

- `NULL`
- `APPROVED`
- `REJECTED`

The two fields can remain, but code should be clear that `human_review_decision` is the approval gate for outreach.

### Outreach Readiness

Use `next_action` for the operator's next action:

- `RUN_AUDIT`
- `HUMAN_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `SEND_OUTREACH`
- `WAIT_FOR_REPLY`
- `SCHEDULE_CALL`
- `TAKE_CALL`
- `FOLLOW_UP_PROPOSAL`
- `START_PROJECT`
- `FULFILL_PROJECT`
- `NONE`

Avoid using readiness blockers (`NEEDS_*`) as `next_action` long term. Keep blockers in `audit_data_status`.

### CRM Stage

Use `prospects.status` for lifecycle/CRM stage:

- `NEW`
- `ELIGIBLE_FOR_AUDIT`
- `INELIGIBLE`
- `AUDIT_READY`
- `PENDING_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `REJECTED_REVIEW`
- `OUTREACH_DRAFTED`
- `OUTREACH_SENT`
- `CONTACT_MADE`
- `CALL_BOOKED`
- `PROPOSAL_SENT`
- `CLOSED_WON`
- `CLOSED_LOST`
- `PROJECT_ACTIVE`
- `PROJECT_COMPLETE`
- `DISCARDED`

Consider whether `REJECTED_REVIEW` should remain distinct from `DISCARDED` in the CRM board. Keeping it distinct is useful for audit history; merging it into `DISCARDED` is simpler for operations.

### Outreach Events

Use `outreach_events.event_type` for what happened:

- `crm_stage_change`
- `sent`
- `send_failed`

Use `outreach_events.status` for event outcome:

- `recorded` for `crm_stage_change`
- `sent` for sent email
- `failed` for send failure
- `queued` only if a queueing path exists

Keep CRM stage values in `metadata_json.new_status`, not as the event status, if this is standardized later.

## Minimal Changes Needed Before Outreach Sending

No new tables are needed.

Recommended minimum before relying on actual sending:

1. Tighten step-1 sender eligibility to the canonical post-draft state.
   - Preferred: require `prospects.status = 'OUTREACH_DRAFTED'` and `next_action = 'SEND_OUTREACH'` for batch step-1 sends.
   - Keep `--prospect-id` override behavior only if it is intentionally operator-controlled.

2. Normalize `outreach_events.status` semantics.
   - For future `crm_stage_change` rows, use an event-outcome status such as `recorded` and keep the CRM stage in metadata.
   - Existing behavior is not dangerous for sending, but mixing CRM stages with email outcomes makes reporting harder.

3. Stop using `qualification_status` for audit outcome.
   - Let `qualification_status` mean only `DISCOVERED`/`QUALIFIED`/`DISQUALIFIED`.
   - Represent audit progress with `audit_data_status` and `website_audits.status`.
   - This reduces the chance that an audited-but-not-scored lead appears review-ready too early.

4. Choose a CRM home for `REJECTED_REVIEW`.
   - Either add it as a CRM column or map it into `DISCARDED`.
   - This is not a sending blocker, but it affects operator visibility.

5. Keep `human_review_decision = 'APPROVED'` as the hard outreach gate.
   - This is already in `outreach_drafts.py` and `send_outreach.py`.
   - Do not loosen this gate.

6. Keep duplicate-send protection based on `outreach_events` event key and `status IN ('sent', 'queued')`.
   - This is already present.
   - If `queued` is not used, leave it as harmless legacy protection or remove it later after confirming no queue path exists.
