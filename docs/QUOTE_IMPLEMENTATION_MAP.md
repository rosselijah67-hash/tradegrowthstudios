# Quote Implementation Map

## SECTION 1 - Relevant Repo Structure

Quote implementation should touch these files/routes/functions:

- `src/db.py`
  - Extend `SCHEMA_SQL` with `quotes` and `quote_items`.
  - Add `ensure_quote_schema(connection)` and call it from `init_db()`.
  - Add quote persistence helpers near existing `upsert_artifact()` / `upsert_outreach_event()` helpers.
- `src/dashboard_app.py`
  - App init: call `ensure_quote_schema(app.config["DATABASE_PATH"])` or equivalent inside `create_app()`, alongside `dashboard_jobs.ensure_schema()` and `ensure_outreach_queue_schema()`.
  - New routes inside `create_app()`:
    - `GET /case/<int:prospect_id>/quote/new` -> quote builder page.
    - `POST /case/<int:prospect_id>/quote` -> create/save fresh quote.
    - `GET /quotes/<quote_key>` -> quote detail/edit page.
    - `POST /quotes/<quote_key>` -> update quote.
    - `POST /quotes/<quote_key>/status` -> mark sent/accepted/declined/void.
    - `GET /quotes/<quote_key>/email` -> copy-paste email text view.
    - `GET /quotes/<quote_key>/print` -> printable HTML view.
  - New helpers near existing dashboard loaders:
    - `load_quote_catalog()`
    - `load_quotes_for_prospect(prospect_id)`
    - `load_quote(quote_key)`
    - `calculate_quote_totals(items, discount)`
    - `save_quote(...)`
    - `write_quote_exports(...)`
    - `insert_quote_event(...)`
- `src/state.py`
  - Do not add quote lifecycle to `ProspectStatus` unless needed. Use existing CRM stages: `PROPOSAL_SENT`, `CLOSED_WON`, `CLOSED_LOST`.
  - Optional: add quote event constants beside `OutreachEventType` if the implementation wants named constants instead of raw strings.
- `templates/dashboard/case.html`
  - Insert a large `Create Quote` action in the existing header action area.
  - Add a quotes panel/list on the case page.
- New templates:
  - `templates/dashboard/quote_form.html`
  - `templates/dashboard/quote_detail.html`
  - `templates/dashboard/quote_print.html`
  - Optional: `templates/dashboard/quote_email.html`
- `templates/dashboard/base.html`
  - No required nav change for the first pass; quotes are lead-attached, not a global nav section.
- `templates/dashboard/crm.html`, `templates/dashboard/crm_stage.html`, `templates/dashboard/leads.html`
  - Optional second-pass indicators only, such as latest quote status/count. Do not block the first implementation on these.
- `static/dashboard.css`
  - Add quote builder/detail/print/copy styles using existing `.button`, `.panel`, `.case-header-actions`, `.detail-list`, `.pill`, and responsive grid patterns.
- New config:
  - `config/quote_catalog.yaml`

## SECTION 2 - Current Dashboard Route Pattern

- `src/dashboard_app.py` registers routes inside `create_app(db_path=None)`.
- Routes use decorators directly on the app, mostly `@app.get(...)`, `@app.post(...)`, and a few `@app.route(...)`.
- Templates render with `render_template("dashboard/<name>.html", active_page=..., ...)`.
- Mutating routes validate the prospect, use `get_connection()`, perform local SQLite writes, `connection.commit()`, then `redirect(url_for(..., result=...))`.
- The app does not currently use Flask blueprints.
- `create_app()` sets `template_folder`, `static_folder`, `DATABASE_PATH`, then runs schema setup:
  - `dashboard_jobs.ensure_schema(app.config["DATABASE_PATH"])`
  - `ensure_outreach_queue_schema(app.config["DATABASE_PATH"])`
  - `dashboard_jobs.mark_stale_jobs(app.config["DATABASE_PATH"])`
- `get_connection()` opens SQLite from `current_app.config["DATABASE_PATH"]`, sets `row_factory = sqlite3.Row`, and enables `PRAGMA foreign_keys = ON`.

Quote routes should follow the same direct app route pattern and live near the existing CRM/case/sales-packet routes.

## SECTION 3 - Current Case Page Integration Point

Primary insertion point:

- `templates/dashboard/case.html`, inside `<section class="case-header">`.
- Existing action container: `<div class="case-header-actions">`.
- Insert `Create Quote` before the conditional `Sales Packet` button so it is always visible near the top:
  - Target route: `url_for("new_quote", prospect_id=prospect.id)`.
  - Use a prominent existing style first: `class="button primary quote-create-button"`.

Case template variables currently available from `case_file(prospect_id)`:

- `prospect`
- `artifacts`, `artifact_map`
- `outreach_drafts`, `step_one_draft`, `followup_drafts`
- `public_packet_status`
- `contacts`, `primary_contact`
- `stage_history`
- `crm_stages`
- `audits`, `audit_map`
- `site_audit`, `site_findings`
- `score_explanation`, `signals`, `top_reasons`
- `email_candidates`, `business_domain_emails`
- `visual_review`, `visual_findings`, `visual_issue_categories`, `visual_issue_map`, `top_visual_issues`
- `review_message`

Quote integration should add:

- `quotes=load_quotes_for_prospect(prospect_id)`
- Optional `latest_quote`

Place the quotes list immediately after the header or before the existing `case-crm-grid`, so created quotes are visible without scrolling deep into audit evidence.

## SECTION 4 - Current CRM/Status/Event Conventions

Current status model:

- `src/state.py` defines `ProspectStatus`, `NextAction`, `CRM_STAGES`, and `CRM_NEXT_ACTIONS`.
- CRM stages are derived by `compute_pipeline_stage(row)`, primarily from `prospects.status` and `prospects.next_action`.
- Existing CRM stage statuses include `CONTACT_MADE`, `CALL_BOOKED`, `PROPOSAL_SENT`, `CLOSED_WON`, `CLOSED_LOST`, `PROJECT_ACTIVE`, and `PROJECT_COMPLETE`.
- `apply_crm_stage_change()` updates `prospects.status`, `prospects.next_action`, `updated_at`, then calls `insert_crm_stage_event()`.
- `insert_crm_stage_event()` writes to `outreach_events` with:
  - `campaign_key = 'crm'`
  - `channel = 'dashboard'`
  - `event_type = 'crm_stage_change'`
  - `status = 'recorded'`
  - `metadata_json` containing old/new status, next action, note, and source.
- `load_stage_history()` currently shows events where `event_type = 'crm_stage_change' OR channel = 'email'`.

Recommended quote event logging:

- Store quote status on the quote row, not in `prospects.status`.
- Log quote events in `outreach_events` for timeline continuity.
- Add `insert_quote_event(connection, quote, event_type, status="recorded", metadata=None)`.
- Use:
  - `campaign_key = 'quote'`
  - `channel = 'dashboard'`
  - `event_type = quote_created | quote_updated | quote_exported | quote_marked_sent | quote_accepted | quote_declined`
  - `status = recorded`, except `quote_marked_sent` can use `sent` if desired.
  - `subject = quote.title` or formatted quote label.
  - `body_path = export path` for `quote_exported`.
  - `metadata_json` should include `quote_id`, `quote_key`, `quote_status`, `one_time_total_cents`, `monthly_total_cents`, and old/new quote status.
- Extend `load_stage_history()` to include `channel = 'quote'` or the explicit quote event types.
- CRM stage effects:
  - `quote_created`, `quote_updated`, `quote_exported`: do not change `prospects.status`.
  - `quote_marked_sent`: update prospect to `PROPOSAL_SENT` via `apply_crm_stage_change()` unless already in `CLOSED_WON`, `PROJECT_ACTIVE`, or `PROJECT_COMPLETE`.
  - `quote_accepted`: update prospect to `CLOSED_WON`.
  - `quote_declined`: update prospect to `CLOSED_LOST`.

## SECTION 5 - Current DB Initialization Pattern

Current pattern:

- `src/db.py` defines `SCHEMA_SQL` with `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`.
- `init_db()` runs `connection.executescript(SCHEMA_SQL)`, then idempotent schema patches:
  - `ensure_prospect_place_columns(connection)`
  - `ensure_artifact_columns(connection)`
- Column migrations check `PRAGMA table_info(...)` before `ALTER TABLE ... ADD COLUMN`.
- Dashboard-only schema setup currently exists too: `ensure_outreach_queue_schema(db_path)` in `src/dashboard_app.py`.

Quote tables should be created idempotently:

- Preferred: put quote tables/indexes in `src/db.py` `SCHEMA_SQL`.
- Add `ensure_quote_schema(connection)` only for follow-up column/index migrations.
- Ensure dashboard startup also creates quote tables, because `create_app()` may run without a CLI `db.init_db()` call.

Recommended tables:

- `quotes`
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `quote_key TEXT NOT NULL UNIQUE`
  - `prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE`
  - `status TEXT NOT NULL DEFAULT 'draft'`
  - `package_key TEXT`
  - `title TEXT`
  - `client_name TEXT`
  - `one_time_subtotal_cents INTEGER NOT NULL DEFAULT 0`
  - `discount_cents INTEGER NOT NULL DEFAULT 0`
  - `one_time_total_cents INTEGER NOT NULL DEFAULT 0`
  - `monthly_total_cents INTEGER NOT NULL DEFAULT 0`
  - `client_notes TEXT`
  - `internal_notes TEXT`
  - `metadata_json TEXT NOT NULL DEFAULT '{}'`
  - `sent_at TEXT`, `accepted_at TEXT`, `declined_at TEXT`
  - `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`
- `quote_items`
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE`
  - `item_key TEXT`
  - `item_type TEXT NOT NULL`
  - `title TEXT NOT NULL`
  - `description TEXT`
  - `quantity REAL NOT NULL DEFAULT 1`
  - `unit_price_cents INTEGER NOT NULL DEFAULT 0`
  - `interval TEXT NOT NULL DEFAULT 'one_time'`
  - `sort_order INTEGER NOT NULL DEFAULT 0`
  - `metadata_json TEXT NOT NULL DEFAULT '{}'`

## SECTION 6 - Config Pattern

Current config pattern:

- `src/config.py` defines `CONFIG_DIR = PROJECT_ROOT / "config"`.
- `load_yaml_config(filename)` reads `config/<filename>`.
- Existing config files are top-level YAML mappings: `markets.yaml`, `niches.yaml`, `outreach.yaml`, `scoring.yaml`.
- `src/dashboard_app.py` already imports `load_yaml_config`.

Quote catalog should live in:

- `config/quote_catalog.yaml`

Recommended shape:

- `base_packages`
  - `site_refresh`: `label: Site Refresh`, `price_cents: 300000`, `floor_cents: 200000`
  - `growth_rebuild`: `min_cents: 350000`, `max_cents: 500000`
  - `conversion_system`: `min_cents: 500000`, `max_cents: 1000000`
  - `engineered_build`: `custom_only: true`, `min_cents: 1000000`, `requires_discovery: true`
- `recurring`
  - `hosting_monitoring`: about `14900` monthly cents
  - `managed_web_ops`: about `39900` monthly cents
  - `growth_ops`: minimum `90000` monthly cents
- `addons`
  - service-page architecture
  - service-area architecture
  - tracking-ready setup
  - conversion-path improvements
  - managed content/support items
- `positioning`
  - Allowed language: website replacement, audit-backed redesign, mobile-first call/request path, conversion infrastructure, service-page and service-area architecture, tracking-ready managed web operations, custom systems for engineered builds.
  - Banned language: AI websites.

Do not put quote pricing inside `config/outreach.yaml`; quote pricing is CRM/proposal data, not outbound sequence configuration.

## SECTION 7 - Export Pattern

Use:

- `runs/latest/quotes/<quote_key>/quote_email.txt`
- `runs/latest/quotes/<quote_key>/quote.html`

Reason:

- Private local generated artifacts already use `runs/latest/artifacts/<prospect_id>/` and `runs/latest/outreach_drafts/<prospect_id>/`.
- Public client-safe packets use `public_outreach/`.
- Quotes can contain pricing/internal context and should stay in private local output unless explicitly copied or printed.

Recommended persistence for exports:

- Recompute exports from SQLite quote data whenever possible.
- When writing export files, upsert `artifacts` rows for discoverability:
  - `artifact_type = 'quote_email_text'`
  - `artifact_type = 'quote_print_html'`
  - `artifact_key = '<quote_key>:email'` and `'<quote_key>:print'`
  - `prospect_id = quote.prospect_id`
  - `path = runs/latest/quotes/<quote_key>/...`
  - `status = 'ready'`
  - `metadata_json.quote_key = <quote_key>`
- Log `quote_exported` in `outreach_events` with `body_path` set to the generated file path.

## SECTION 8 - Implementation Plan

1. Pricing catalog and schema
   - Add `config/quote_catalog.yaml`.
   - Add quote tables/indexes in `src/db.py`.
   - Add quote loader/saver/calculation helpers.
   - Wire idempotent schema setup into dashboard startup.

2. Quote builder page
   - Add `GET /case/<int:prospect_id>/quote/new`.
   - Add `POST /case/<int:prospect_id>/quote`.
   - Add `templates/dashboard/quote_form.html`.
   - Base package, add-ons, recurring items, discounts, client-facing notes, and internal notes must be editable.

3. Quote detail/edit/export
   - Add `GET /quotes/<quote_key>`.
   - Add `POST /quotes/<quote_key>`.
   - Add printable route `GET /quotes/<quote_key>/print`.
   - Add copy-paste email route `GET /quotes/<quote_key>/email`.
   - Add local export writer to `runs/latest/quotes/<quote_key>/`.

4. Case/CRM integration
   - Add `Create Quote` to `templates/dashboard/case.html` header actions.
   - Add quotes list/panel to the case page.
   - Add quote events to `load_stage_history()`.
   - Add status actions for sent/accepted/declined and map them to CRM stages.

5. QA
   - Start with a local test DB copy.
   - Verify schema creation is idempotent.
   - Verify a quote can be created, edited, exported, marked sent, accepted, and declined.
   - Verify no email is sent.
   - Verify exported copy never says `AI websites`.
   - Verify browser print-to-PDF works from `/quotes/<quote_key>/print`.

## SECTION 9 - Do-Not-Touch List

Quote implementation should not modify:

- `templates/outreach/*.txt.j2`
- `src/outreach_drafts.py`
- `src/send_outreach.py`
- `src/public_packets.py`
- `templates/public_packet/index.html.j2`
- `static/public_packet.css`
- `scripts/deploy_public_packets_cloudflare.ps1`
- `scripts/deploy_public_packets_cloudflare.bat`
- `config/outreach.yaml`
- `config/markets.yaml`
- `config/niches.yaml`
- `config/scoring.yaml`
- `config/franchise_exclusions.yaml`
- `public_outreach/**`
- `runs/latest/outreach_drafts/**`
- `runs/latest/public_packets/**`
- `screenshots/**`
- Existing SQLite data rows outside the new quote tables and normal CRM/event updates caused by quote actions.

## SECTION 10 - Acceptance Criteria

Quote module is implemented when:

- Every `/case/<prospect_id>` page shows a prominent `Create Quote` button near the top.
- The button opens a fresh quote builder for that prospect.
- A quote can select one base package, add one-time add-ons, add recurring monthly items, apply discounts, and save internal/client-facing notes.
- Quote totals are stored in SQLite and attached to the prospect.
- The case page lists existing quotes for that prospect with status, one-time total, monthly total, updated time, and open/export actions.
- Quote detail page supports edit and status changes.
- Printable HTML export works through browser print-to-PDF.
- Copy-paste email text export is available and locally written under `runs/latest/quotes/<quote_key>/`.
- Quote events appear in case history.
- Marking a quote sent moves the prospect to `PROPOSAL_SENT`; accepted moves to `CLOSED_WON`; declined moves to `CLOSED_LOST`.
- Export/client copy uses website replacement, audit-backed redesign, mobile-first call/request path, conversion infrastructure, service-page/service-area architecture, tracking-ready managed web operations, and custom systems language.
- Export/client copy does not position the work as `AI websites`.
- Existing outreach, public packet, sending, market, scoring, screenshot, and deployment flows continue unchanged.
