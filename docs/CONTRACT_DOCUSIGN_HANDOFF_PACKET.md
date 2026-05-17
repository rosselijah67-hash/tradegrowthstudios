# Contract + DocuSign Integration Handoff Packet

Purpose: give ChatGPT or another coding agent enough repo context to engineer efficient, highly targeted prompts for adding CRM contracts and DocuSign signing to the existing quote/CRM ecosystem.

## 1. Repository Snapshot

Workspace root:

```text
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN
```

Actual app repo:

```text
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads
```

Latest commit observed:

```text
d767555 Polish printable quote PDF layout
```

Working tree was already dirty before this packet was created:

```text
 M .env.example
 M .gitignore
 M config/users.yaml
```

Relevant existing uncommitted changes:

- `.env.example` now includes DocuSign environment placeholders.
- `.gitignore` now ignores `secrets/`.
- `config/users.yaml` disables `outbound` and `send` permissions for non-admin users while preserving `quotes: true`.
- `config/users.yaml` references `AUTH_JOSHGETSMONEY_PASSWORD_HASH`, but `.env.example` currently lists `AUTH_ADMIN_PASSWORD_HASH`, `AUTH_QWHITE_PASSWORD_HASH`, `AUTH_JROSS_PASSWORD_HASH`, and `AUTH_AG_PASSWORD_HASH`; check whether the Josh env key should also be added.

Important safety note:

- I did not read `.env`; only `.env.example`.
- A `secrets/` directory exists and is now ignored. Treat it as local secret material.
- No `.docx`, `.doc`, `.pdf`, `.rtf`, or `.odt` contract template file was found under the workspace during this scan. The contract source document still needs to be provided or added.

## 2. High-Level App Shape

The app is now a Flask CRM/dashboard, not just the original CLI scaffold.

Core stack:

- Python app with Flask and Jinja templates.
- SQLite persistence at `data/leads.db` by default.
- Config through `.env` plus YAML files in `config/`.
- Generated private artifacts under `runs/latest/`, `artifacts/`, and `screenshots/`.
- Public outreach packets under `public_outreach/`.
- No Node/frontend build system.
- No current DocuSign SDK dependency.
- No current DOCX templating/conversion dependency such as `python-docx`, `docxtpl`, LibreOffice, or PDF renderer.

Current `requirements.txt`:

```text
python-dotenv==1.0.1
PyYAML==6.0.2
requests==2.32.3
beautifulsoup4==4.12.3
playwright==1.49.1
Jinja2>=3.1.4,<4.0
Flask>=3.0.0,<4.0
Flask-Login>=0.6.3,<1.0
dnspython>=2.6.1,<3.0
gunicorn>=22.0.0,<23.0
```

Start the dashboard:

```powershell
.\scripts\start_dashboard.ps1
```

or:

```powershell
python -m src.dashboard_app --host 127.0.0.1 --port 8787
```

Dashboard local URL:

```text
http://127.0.0.1:8787
```

Import/compile smoke check:

```powershell
python -m compileall src
```

## 3. Repo Tree Summary

Top-level app directories:

```text
ai-local-site-leads/
  .venv/
  artifacts/
  backups/
  config/
  data/
  docs/
  logs/
  public_outreach/
  runs/
  screenshots/
  scripts/
  secrets/
  src/
  static/
  templates/
```

Important source files:

```text
src/
  auth.py
  audit_site.py
  config.py
  dashboard_app.py
  dashboard_jobs.py
  db.py
  generate_artifacts.py
  inbox_sync.py
  pagespeed.py
  public_packets.py
  quote_exports.py
  quotes.py
  review_leads.py
  score_leads.py
  send_outreach.py
  state.py
  tasks.py
  territories.py
```

Important templates:

```text
templates/dashboard/
  base.html
  case.html
  crm.html
  crm_stage.html
  quote_builder.html
  quote_detail.html
  quote_printable.html
  quotes_list.html
  sales_packet.html
  task_detail.html
  tasks.html
```

Important config:

```text
config/
  quote_catalog.yaml
  task_templates.yaml
  users.yaml
  markets.yaml
  niches.yaml
  scoring.yaml
  outreach.yaml
```

Generated quote output convention already exists:

```text
runs/latest/quotes/<quote_key>/quote.txt
runs/latest/quotes/<quote_key>/quote.html
```

## 4. Current Database State

Current live SQLite tables and counts observed in `data/leads.db`:

```text
artifacts: 236
contacts: 2
crm_tasks: 0
dashboard_jobs: 31
outreach_events: 8
outreach_queue: 0
prospects: 311
quote_events: 7
quote_line_items: 6
quotes: 1
suppression_list: 0
website_audits: 541
```

Important tables:

- `prospects`: lead/business source of truth.
- `contacts`: manually saved or selected contacts; currently 2 rows.
- `website_audits`: site, PageSpeed, screenshot, visual review, and score evidence.
- `artifacts`: generated screenshots, public packets, quote exports, audit cards, etc.
- `quotes`: existing CRM quote headers.
- `quote_line_items`: existing quote package/add-on/recurring/custom lines.
- `quote_events`: quote-local event history.
- `outreach_events`: CRM/timeline events, email events, task events, quote lifecycle outreach timeline entries.
- `crm_tasks`: task system, currently no rows.

There are no contract-specific tables yet.

## 5. Existing Quote System

This is the most important integration point. Quotes are already implemented and should be extended rather than replaced.

Quote schema:

- `src/db.py` defines `QUOTE_SCHEMA_SQL` around line 232.
- Tables:
  - `quotes`
  - `quote_line_items`
  - `quote_events`
- `init_db()` calls `ensure_quote_schema()`.
- `create_app()` also calls `pipeline_db.ensure_quote_schema_for_path(...)` on dashboard startup.

Quote service:

- `src/quotes.py`
- Valid quote statuses: `draft`, `sent`, `accepted`, `declined`, `expired`, `superseded`.
- Key functions:
  - `load_quote_catalog()`
  - `create_quote_for_prospect()`
  - `get_quote()`
  - `list_quotes_for_prospect()`
  - `update_quote_header()`
  - `replace_quote_line_items()`
  - `update_quote_status()`
  - `create_quote_revision()`
  - `delete_quote()`
  - `recalculate_quote_totals()`
  - `log_quote_event()`

Quote export service:

- `src/quote_exports.py`
- Builds client-facing quote context, email text, and printable HTML.
- Writes exports and upserts artifact rows.
- Existing output path function writes under `runs/latest/quotes/<quote_key>/`.
- Default client-facing assumptions are defined in `DEFAULT_ASSUMPTIONS`.
- This file is a natural model for future `contract_exports.py`.

Quote routes:

- Implemented in `src/dashboard_app.py` inside `create_app()`.
- Existing routes include:
  - `GET /quotes`
  - `GET /quotes/new?prospect_id=<id>`
  - `POST /quotes/new`
  - `GET /quotes/<quote_id>`
  - `GET /quotes/<quote_id>/edit`
  - `POST /quotes/<quote_id>/edit`
  - `GET /quotes/<quote_id>/export/text`
  - `GET /quotes/<quote_id>/export/html`
  - `GET /quotes/<quote_id>/print`
  - `POST /quotes/<quote_id>/status`
  - `POST /quotes/<quote_id>/mark-sent`
  - `POST /quotes/<quote_id>/mark-accepted`
  - `POST /quotes/<quote_id>/mark-declined`
  - `POST /quotes/<quote_id>/create-revision`
  - `POST /quotes/<quote_id>/delete`

Quote templates:

- `templates/dashboard/quote_builder.html`
- `templates/dashboard/quote_detail.html`
- `templates/dashboard/quote_printable.html`
- `templates/dashboard/quotes_list.html`

Case integration:

- `templates/dashboard/case.html` already shows a prominent Create Quote button near the top.
- It also shows latest quote summary, view/edit/export/print/revision/delete actions, and attaches quote id to quick task creation.
- `case_file(prospect_id)` loads `quote_rows = quote_service.list_quotes_for_prospect(...)`, then passes `quotes` and `latest_quote`.

Quote lifecycle behavior:

- `handle_quote_lifecycle_action()` in `src/dashboard_app.py` updates quote status and can update CRM stage.
- Mark sent:
  - Updates quote status to `sent`.
  - Moves prospect to `PROPOSAL_SENT` unless protected.
  - Creates proposal follow-up auto task.
- Mark accepted:
  - Updates quote status to `accepted`.
  - Moves prospect to `CLOSED_WON`.
  - Creates closed-won auto tasks.
- Mark declined:
  - Updates quote status to `declined`.
  - Only moves prospect to `CLOSED_LOST` if `confirm_close_lost=1`.

Case timeline:

- `load_stage_history()` reads both `outreach_events` and `quote_events`.
- It adds quote events into the case history with `channel = "quote"`.

## 6. CRM, Tasks, Auth, And Permissions

State model:

- `src/state.py`
- Main CRM statuses include:
  - `CONTACT_MADE`
  - `CALL_BOOKED`
  - `PROPOSAL_SENT`
  - `CLOSED_WON`
  - `CLOSED_LOST`
  - `PROJECT_ACTIVE`
  - `PROJECT_COMPLETE`
- `CRM_NEXT_ACTIONS` maps statuses to next actions.
- `compute_pipeline_stage(row)` derives visible stage from status, next action, audit readiness, and review fields.

Stage changes:

- `apply_crm_stage_change()` in `src/dashboard_app.py` updates `prospects.status`, `prospects.next_action`, logs `crm_stage_change`, manages trash metadata for closed lost/discarded, and creates stage auto tasks.

Task system:

- `src/tasks.py`
- `src/db.py` defines `crm_tasks`.
- `config/task_templates.yaml` contains templates.
- Existing task types include:
  - `needs_quote`
  - `proposal_follow_up`
  - `contract_deposit`
  - `project_handoff`
  - `collect_assets`
  - `client_access_needed`
  - `launch_qa`
- `create_closed_won_auto_tasks()` currently creates:
  - `contract_deposit` task titled "Send contract and deposit next step"
  - `collect_assets` task
- This is the cleanest existing hook for the new contracts workflow.

Auth and permissions:

- `src/auth.py`
- Permission keys: `markets`, `quotes`, `run_jobs`, `outbound`, `send`.
- `quotes` permission gates quote pages.
- No `contracts` permission exists yet. Options:
  - Reuse `quotes` for contracts because contracts are quote-attached sales artifacts.
  - Or add a new `contracts` permission key in `auth.py`, `config/users.yaml`, and templates.
- Non-admin users currently have `quotes: true`, `outbound: false`, `send: false`.

CSRF:

- Dashboard has CSRF context helpers and validates mutating requests.
- New POST forms should include `{{ csrf_input() }}`.

Territory scoping:

- Prospects, quotes, jobs, outreach queue, and tasks carry `owner_username` and/or `market_state`.
- New contract tables should include `owner_username` and `market_state` so access patterns can match quote/task behavior.
- Existing quote access uses `require_quote_access(quote_id)` and then `require_prospect_access()`.
- Contracts should use the same pattern: load contract, load linked quote/prospect, require access to prospect.

## 7. Available Data For Contract Variables

Use stored data where possible, but require salesman confirmation for legal identity, signer authority, and entity details.

Prospect fields available from `prospects`:

- `id`
- `prospect_key`
- `business_name`
- `market`
- `niche`
- `address`
- `formatted_address`
- `city`
- `state`
- `city_guess`
- `state_guess`
- `market_state`
- `owner_username`
- `postal_code`
- `phone`
- `website_url`
- `domain`
- `rating`
- `user_rating_count`
- `primary_type`
- `types_json`
- `business_status`
- `status`
- `qualification_status`
- scoring fields
- `score_explanation_json`
- `audit_data_status`
- `human_review_status`
- `human_review_decision`
- `next_action`
- timestamps

Contact fields available from `contacts`:

- `name`
- `role`
- `email`
- `phone`
- `source`
- `confidence`
- `metadata_json`

Quote fields available from `quotes`:

- `quote_key`
- `prospect_id`
- `version`
- `status`
- `package_key`
- `package_name`
- `title`
- `client_business_name`
- `client_contact_name`
- `client_email`
- `client_phone`
- `website_url`
- `one_time_subtotal_cents`
- `one_time_discount_cents`
- `one_time_total_cents`
- `recurring_monthly_total_cents`
- `term_months`
- `deposit_percent`
- `deposit_due_cents`
- `balance_due_cents`
- `valid_until`
- `client_visible_notes`
- `assumptions_json`
- `internal_notes`
- `metadata_json`
- `sent_at`
- `accepted_at`
- `declined_at`
- `owner_username`
- `market_state`

Quote line item fields:

- `item_key`
- `item_type`: `package`, `addon`, `recurring`, `discount`, `custom`
- `category`
- `name`
- `description`
- `quantity`
- `unit_price_cents`
- `line_total_cents`
- `recurring_interval`
- `is_optional`
- `is_included`
- `sort_order`
- `metadata_json`, including salesman notes and client visibility flags

Audit/site data available from `website_audits.findings_json`:

- Site audit:
  - `audit_mode`
  - `final_homepage_url`
  - `title`
  - `meta_description`
  - `pages_crawled`
  - `page_urls`
  - `visible_phone_numbers`
  - `tel_links`
  - `mailto_emails`
  - `visible_emails`
  - `forms`
  - `booking_links`
  - `service_page_links`
  - `contact_page_links`
  - `about_page_links`
  - `tracking`
  - `schema`
  - `technology`
- PageSpeed audits:
  - `score`
  - status
  - metrics in findings when present
- Visual review audit:
  - `visual_total_score`
  - `top_issues`
  - per-category issues
- Lead score data:
  - `top_reasons`
  - `signals.email_candidates`
  - `signals.business_domain_emails`
  - `signals.mobile_pagespeed_score`
  - `signals.desktop_pagespeed_score`
  - screenshot paths

Sales packet:

- `templates/dashboard/sales_packet.html` exists and is built from stored prospect/audit/quote-style data.
- It is explicitly not a contract or payment request.
- It may be useful as a source of sales context, but contract variables should be quote/prospect/contact driven.

Suggested contract variable strategy:

- Auto-fill:
  - display business name from prospect/quote
  - website URL
  - phone
  - address when present
  - quote/package/scope/line items/totals/deposit/term
  - client contact name/email/phone from quote or primary contact
- Manual confirmation required:
  - legal business name
  - entity type, such as LLC, sole proprietor, corporation
  - signer legal name
  - signer title/authority
  - additional members/signers
  - billing address if different
  - effective date/start date
  - contract-specific terms that have legal effect
- Never infer:
  - LLC membership
  - legal owner identity
  - authority to sign
  - licensing/insurance/warranty claims

## 8. Existing DocuSign Environment Contract

`.env.example` has DocuSign placeholders:

```text
DOCUSIGN_ENVIRONMENT=demo
DOCUSIGN_AUTH_SERVER=account-d.docusign.com
DOCUSIGN_BASE_PATH=https://demo.docusign.net/restapi
DOCUSIGN_ACCOUNT_ID=
DOCUSIGN_INTEGRATION_KEY=
DOCUSIGN_SECRET_KEY=
DOCUSIGN_REDIRECT_URI=
DOCUSIGN_USER_ID=
DOCUSIGN_RSA_PRIVATE_KEY_PATH=secrets/docusign_private_key.pem
DOCUSIGN_SCOPES=signature impersonation
```

Recommended implementation stance:

- Keep secrets in `.env` and private key in `secrets/`.
- Add DocuSign config loading in a small module, not scattered through route handlers.
- Do not log private keys, access tokens, or full envelope payloads with sensitive contract contents.
- Prefer a service layer such as `src/docusign_client.py`.
- Add a dry-run or draft-envelope mode before sending live envelopes.
- Store DocuSign envelope IDs/statuses locally.

DocuSign official docs to have the implementation agent verify before coding:

- Remote signing from a document: https://developers.docusign.com/docs/esign-rest-api/how-to/request-signature-email-remote/
- Remote signing from a template: https://developers.docusign.com/docs/esign-rest-api/how-to/request-signature-template-remote/
- Composite templates concept: https://developers.docusign.com/docs/esign-rest-api/esign101/concepts/templates/composite/
- Tabs concept: https://developers.docusign.com/docs/esign-rest-api/esign101/concepts/tabs/
- Recipients concept: https://developers.docusign.com/docs/esign-rest-api/esign101/concepts/recipients/
- JWT auth: https://developers.docusign.com/platform/auth/jwt/
- Individual consent for JWT: https://developers.docusign.com/platform/auth/consent/obtaining-individual-consent/

Useful DocuSign design note:

- DocuSign supports creating envelopes from runtime documents and from templates.
- Composite templates are the likely hybrid if the app needs to generate a custom document while applying reusable DocuSign template tabs/roles.
- Tabs are the signature/text/date/initial/etc fields. Signers/roles and tab placement should be modeled explicitly.

## 9. Contract Architecture Recommendation

Recommended approach for this app:

Use app-generated contracts as the source of truth, then send them to DocuSign as envelope documents with anchor-based tabs or composite templates. This fits the CRM goal best because the salesman needs to confirm variables and optionally inject sections for multi-member LLCs or special cases.

Why this is better than a single DocuSign-only template:

- The CRM already owns quote/package/scope data.
- Salesman can review and edit contract variables before sending.
- Dynamic sections are easier to render in the app than to hide/show inside a fixed DocuSign template.
- The contract artifact can be stored locally and attached to the lead timeline.
- DocuSign becomes the signing transport and certificate source, not the only contract editor.

Viable implementation options:

Option A - generated document per contract, DocuSign tabs added by API:

- Generate DOCX or HTML/PDF from a contract template in app.
- Include anchor strings such as `/signer1_sign/`, `/signer1_date/`, `/company_sign/`, etc., or attach tabs by coordinates.
- Create a DocuSign envelope with the generated document.
- Add recipients/tabs dynamically based on signer list.
- Best for dynamic sections and CRM-controlled previews.

Option B - single DocuSign server template:

- Create one DocuSign template with fields/signatures.
- App creates envelope from template roles and fills tab values.
- Faster for a fixed one-signer document.
- Weak fit for dynamic sections and variable legal text unless all variations can be expressed as optional tabs/text blocks.

Option C - composite template hybrid:

- Use a generated document from the app.
- Apply a DocuSign server template for roles/tabs.
- Also pass runtime recipients and tab values.
- Best long-term if DocuSign template admins want to control signature placement while app controls contract content.

Recommended first implementation path:

1. Build contract data model and local document generation with no DocuSign send.
2. Add CRM UI for contract builder/review from quote.
3. Add DocuSign client in draft mode or demo mode.
4. Add envelope send/status sync.
5. Add webhook/status pull later if needed.

## 10. Suggested New Files

Likely new source files:

```text
src/contracts.py
src/contract_exports.py
src/docusign_client.py
```

Likely new templates:

```text
templates/dashboard/contracts_list.html
templates/dashboard/contract_builder.html
templates/dashboard/contract_detail.html
templates/dashboard/contract_preview.html
```

Likely new private generated outputs:

```text
runs/latest/contracts/<contract_key>/contract.docx
runs/latest/contracts/<contract_key>/contract.pdf
runs/latest/contracts/<contract_key>/contract.html
runs/latest/contracts/<contract_key>/docusign_payload_redacted.json
```

Possible template storage:

```text
contract_templates/
  service_contract.docx
```

or:

```text
templates/contracts/
  service_contract.html.j2
```

Do not put contract templates under `templates/dashboard/` unless they are UI templates.

## 11. Suggested New Tables

Add these in `src/db.py` with idempotent `ensure_contract_schema()` and a `ensure_contract_schema_for_path()` call from `create_app()`.

Minimal tables:

```text
contracts
  id INTEGER PRIMARY KEY
  contract_key TEXT UNIQUE NOT NULL
  prospect_id INTEGER NOT NULL
  quote_id INTEGER
  owner_username TEXT
  market_state TEXT
  status TEXT NOT NULL DEFAULT 'draft'
  title TEXT
  template_key TEXT
  version INTEGER DEFAULT 1
  legal_business_name TEXT
  business_entity_type TEXT
  signer_name TEXT
  signer_title TEXT
  signer_email TEXT
  signer_phone TEXT
  client_business_name TEXT
  client_contact_name TEXT
  client_email TEXT
  client_phone TEXT
  website_url TEXT
  effective_date TEXT
  start_date TEXT
  term_months INTEGER
  one_time_total_cents INTEGER DEFAULT 0
  recurring_monthly_total_cents INTEGER DEFAULT 0
  deposit_due_cents INTEGER DEFAULT 0
  balance_due_cents INTEGER DEFAULT 0
  variables_json TEXT DEFAULT '{}'
  sections_json TEXT DEFAULT '[]'
  signers_json TEXT DEFAULT '[]'
  generated_docx_path TEXT
  generated_pdf_path TEXT
  generated_html_path TEXT
  docusign_envelope_id TEXT
  docusign_status TEXT
  docusign_status_updated_at TEXT
  sent_at TEXT
  completed_at TEXT
  voided_at TEXT
  metadata_json TEXT DEFAULT '{}'
  created_at TEXT
  updated_at TEXT
```

```text
contract_events
  id INTEGER PRIMARY KEY
  contract_id INTEGER
  prospect_id INTEGER
  quote_id INTEGER
  event_type TEXT
  status TEXT
  note TEXT
  metadata_json TEXT
  created_at TEXT
```

Optional if DocuSign needs more detail:

```text
docusign_envelopes
  id INTEGER PRIMARY KEY
  contract_id INTEGER NOT NULL
  envelope_id TEXT UNIQUE NOT NULL
  status TEXT
  email_subject TEXT
  recipients_json TEXT DEFAULT '[]'
  tabs_json TEXT DEFAULT '{}'
  last_event_json TEXT DEFAULT '{}'
  created_at TEXT
  updated_at TEXT
```

Status suggestions:

```text
draft
generated
sent
delivered
completed
declined
voided
error
superseded
```

## 12. Suggested Routes

Use direct route decorators inside `create_app()` like the rest of `src/dashboard_app.py`.

Suggested routes:

```text
GET  /contracts
GET  /quotes/<int:quote_id>/contract/new
POST /quotes/<int:quote_id>/contract
GET  /contracts/<int:contract_id>
GET  /contracts/<int:contract_id>/edit
POST /contracts/<int:contract_id>/edit
POST /contracts/<int:contract_id>/generate
GET  /contracts/<int:contract_id>/preview
GET  /contracts/<int:contract_id>/download/docx
GET  /contracts/<int:contract_id>/download/pdf
POST /contracts/<int:contract_id>/send-docusign
POST /contracts/<int:contract_id>/void
POST /contracts/<int:contract_id>/refresh-docusign-status
POST /webhooks/docusign
```

Access rules:

- Gate routes with `quotes` permission at first, or add a dedicated `contracts` permission.
- Always load contract -> quote/prospect -> `require_prospect_access(prospect_id)`.
- Include CSRF input on all dashboard POST forms.
- Webhook route needs its own authentication/validation strategy rather than dashboard CSRF.

## 13. Suggested UI Integration

Quote detail page:

- Add a "Create Contract" button after quote accepted or always visible with a warning while draft/sent.
- Show linked contract status if a contract exists.
- Suggested source: `templates/dashboard/quote_detail.html`.

Case page:

- Add a contracts panel near the quote summary.
- Show latest contract status, generated path, DocuSign envelope ID/status, and open/send actions.
- Suggested source: `templates/dashboard/case.html`.

Tasks:

- Update closed-won auto task notes to point to the new contract action once contracts exist.
- Optional: when contract is sent, create follow-up task.
- Optional: when contract completed, create project handoff or deposit verification task.

Nav:

- `templates/dashboard/base.html` currently has a Quotes nav entry behind `dashboard_permissions.quotes`.
- Contracts can initially live under quote/case pages; add global nav only if needed.

## 14. Contract Variable Model

Define a canonical variable map in `src/contracts.py` so templates, preview, DocuSign payloads, and future prompts all use the same names.

Suggested variable groups:

```text
business.*
  display_name
  legal_name
  entity_type
  website_url
  phone
  address_line
  city
  state
  postal_code

signer_primary.*
  name
  title
  email
  phone

signers[]
  role
  name
  title
  email
  required
  signature_anchor
  date_anchor

quote.*
  quote_key
  package_name
  one_time_total
  recurring_monthly_total
  deposit_due
  balance_due
  term_months
  valid_until

scope.*
  included_items[]
  optional_items[]
  recurring_items[]
  assumptions[]

contract.*
  contract_key
  effective_date
  start_date
  template_key
  version
  additional_sections[]
```

For salesman-added sections:

```json
[
  {
    "section_key": "additional_member_signature",
    "title": "Additional Member Signature",
    "body": "Approved text...",
    "requires_signature": true,
    "signer_index": 2
  }
]
```

## 15. Prompt Engineering Plan For Parallelization

The work can be split efficiently if prompts use disjoint write scopes.

Prompt 1 - schema and contract service foundation:

- Files:
  - `src/db.py`
  - new `src/contracts.py`
- Tasks:
  - Add contract schema and migrations.
  - Add contract create/load/update/status/event helpers.
  - Add quote/prospect variable hydration helpers.
  - No UI and no DocuSign API calls.
- Acceptance:
  - `python -m src.db` includes contract table counts.
  - schema setup is idempotent.

Prompt 2 - document rendering/export:

- Files:
  - new `src/contract_exports.py`
  - new contract template folder/files
  - maybe `requirements.txt` if choosing `python-docx`, `docxtpl`, or PDF library
- Tasks:
  - Render a contract preview/export from a contract row.
  - Store generated artifacts under `runs/latest/contracts/<contract_key>/`.
  - Use the provided DOCX/PDF once available.
  - Add anchor strings for signer tabs if using generated-document DocuSign flow.
- Acceptance:
  - Generated doc/HTML/PDF can be created without DocuSign.

Prompt 3 - CRM routes and templates:

- Files:
  - `src/dashboard_app.py`
  - `templates/dashboard/contract_builder.html`
  - `templates/dashboard/contract_detail.html`
  - `templates/dashboard/contracts_list.html`
  - `templates/dashboard/case.html`
  - `templates/dashboard/quote_detail.html`
  - `static/dashboard.css`
- Tasks:
  - Add contract builder from quote.
  - Add edit/review/generate/download actions.
  - Add contract panels to quote detail and case pages.
  - Follow existing quote route style.
- Acceptance:
  - User can create/edit/generate a contract from a quote in the dashboard.

Prompt 4 - DocuSign client:

- Files:
  - new `src/docusign_client.py`
  - `requirements.txt`
  - maybe `.env.example` only if more keys are needed
- Tasks:
  - Load DocuSign config.
  - Implement auth token flow appropriate to configured env.
  - Create envelope from generated contract document.
  - Add recipients/tabs from contract signers.
  - Return envelope id/status.
  - Include safe test/dry-run behavior.
- Acceptance:
  - Unit-ish function can build a redacted envelope payload.
  - Sending is isolated to a single explicit function.

Prompt 5 - DocuSign route integration and status:

- Files:
  - `src/dashboard_app.py`
  - `src/contracts.py`
  - maybe `src/docusign_client.py`
  - templates/CSS touched by Prompt 3 if needed
- Tasks:
  - Add `send-docusign`, `refresh-docusign-status`, and webhook/status handling.
  - Store envelope id/status.
  - Log contract events and timeline entries.
  - Add tasks for sent/completed/failed if desired.
- Acceptance:
  - Contract can move from generated -> sent -> completed/declined/voided.

Prompt 6 - QA and hardening:

- Files:
  - focused tests or docs/QA report
  - possibly small fixes found during QA
- Tasks:
  - Compile/import check.
  - Schema idempotency check.
  - UI smoke with local dashboard.
  - Confirm no accidental send without explicit button.
  - Confirm no secrets logged.
  - Confirm access control and CSRF.

## 16. Do-Not-Touch / Caution List

Avoid unrelated churn in:

- `.env`
- `secrets/**`
- `data/leads.db` except through intended local migration/test flows
- `public_outreach/**`
- `runs/latest/public_packets/**`
- `screenshots/**`
- existing outreach copy/templates unless specifically asked
- existing lead pipeline/audit/page speed commands unless needed for contract variables

Be careful with:

- `.env.example`, because it already has uncommitted DocuSign additions.
- `config/users.yaml`, because it already has uncommitted permission changes.
- `src/dashboard_app.py`, because it is large and central. Prefer small helpers and new modules rather than stuffing all contract/DocuSign logic into route handlers.

## 17. Suggested Acceptance Criteria For The Overall Feature

Minimum local contract feature:

- From a quote detail page, a salesman can create a contract draft.
- Contract draft auto-fills from quote/prospect/contact data.
- Salesman can confirm/edit legal business name, signer, signer title, email, entity type, dates, terms, and additional sections/signers.
- Contract can be generated locally without sending to DocuSign.
- Generated files live under `runs/latest/contracts/<contract_key>/`.
- Contract events appear in case history or contract detail history.
- Case and quote pages show contract status.

Minimum DocuSign feature:

- App can create/send a DocuSign envelope from a generated contract.
- App stores `docusign_envelope_id` and status.
- Envelope has at least one required signer signature/date field.
- Additional signers/sections can add extra signer tabs.
- There is an explicit send action; generating a contract does not send it.
- There is a safe failure state and user-visible error message.
- No secret/token/private key is printed into logs or UI.

Recommended final ecosystem behavior:

- CRM produces quote.
- Accepted or ready quote produces contract.
- Contract sends through DocuSign.
- Signed/completed contract updates CRM tasks and status.
- Salesman can see quote, contract, DocuSign envelope, and next tasks from the case page.

## 18. Important Open Questions For The User

Ask these before final implementation, or make conservative defaults:

1. Is the contract template legally final, and will it be DOCX, PDF, or HTML source?
2. Should the app generate a finalized PDF before DocuSign, or upload DOCX directly?
3. Should the signer be remote email signing only, or embedded signing inside the CRM?
4. Should DocuSign auth use JWT impersonation or Authorization Code Grant?
5. Should contracts require quote status `accepted`, or can they be created from draft/sent quotes?
6. Should contracts require deposit/payment flow now, or only task reminders?
7. Should multiple signers sign sequentially or in parallel?
8. Should internal counters/contract numbers be sequential or random like quote keys?
9. Should contract generation be available to all users with `quotes` permission?
10. Should signed PDFs/certificates be downloaded and stored locally after completion?

## 19. Suggested First Prompt To Give GPT

Use this after supplying the actual contract template file:

```text
You are working in:
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads

Read docs/CONTRACT_DOCUSIGN_HANDOFF_PACKET.md first. Implement only the local contract foundation, not DocuSign sending yet.

Goal:
Add quote-attached CRM contracts that can be drafted from an existing quote, manually confirmed by the salesman, and generated locally.

Write scope:
- src/db.py
- new src/contracts.py
- new src/contract_exports.py if needed
- templates/dashboard/contract_builder.html
- templates/dashboard/contract_detail.html
- src/dashboard_app.py route additions
- templates/dashboard/case.html and quote_detail.html for links/status
- static/dashboard.css only for necessary styles

Requirements:
- Add idempotent contract schema.
- Link contracts to prospect_id and quote_id.
- Include owner_username and market_state for access scoping.
- Reuse require_prospect_access via a new require_contract_access helper.
- Add GET/POST flow to create a contract from a quote.
- Auto-fill from quote/prospect/contact, but make legal business name, entity type, signer name/title/email, and extra signers/sections manually editable.
- Generate a local preview/export under runs/latest/contracts/<contract_key>/.
- No DocuSign API calls.
- No email sending.
- Include csrf_input() in forms.
- Preserve existing quote behavior.
- Verify with python -m compileall src.

Return changed files and verification result.
```

Then follow with a separate DocuSign prompt once local contracts are stable.

