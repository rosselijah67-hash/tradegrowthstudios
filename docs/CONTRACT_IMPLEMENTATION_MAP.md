# Contract Implementation Map

Report-only inspection for CRM contracts and DocuSign integration. No code/config/database/API actions were performed.

## SECTION 1 - Existing quote integration points

Quote routes live in `src/dashboard_app.py` inside `create_app()` and are gated with `@require_dashboard_permission("quotes")`:

- `GET /quotes` -> `quotes_list`
- `GET /quotes/new` -> `new_quote`
- `POST /quotes/new` -> `create_quote`
- `GET /quotes/<int:quote_id>` -> `quote_detail`
- `GET /quotes/<int:quote_id>/edit` -> `edit_quote`
- `POST /quotes/<int:quote_id>/edit` -> `update_quote`
- `GET /quotes/<int:quote_id>/export/text` -> `quote_export_text`
- `GET /quotes/<int:quote_id>/export.txt` -> `quote_export_text_legacy`
- `GET /quotes/<int:quote_id>/export/html` -> `quote_export_html`
- `GET /quotes/<int:quote_id>/print` -> `quote_printable`
- `POST /quotes/<int:quote_id>/status` -> `update_quote_status`
- `POST /quotes/<int:quote_id>/mark-sent` -> `mark_quote_sent`
- `POST /quotes/<int:quote_id>/mark-accepted` -> `mark_quote_accepted`
- `POST /quotes/<int:quote_id>/mark-declined` -> `mark_quote_declined`
- `POST /quotes/<int:quote_id>/create-revision` and `/revision` -> `create_quote_revision`
- `POST /quotes/<int:quote_id>/delete` -> `delete_quote`

Available quote helpers in `src/quotes.py`:

- Constants: `VALID_QUOTE_STATUSES`, `VALID_ITEM_TYPES`, `VALID_RECURRING_INTERVALS`
- Catalog/money: `load_quote_catalog`, `format_money`, `parse_money_to_cents`
- Load/list: `get_quote`, `get_quote_by_key`, `list_quotes_for_prospect`, `list_quotes`
- Mutate: `create_quote_for_prospect`, `update_quote_header`, `replace_quote_line_items`, `update_quote_status`, `create_quote_revision`, `delete_quote`, `add_or_update_line_item`, `recalculate_quote_totals`
- Render/log: `render_quote_text`, `render_quote_print_html`, `log_quote_event`

Quote fields available for contract autofill from `src/db.py` `quotes` table:

- Identity/status: `id`, `quote_key`, `prospect_id`, `owner_username`, `market_state`, `version`, `status`, `supersedes_quote_id`
- Package/header: `package_key`, `package_name`, `title`
- Client: `client_business_name`, `client_contact_name`, `client_email`, `client_phone`, `website_url`
- Money/terms: `one_time_subtotal_cents`, `one_time_discount_cents`, `one_time_total_cents`, `recurring_monthly_total_cents`, `term_months`, `deposit_percent`, `deposit_due_cents`, `balance_due_cents`, `valid_until`
- Notes/json: `client_visible_notes`, `assumptions_json`, `internal_notes`, `metadata_json`
- Lifecycle timestamps: `created_at`, `updated_at`, `sent_at`, `accepted_at`, `declined_at`

Line items available from `quote_line_items`:

- `id`, `quote_id`, `item_key`, `item_type`, `category`, `name`, `description`, `quantity`, `unit_price_cents`, `line_total_cents`, `recurring_interval`, `is_optional`, `is_included`, `sort_order`, `metadata_json`, `created_at`, `updated_at`

Quote events are logged in two places:

- `src/quotes.py::log_quote_event()` inserts into `quote_events`.
- `src/dashboard_app.py::insert_quote_lifecycle_outreach_event()` inserts lifecycle timeline rows into `outreach_events` with `campaign_key='quote'`, `channel='dashboard'`, and `event_type='quote_event'`.
- `src/quote_exports.py::log_export_event()` calls `quotes.log_quote_event()` after export generation.

Case page quote loading:

- `src/dashboard_app.py::case_file(prospect_id)` calls `quote_service.list_quotes_for_prospect(get_connection(), prospect_id)`.
- It passes `quotes=quote_rows` and `latest_quote=quote_rows[0] if quote_rows else None` into `templates/dashboard/case.html`.

## SECTION 2 - Contract template availability

- `src/contracts.py`: not present.
- `contract_templates/`: not present.
- `service_contract.docx`: not found.
- Any contract DOCX/PDF/HTML template: not found in the requested template location.

No contract source template found. Implementation must support a required template path and fail gracefully until added.

## SECTION 3 - Schema extension plan

Add `CONTRACT_SCHEMA_SQL`, `ensure_contract_schema(connection)`, and `ensure_contract_schema_for_path(db_path=None)` in `src/db.py`. Call the path helper during `create_app()` startup near `ensure_quote_schema_for_path(...)`.

Recommended table: `contracts`

```text
id INTEGER PRIMARY KEY
contract_key TEXT UNIQUE NOT NULL
prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE
quote_id INTEGER REFERENCES quotes(id) ON DELETE SET NULL
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
term_months INTEGER DEFAULT 0
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

Recommended indexes:

- `idx_contracts_prospect ON contracts(prospect_id)`
- `idx_contracts_quote ON contracts(quote_id)`
- `idx_contracts_status ON contracts(status)`
- `idx_contracts_updated ON contracts(updated_at)`
- `idx_contracts_market_state ON contracts(market_state)`
- `idx_contracts_owner_username ON contracts(owner_username)`
- `idx_contracts_docusign_envelope ON contracts(docusign_envelope_id)` where not null/blank

Recommended table: `contract_events`

```text
id INTEGER PRIMARY KEY
contract_id INTEGER REFERENCES contracts(id) ON DELETE SET NULL
prospect_id INTEGER REFERENCES prospects(id) ON DELETE CASCADE
quote_id INTEGER REFERENCES quotes(id) ON DELETE SET NULL
event_type TEXT
status TEXT
note TEXT
metadata_json TEXT DEFAULT '{}'
created_at TEXT
```

Recommended indexes:

- `idx_contract_events_contract ON contract_events(contract_id)`
- `idx_contract_events_prospect ON contract_events(prospect_id)`
- `idx_contract_events_quote ON contract_events(quote_id)`
- `idx_contract_events_created ON contract_events(created_at)`

Recommended table if DocuSign details grow beyond `contracts`: `docusign_envelopes`

```text
id INTEGER PRIMARY KEY
contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE
envelope_id TEXT UNIQUE NOT NULL
status TEXT
email_subject TEXT
recipients_json TEXT DEFAULT '[]'
tabs_json TEXT DEFAULT '{}'
last_event_json TEXT DEFAULT '{}'
created_at TEXT
updated_at TEXT
```

Recommended indexes:

- `idx_docusign_envelopes_contract ON docusign_envelopes(contract_id)`
- `idx_docusign_envelopes_status ON docusign_envelopes(status)`

## SECTION 4 - Access control plan

Recommendation: reuse existing `quotes` permission for first pass. Contracts are quote-attached sales artifacts, and `src/auth.py` currently has only `markets`, `quotes`, `run_jobs`, `outbound`, and `send`. Adding a new `contracts` permission would require touching `src/auth.py`, `config/users.yaml`, admin user forms, and nav logic.

Existing helpers to reuse:

- `src/dashboard_app.py::require_dashboard_permission("quotes")`
- `src/dashboard_app.py::require_prospect_access(prospect_id)`
- `src/dashboard_app.py::require_quote_access(quote_id)`
- `src/dashboard_app.py::current_dashboard_user()`
- `src/dashboard_app.py::prospect_state_from_record(prospect)`
- `src/dashboard_app.py::sync_quote_territory_fields(...)` as the pattern for contracts

Proposed `require_contract_access(contract_id)`:

- Load contract via `contract_service.get_contract(get_connection(), contract_id)`.
- Abort 404 if missing.
- Use contract `prospect_id` to call `require_prospect_access(int(contract["prospect_id"]))`.
- Optionally load linked quote with `quote_service.get_quote(...)`; if present, verify `quote["prospect_id"] == contract["prospect_id"]`.
- Return `(contract, quote, prospect)`.

Owner/territory setting:

- When creating from quote, copy `owner_username` and `market_state` from the quote if present.
- If absent, copy from prospect.
- If still absent, use `current_dashboard_user().username` for `owner_username`.
- Use `prospect_state_from_record(prospect)` for `market_state`.

## SECTION 5 - Route plan

Add route decorators inside `src/dashboard_app.py::create_app()` near the existing quote routes:

- `GET /contracts` -> `contracts_list`
- `GET /quotes/<int:quote_id>/contract/new` -> `new_contract_from_quote`
- `POST /quotes/<int:quote_id>/contract` -> `create_contract_from_quote`
- `GET /contracts/<int:contract_id>` -> `contract_detail`
- `GET /contracts/<int:contract_id>/edit` -> `edit_contract`
- `POST /contracts/<int:contract_id>/edit` -> `update_contract`
- `POST /contracts/<int:contract_id>/generate` -> `generate_contract`
- `GET /contracts/<int:contract_id>/preview` -> `contract_preview`
- `GET /contracts/<int:contract_id>/download/docx` -> `download_contract_docx`
- `POST /contracts/<int:contract_id>/send-docusign` -> `send_contract_docusign`
- `POST /contracts/<int:contract_id>/refresh-docusign-status` -> `refresh_contract_docusign_status`

All dashboard POST routes should include `{{ csrf_input() }}` in forms. No DocuSign send should happen except `send_contract_docusign`.

## SECTION 6 - Template/UI integration points

`templates/dashboard/quote_detail.html`:

- Add "Create Contract" in the top `.quote-link-row` beside `Edit Quote`, `Copy Email Text`, and `Printable Quote`.
- Also add a contract status panel near the existing `Actions` panel once contracts can be loaded into the route.

`templates/dashboard/case.html`:

- Existing quote summary starts immediately after `<section class="case-header">`.
- Add a contract panel after the quote summary panel so quote -> contract progression is visible above the deep audit/task sections.
- If no contract exists but `latest_quote` exists, show "Create Contract" linked to `/quotes/<latest_quote.id>/contract/new`.

`templates/dashboard/base.html`:

- No global Contracts nav is required in first pass. Keep contracts reachable from Quote and Case.
- If a global contracts list proves useful later, add nav behind `dashboard_permissions.quotes` or a future `dashboard_permissions.contracts`.

Dashboard templates to create:

- `templates/dashboard/contracts_list.html`
- `templates/dashboard/contract_builder.html`
- `templates/dashboard/contract_detail.html`
- `templates/dashboard/contract_preview.html`

CSS:

- Extend `static/dashboard.css` near existing quote styles around `.quote-summary-panel`, `.quote-detail-grid`, `.quote-link-row`, `.quote-builder-form`, and `.quote-actions-panel`.
- Prefer parallel contract class names: `.contract-summary-panel`, `.contract-detail-grid`, `.contract-link-row`, `.contract-builder-form`, `.contract-actions-panel`.

## SECTION 7 - Contract variable map

Canonical keys for `src/contracts.py` and generated documents:

```text
business.display_name
business.legal_name
business.entity_type
business.website_url
business.phone
business.address_line
business.city
business.state
business.postal_code
```

```text
signer_primary.name
signer_primary.title
signer_primary.email
signer_primary.phone
```

```text
signers[]
  role
  name
  title
  email
  phone
  required
  routing_order
  signature_anchor
  date_anchor
```

```text
quote.quote_key
quote.package_key
quote.package_name
quote.title
quote.one_time_subtotal
quote.one_time_discount
quote.one_time_total
quote.recurring_monthly_total
quote.deposit_percent
quote.deposit_due
quote.balance_due
quote.term_months
quote.valid_until
```

```text
scope.included_items[]
scope.optional_items[]
scope.recurring_items[]
scope.assumptions[]
scope.client_visible_notes
```

```text
contract.contract_key
contract.title
contract.template_key
contract.version
contract.effective_date
contract.start_date
contract.additional_sections[]
contract.generated_docx_path
contract.generated_pdf_path
contract.generated_html_path
```

Manual confirmation required before generation/send:

- `business.legal_name`
- `business.entity_type`
- `signer_primary.name`
- `signer_primary.title`
- `signer_primary.email`
- any additional `signers[]`
- any `contract.additional_sections[]`

## SECTION 8 - DocuSign design

Recommended first implementation:

- Generate a DOCX contract from the CRM contract record.
- Include anchor strings for signatures/dates, for example `/signer1_sign/`, `/signer1_date/`, `/signer2_sign/`, `/signer2_date/`.
- Create a DocuSign envelope at runtime from the generated document.
- Add signer recipients and anchor tabs dynamically from `signers_json`.
- Do not use composite templates in first pass unless the provided DocuSign account/template requires it.
- Do not send during contract generation.
- Only `POST /contracts/<int:contract_id>/send-docusign` should perform a real send.
- Use DocuSign demo settings from `.env.example` first; keep private key/token material in `.env`/`secrets/**`.

DocuSign client should live in new `src/docusign_client.py`, with route handlers only calling explicit service functions.

## SECTION 9 - Implementation order

1. Schema/service
   - Add `contracts`/`contract_events` schema in `src/db.py`.
   - Add `src/contracts.py` with create/load/update/status/event helpers and quote/prospect autofill.

2. Local generation
   - Add `src/contract_exports.py`.
   - Require `contract_templates/service_contract.docx` or configured template path.
   - Fail gracefully if missing.
   - Write generated files under `runs/latest/contracts/<contract_key>/`.

3. Dashboard UI
   - Add contract routes in `src/dashboard_app.py`.
   - Create contract templates.
   - Add quote detail and case page links/panels.
   - Add minimal CSS beside existing quote styles.

4. DocuSign client
   - Add `src/docusign_client.py`.
   - Load env config.
   - Build redacted payloads and explicit send function.

5. DocuSign route integration
   - Add send/status refresh routes.
   - Store envelope id/status.
   - Log contract events and case timeline entries.

6. QA
   - Run import/compile checks.
   - Verify schema idempotency.
   - Verify UI create/edit/generate.
   - Verify no send occurs except explicit DocuSign send route.
   - Verify access control, CSRF, and no secret logging.

## SECTION 10 - Do-not-touch list

Avoid these unless explicitly required by the specific implementation prompt:

- `.env`
- `secrets/**`
- `data/leads.db` except local test/migration flows
- `public_outreach/**`
- `screenshots/**`
- `templates/outreach/**`
- existing outreach copy modules such as `src/outreach_drafts.py`, `src/send_outreach.py`, and `src/public_packets.py`
- lead/audit pipeline modules such as `src/places_pull.py`, `src/audit_site.py`, `src/pagespeed.py`, `src/screenshot_site.py`, and `src/score_leads.py`
- generated public packets and screenshot artifacts
