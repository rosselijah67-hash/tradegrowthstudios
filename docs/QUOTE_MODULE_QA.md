# Quote Module QA

Method: static repo inspection only. No SQLite writes, email sends, external API calls, server starts, or compile/cache-generating commands were run for this audit.

## SECTION 1 — Route Inventory

Quote routes are registered directly inside `src/dashboard_app.py:create_app()`.

| Route | Handler | Reads DB | Writes DB | Exports files | Sends email |
| --- | --- | --- | --- | --- | --- |
| `GET /quotes` | `quotes_list()` | Yes: `quote_service.list_quotes()` | No | No | No |
| `GET /quotes/new?prospect_id=<id>` | `new_quote()` | Yes: prospect + contacts | No | No | No |
| `POST /quotes/new` | `create_quote()` | Yes: prospect + contacts + catalog | Yes: `quotes`, `quote_line_items`, `quote_events` | No | No |
| `GET /quotes/<quote_id>` | `quote_detail()` | Yes: quote + prospect | No | No | No |
| `GET /quotes/<quote_id>/edit` | `edit_quote()` | Yes: quote + prospect + contacts | No | No | No |
| `POST /quotes/<quote_id>/edit` | `update_quote()` | Yes | Yes: quote header, line items, `quote_updated` | No | No |
| `GET /quotes/<quote_id>/export/text` | `quote_export_text()` | Yes | Yes: `artifacts`, `quote_events` | Yes: `runs/latest/quotes/<quote_key>/quote.txt` | No |
| `GET /quotes/<quote_id>/export.txt` | `quote_export_text_legacy()` | No direct DB read | No | Redirect only | No |
| `GET /quotes/<quote_id>/export/html` | `quote_export_html()` | Yes | Yes: `artifacts`, `quote_events` | Yes: `runs/latest/quotes/<quote_key>/quote.html` | No |
| `GET /quotes/<quote_id>/print` | `quote_printable()` | No direct DB read | No | Redirect only | No |
| `POST /quotes/<quote_id>/status` | `update_quote_status()` | Yes | Yes: quote + optional CRM lifecycle | No | No |
| `POST /quotes/<quote_id>/mark-sent` | `mark_quote_sent()` | Yes | Yes: quote, prospect CRM status, `quote_events`, `outreach_events` | No | No |
| `POST /quotes/<quote_id>/mark-accepted` | `mark_quote_accepted()` | Yes | Yes: quote, prospect CRM status, `quote_events`, `outreach_events` | No | No |
| `POST /quotes/<quote_id>/mark-declined` | `mark_quote_declined()` | Yes | Yes: quote, optional prospect CRM close-lost, `quote_events`, `outreach_events` | No | No |
| `POST /quotes/<quote_id>/create-revision` | `create_quote_revision()` | Yes | Yes: new quote, copied line items, old quote superseded, events | No | No |
| `POST /quotes/<quote_id>/revision` | `create_quote_revision()` | Yes | Yes: legacy alias for create revision | No | No |

No quote route calls `send_dashboard_test_email()`, `send_dashboard_batch()`, SMTP, or outbound sender logic. Existing send routes remain separate in `src/dashboard_app.py`.

## SECTION 2 — Schema Audit

Schema location: `src/db.py`.

- `QUOTE_SCHEMA_SQL` defines `quotes`, `quote_line_items`, and `quote_events` with `CREATE TABLE IF NOT EXISTS`.
- Quote indexes use `CREATE INDEX IF NOT EXISTS`.
- `init_db()` calls `ensure_quote_schema(connection)`.
- Dashboard startup calls `pipeline_db.ensure_quote_schema_for_path(app.config["DATABASE_PATH"])` in `src/dashboard_app.py:create_app()`.
- This is idempotent for fresh and existing SQLite files.

Money storage:

- `quotes` uses cent columns: `one_time_subtotal_cents`, `one_time_discount_cents`, `one_time_total_cents`, `recurring_monthly_total_cents`, `deposit_due_cents`, `balance_due_cents`.
- `quote_line_items` uses `unit_price_cents` and `line_total_cents`.
- `src/quotes.py:parse_money_to_cents()` parses display dollars into cents.
- `src/quotes.py:format_money()` formats cents back to display money.

## SECTION 3 — Case Integration

File: `templates/dashboard/case.html`.

- Visible top action exists: `Create Quote` links to `url_for("new_quote", prospect_id=prospect.id)`.
- Quote panel appears near the top when `latest_quote` exists.
- Panel shows latest quote status, package, one-time total, monthly total, valid-until, updated time, and quote count.
- Panel actions include View, Edit, Export, Printable Quote, Create Revision, and New Quote.
- `src/dashboard_app.py:case_file()` loads `quote_rows = quote_service.list_quotes_for_prospect(...)` and passes `quotes` plus `latest_quote`.

## SECTION 4 — Builder Audit

Files: `templates/dashboard/quote_builder.html`, `src/dashboard_app.py`, `src/quotes.py`.

- Package selection: radio inputs named `package_key`; POST validation requires a known catalog package in `parse_quote_builder_form()`.
- Base package line item: `create_quote_for_prospect()` creates an initial package item; `replace_quote_line_items()` then saves the submitted package/add-on/recurring/custom set.
- Add-ons: rendered from `catalog.addon_groups`; selected by checkbox `name="addons"`.
- Quantity validation: `parse_quote_quantity()` rejects negative quantities and non-numeric values.
- Price validation: `parse_quote_price()` plus explicit checks reject negative package, add-on, recurring, and custom unit prices.
- Discount validation: discount must be non-negative, requires internal notes, and cannot exceed computed one-time subtotal.
- Recurring separation: recurring items use `item_type="recurring"` and `recurring_interval="monthly"`; `recalculate_quote_totals()` keeps `recurring_monthly_total_cents` separate from one-time totals.
- Notes separation: builder has separate `client_visible_notes`, `assumptions_text`, and `internal_notes`.
- Floor/fallback exposure: `quote_catalog_view()` intentionally omits `floor_price_cents`, `quote_range`, and fallback/floor language from the template context. The builder displays default/base prices, not internal floor prices.

## SECTION 5 — Export Audit

Files: `src/quote_exports.py`, `templates/dashboard/quote_printable.html`, `src/dashboard_app.py`.

- Text export works through `GET /quotes/<quote_id>/export/text`, rendered by `quote_exports.render_email_text()`.
- Printable HTML export works through `GET /quotes/<quote_id>/export/html`, rendered with `templates/dashboard/quote_printable.html`.
- Export files are written privately under `runs/latest/quotes/<quote_key>/quote.txt` and `runs/latest/quotes/<quote_key>/quote.html`.
- Export artifact rows are upserted as `quote_email_text` and `quote_print_html`.
- Export events are logged as `quote_exported_text` and `quote_exported_html` in `quote_events`.
- Client exports do not reference `internal_notes`, `pricing_warnings`, floor/fallback fields, score/audit internals, or CRM statuses.
- `quote_printable.html` receives a `quote` object in context, but the template does not render `quote.internal_notes`, `quote.metadata`, or floor/fallback fields.
- No email is sent. Text export is a returned plain text response for manual copy-paste only.

## SECTION 6 — CRM Lifecycle Audit

Files: `src/dashboard_app.py`, `src/quotes.py`, `src/state.py`, `templates/dashboard/quote_detail.html`.

- Mark sent:
  - Route: `POST /quotes/<quote_id>/mark-sent`.
  - Updates `quotes.status = sent` and `sent_at` through `quote_service.update_quote_status()`.
  - Logs `quote_marked_sent` in `quote_events`.
  - Updates prospect to `PROPOSAL_SENT` via `apply_crm_stage_change()` unless current status is `CLOSED_WON`, `CLOSED_LOST`, `PROJECT_ACTIVE`, or `PROJECT_COMPLETE`.
  - `src/state.py` maps `PROPOSAL_SENT` to `FOLLOW_UP_PROPOSAL`.
- Mark accepted:
  - Route: `POST /quotes/<quote_id>/mark-accepted`.
  - Updates quote to `accepted`, sets `accepted_at`, logs `quote_accepted`.
  - Updates prospect to `CLOSED_WON`.
  - `src/state.py` maps `CLOSED_WON` to `START_PROJECT`.
- Mark declined:
  - Route: `POST /quotes/<quote_id>/mark-declined`.
  - Updates quote to `declined`, sets `declined_at`, logs `quote_declined`.
  - Does not close the prospect lost unless `confirm_close_lost=1` is posted from the quote detail checkbox.
  - With confirmation, updates prospect to `CLOSED_LOST`; `src/state.py` maps that to `NONE`.
- Revision:
  - Route: `POST /quotes/<quote_id>/create-revision` plus legacy `/revision`.
  - `quote_service.create_quote_revision()` creates a new draft quote, copies line items, increments version, sets `supersedes_quote_id`, and marks the old quote `superseded`.
  - Logs `quote_revision_created` for the new quote and `quote_updated` for the old quote.
- CRM visibility:
  - `load_crm_prospects()` calls `attach_quote_summaries_to_prospects()`.
  - `templates/dashboard/crm.html` shows latest quote status, amount, monthly amount, and quote count on cards.
  - `templates/dashboard/crm_stage.html` shows quote indicators in the stage table.
- Timeline continuity:
  - Quote lifecycle actions also insert lightweight `outreach_events` rows with `event_type = quote_event`.
  - `load_stage_history()` includes `quote_event` and direct `quote_events`.

## SECTION 7 — Pricing Catalog Audit

File: `config/quote_catalog.yaml`.

Base packages present:

- `site_refresh`: Site Refresh.
- `growth_rebuild`: Growth Rebuild.
- `conversion_system`: Conversion System.
- `engineered_build`: Engineered Build, with `requires_custom_quote: true` and `requires_discovery: true`.

Recurring retainers present:

- `hosting_monitoring`.
- `managed_web_ops`.
- `growth_ops`.

Required add-on coverage:

- Extra pages: `extra_standard_page`.
- Service pages: `extra_service_page`, `service_page_4_pack`.
- Service-area pages: `service_area_5_pack`.
- Blog/content packs: `blog_page_4_posts`, `seo_content_10_pack`, `seo_content_20_pack`.
- Tracking: `ga4_gtm_search_console`, `ads_conversion_tracking_readiness`, `call_tracking_setup`.
- Forms: `custom_quote_form`.
- Calendar: `calendar_integration`.
- Gallery: `before_after_gallery`.
- Media/licensing: `photo_video_sourcing`, `licensed_stock_pack`.
- Copywriting: `copywriting_refresh`.
- Schema: `schema_foundation`.
- Migration: `redirect_migration_map`.
- Landing pages: `ads_landing_page`.
- Emergency module: `emergency_service_module`.
- Trust module: `financing_warranty_trust_module`.
- Calculators: `custom_calculator`.
- Dashboard/tools: `client_dashboard_or_internal_tool`.
- Multi-location: `multi_location_architecture`.
- CRM/integration: `crm_integration_workflow`.

Catalog notes:

- Floor fields are present only as internal guardrails in catalog data.
- Static inspection found `quote_catalog_view()` does not pass floor fields to builder templates.
- Export helpers do not read or render floor fields.

## SECTION 8 — P0/P1/P2 Issues

P0:

- None found in static inspection. No quote route sends email, quote tables are idempotent, and client exports do not render internal notes or floor/fallback pricing.

P1:

- Repeated export page refreshes create repeated `quote_exported_text` / `quote_exported_html` events. This is acceptable for traceability but can make quote history noisy before real client use. Relevant code: `src/dashboard_app.py:quote_export_text()`, `quote_export_html()`.
- Quote detail is an internal page and intentionally shows `internal_notes` and pricing warnings. Operators must use `Printable Quote`, not browser print from quote detail, for client-facing output. Relevant file: `templates/dashboard/quote_detail.html`.

P2:

- Legacy routes remain: `/quotes/<quote_id>/export.txt`, `/quotes/<quote_id>/print`, `/quotes/<quote_id>/status`, and `/quotes/<quote_id>/revision`. They are compatible redirects/aliases, but cleanup would reduce route surface.
- Legacy rendering helpers remain in `src/quotes.py`: `render_quote_text()` and `render_quote_print_html()`. Current routes use `src/quote_exports.py`; future callers should avoid the old helpers or they may bypass export file/event behavior.
- Editing a quote replaces line-item rows via `replace_quote_line_items()` rather than preserving line-item history. Use `Create Revision` for client-visible scope history.
- There is no committed automated test file for quote lifecycle/export behavior. Prior smoke validation was manual/runtime outside this report; adding tests would reduce regression risk.

## SECTION 9 — Final Verdict

- Can a quote be created from a case? Yes. `templates/dashboard/case.html` has a visible `Create Quote` button wired to `/quotes/new?prospect_id=<id>`.
- Can it be saved and attached to the prospect? Yes. `POST /quotes/new` writes `quotes` and `quote_line_items` with `prospect_id`.
- Can it be exported for email? Yes. `/quotes/<quote_id>/export/text` returns copy-paste email text and writes `runs/latest/quotes/<quote_key>/quote.txt`.
- Is any sensitive/internal pricing exposed? No client-facing exposure found. Builder/detail are internal; text and printable HTML exports do not render internal notes, metadata, or floor/fallback prices.
- Is it safe to use for first sales calls? Yes, with one operating rule: use the `Copy Email Text` or `Printable Quote` exports for clients, not the internal quote detail page.
