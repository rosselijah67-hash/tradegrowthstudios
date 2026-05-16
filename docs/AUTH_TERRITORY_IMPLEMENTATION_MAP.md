# AUTH AND TERRITORY IMPLEMENTATION MAP

## SECTION 1 — Current auth/session state

- Current auth exists, but it is optional, single-user dashboard auth in `src/dashboard_app.py`, not role-based multi-user auth.
- `create_app()` sets `app.config["SECRET_KEY"]` from `FLASK_SECRET_KEY`, then `SECRET_KEY`, then the hardcoded fallback `local-dashboard-dev-secret`. It does not use the required `APP_SECRET_KEY`.
- Auth is enabled by `dashboard_auth_enabled()` when `DASHBOARD_AUTH_ENABLED` is truthy or any of `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `DASHBOARD_PASSWORD_HASH` is set.
- Login uses `/login`, `dashboard_username()`, and `dashboard_password_matches()`. Hash verification uses `werkzeug.security.check_password_hash`. Plain `DASHBOARD_PASSWORD` fallback currently exists and should be removed for the new requirement.
- Flask sessions are used: `session["dashboard_authenticated"]` and `session["dashboard_username"]`; no role, allowed states, user id, or expiration is stored.
- `PUBLIC_AUTH_ENDPOINTS` are `login`, `health`, `static`, `public_packet_page`, and `public_packet_asset`.
- If auth is disabled, every dashboard route is public.
- If auth is enabled, these remain public: `/login`, `/health`, `/p/<token>/`, `/p/<token>/<path:filename>`, `/assets/<path:filename>`, and static assets.
- Mutating routes: `/admin/database/import`, `/admin/media/import`, `/review/<id>/delete`, `/trash/<id>/restore`, `/trash/purge-media`, `/outbound/queue`, `/send/test`, `/send/batch`, quote create/edit/export/status/revision/delete routes, `/sales-packet/<id>/notes`, `/run/full-pipeline`, `/jobs/start`, `/markets/add`, case review/stage/contact/visual-review/outreach-draft/save routes.

## SECTION 2 — Dashboard route inventory

| Route | Function | Purpose | Reads DB | Writes DB/files | External APIs | Can send email | Required scope |
|---|---|---:|---:|---:|---:|---:|---|
| `/login` GET/POST | `login` | sign in | no | session | no | no | public |
| `/logout` POST | `logout` | sign out | no | session | no | no | authenticated-any-user |
| `/health` GET | `health` | healthcheck | no | no | no | no | public |
| `/p/<token>/...` GET | `public_packet_page` | public packet files | no | no | no | no | public token route |
| `/assets/<path>` GET | `public_packet_asset` | public packet assets | no | no | no | no | public |
| `/admin/database` GET | `database_admin` | DB import page | no | no | no | no | admin-only |
| `/admin/database/import` POST | `database_import` | replace SQLite DB | no | DB file | no | no | admin-only |
| `/admin/media` GET | `media_admin` | media import page | no | no | no | no | admin-only |
| `/admin/media/import` POST | `media_import` | import media zip | no | files | no | no | admin-only |
| `/` GET | `overview` | counts and market summary | yes | no | no | no | territory-scoped |
| `/markets` GET | `markets` | market manager | yes | no | no | no | territory-scoped |
| `/markets/add` POST | `add_market` | append `config/markets.yaml` | no | config file | no | no | territory-scoped; deny with `"you do not own this market"` |
| `/run` GET | `run_controls` | run controls | yes | no | no | no | territory-scoped |
| `/run/full-pipeline` POST | `start_full_market_pipeline` | create and start full job | yes | `dashboard_jobs`, logs | via subprocess | no | territory-scoped |
| `/jobs` GET | `jobs` | job list | yes | no | no | no | territory-scoped |
| `/jobs/start` POST | `start_dashboard_job` | create and start job | yes | `dashboard_jobs`, logs | via subprocess | no | territory-scoped |
| `/jobs/<job_key>` GET | `job_detail` | job detail/log | yes | no | no | no | territory-scoped direct-ID guard |
| `/jobs/<job_key>/status` GET | `job_status` | job status JSON/log | yes | no | no | no | territory-scoped direct-ID guard |
| `/pipeline` GET | `pipeline` | redirect to Run | no | no | no | no | territory-scoped if `market` present |
| `/pipeline/run` POST | `run_pipeline_job` | retired runner redirect | no | no | no | no | authenticated-any-user |
| `/leads` GET | `leads` | lead table | yes | no | no | no | territory-scoped |
| `/review` GET | `review_queue` | manual review queue | yes | no | no | no | territory-scoped |
| `/review/<id>/delete` POST | `quick_delete_review_card` | reject/mark trash | yes | prospects/events | no | no | territory-scoped direct-ID guard |
| `/trash` GET | `trash_can` | trashed leads | yes | no | no | no | territory-scoped |
| `/trash/<id>/restore` POST | `restore_trash` | restore lead | yes | prospects | no | no | territory-scoped direct-ID guard |
| `/trash/purge-media` POST | `purge_trash_media` | purge due media | yes | prospects/files | no | no | territory-scoped; admin-only is safer unless purger is scoped |
| `/crm` GET | `crm` | CRM board | yes | no | no | no | territory-scoped |
| `/crm/stage/<stage>` GET | `crm_stage` | CRM stage list | yes | no | no | no | territory-scoped |
| `/case/<id>` GET | `case_file` | full prospect case | yes | no | no | no | territory-scoped direct-ID guard |
| `/case/<id>/review` POST | `record_case_review` | approve/reject/hold | yes | prospects/contacts/events | no | no | territory-scoped direct-ID guard |
| `/case/<id>/stage` POST | `update_case_stage` | CRM stage change | yes | prospects/events | no | no | territory-scoped direct-ID guard |
| `/case/<id>/contact` POST | `save_case_contact` | upsert primary contact | yes | contacts | no | no | territory-scoped direct-ID guard |
| `/case/<id>/visual-review` POST | `record_visual_review` | save visual audit | yes | website_audits | no | no | territory-scoped direct-ID guard |
| `/case/<id>/outreach-drafts` POST | `generate_case_outreach_drafts` | run `src.outreach_drafts` | yes | artifacts/files/log | no | no | territory-scoped direct-ID guard |
| `/case/<id>/drafts/regenerate` POST | `regenerate_case_outreach_drafts` | rerun drafts | yes | artifacts/files/log | no | no | territory-scoped direct-ID guard |
| `/case/<id>/draft/<step>/save` POST | `save_case_outreach_draft` | edit draft artifact | yes | artifacts/files | no | no | territory-scoped direct-ID guard |
| `/sales-packet/<id>` GET | `sales_packet` | sales packet view | yes | no | no | no | territory-scoped direct-ID guard |
| `/sales-packet/<id>/notes` POST | `save_sales_packet_notes` | save notes | yes | prospects | no | no | territory-scoped direct-ID guard |
| `/outbound` GET | `outbound` | outbound readiness | yes | no | no | no | territory-scoped |
| `/outbound/queue` POST | `create_outbound_queue` | queue step 1 sends | yes | outreach_queue | no | no | territory-scoped |
| `/send` GET | `send_page` | send queue/readiness | yes | no | no | no | territory-scoped |
| `/send/test` POST | `send_test_email` | arbitrary test email | no | log file | SMTP | yes | admin-only |
| `/send/batch` POST | `send_batch` | send queued email | yes | queue/prospects/events | SMTP | yes | territory-scoped; admin all |
| `/quotes` GET | `quotes_list` | quote list | yes | no | no | no | territory-scoped |
| `/quotes/new` GET/POST | `new_quote`, `create_quote` | create quote | yes | quotes/items/events | no | no | territory-scoped prospect guard |
| `/quotes/<id>` GET | `quote_detail` | quote detail | yes | no | no | no | territory-scoped quote guard |
| `/quotes/<id>/edit` GET/POST | `edit_quote`, `update_quote` | edit quote | yes | quotes/items/events | no | no | territory-scoped quote guard |
| `/quotes/<id>/export/text` GET | `quote_export_text` | write text export | yes | artifact/file/event | no | no | territory-scoped quote guard |
| `/quotes/<id>/export/html` GET | `quote_export_html` | write HTML export | yes | artifact/file/event | no | no | territory-scoped quote guard |
| `/quotes/<id>/print` GET | `quote_printable` | redirect export | yes | via export | no | no | territory-scoped quote guard |
| `/quotes/<id>/status` POST | `update_quote_status` | status change | yes | quotes/events/prospect stage | no | no | territory-scoped quote guard |
| `/quotes/<id>/mark-sent` POST | `mark_quote_sent` | mark sent | yes | quotes/events/prospect stage | no | no | territory-scoped quote guard |
| `/quotes/<id>/mark-accepted` POST | `mark_quote_accepted` | mark accepted | yes | quotes/events/prospect stage | no | no | territory-scoped quote guard |
| `/quotes/<id>/mark-declined` POST | `mark_quote_declined` | mark declined | yes | quotes/events/prospect stage | no | no | territory-scoped quote guard |
| `/quotes/<id>/revision` POST | `create_quote_revision` | create revision | yes | quotes/items/events | no | no | territory-scoped quote guard |
| `/quotes/<id>/delete` POST | `delete_quote` | delete quote | yes | quotes/items/events | no | no | territory-scoped quote guard |
| `/media/<path>` GET | `project_media` | serve media file | no | no | no | no | authenticated; territory-scope if path maps to artifact |
| `/files/<path>` GET | `project_file` | serve project file | no | no | no | no | authenticated; restrict sensitive/log/artifact paths |

## SECTION 3 — Current market model

- `config/markets.yaml` is a top-level `markets:` mapping keyed by market key.
- Current keys are lowercase/underscore style: `mckinney_tx`, `cincinnati`, `dayton`, `columbus_oh`, `akron_oh`, `cleveland_oh`.
- `MARKET_KEY_PATTERN` is `^[a-z0-9_]+$`; `generate_market_key(label, state)` builds `<label>_<state>` lowercased, non-alphanumeric collapsed to `_`, max 64 chars.
- Markets currently have a `state` field. Existing values are two-letter codes such as `TX` and `OH`.
- `add_market_from_form()` validates state as `[A-Z]{2}` and writes it as uppercase.
- Some market config uses `included_cities`; older entries can use `cities`; `load_configured_markets()` accepts either.
- Market dropdowns are populated by `build_market_options()` from `load_configured_markets()`, plus selected unconfigured values and `UNKNOWN_MARKET_VALUE`.
- Query param market inputs are accepted by `selected_market_from_request()`, `outbound_filters_from_request()`, `/leads`, `/crm`, `/crm/stage`, `/review`, `/trash`, `/run`, `/jobs`, and redirect helpers.
- Market form submissions occur in `add_market_from_form()`, `start_job_from_form()`, `start_full_pipeline_from_form()`, `create_outbound_queue()`, and hidden form fields in `templates/dashboard/run.html`, `review.html`, and `outbound.html`.

## SECTION 4 — Current data model

Current `src/db.py` tables already in one SQLite DB:

- `prospects`: has `market`, `niche`, `city`, `state`, `city_guess`, `state_guess`; no user/owner/canonical-state field.
- `dashboard_jobs`: has `market`, `niche`; no actor, role, state, or allowed-state snapshot.
- `outreach_queue`: linked to `prospect_id`; no actor/state column.
- `quotes`: linked to `prospect_id`; no actor/state column.
- `quote_line_items`: linked to `quote_id`; derive territory through `quotes.prospect_id`.
- `quote_events`: has `quote_id` and `prospect_id`; derive territory through prospect.
- `outreach_events`: linked to `prospect_id`; derive territory through prospect.
- `contacts`: linked to `prospect_id`; derive territory through prospect.
- `artifacts`: linked to `prospect_id`; derive territory through prospect when present.
- `website_audits`: linked to `prospect_id`; derive territory through prospect.
- `suppression_list`: global; no market/state/user. Treat as admin-managed global compliance unless adding per-territory suppression UI.

Recommended columns:

- `prospects.canonical_state TEXT`: required. Backfill from `config/markets.yaml[market].state`, then `state`, then `state_guess`, normalized to two-letter uppercase.
- `dashboard_jobs.actor_username TEXT`: required for auditability.
- `dashboard_jobs.actor_role TEXT`: optional but useful snapshot.
- `dashboard_jobs.market_state TEXT`: required for job-list filtering and direct job guards.
- `dashboard_jobs.actor_allowed_states_json TEXT`: optional snapshot to explain historic job visibility.
- `outreach_queue.created_by_user TEXT`: recommended for auditability; territory can still be derived by joining `prospects`.
- `quotes.created_by_user TEXT` and `quotes.updated_by_user TEXT`: recommended for auditability; territory can still be derived by joining `prospects`.
- Add indexes: `idx_prospects_canonical_state_market ON prospects(canonical_state, market)`, `idx_dashboard_jobs_market_state ON dashboard_jobs(market_state)`, and optional `idx_quotes_created_by_user`, `idx_outreach_queue_created_by_user`.

No new ownership columns are needed on `quote_line_items`, `quote_events`, `outreach_events`, `contacts`, `artifacts`, or `website_audits` if all access joins or first guards through the parent prospect.

## SECTION 5 — Current query/filtering hotspots

Must be territory-scoped:

- Market helpers: `load_configured_markets()`, `build_market_options()`, `market_filter_context()`, `market_where_clause()`, `append_market_filter()`, `load_market_manager_rows()`.
- Prospect direct access: `load_prospect()`, `_load_prospect_from_connection()`.
- Prospect lists/counts: `load_stage_counts()`, `load_pipeline_counts()`, `load_run_counts()`, `load_run_recommended_action()`, `load_review_queue()`, `load_group_counts()`, `load_distinct_values()`, `load_market_summary_counts()`, `load_market_summary_rows()`, `load_stage_counts_by_market()`, `load_leads()`, `load_trash_rows()`, `load_crm_prospects()`, `load_crm_columns()`, `load_crm_stage_prospects()`.
- Child records: `load_artifacts()`, `load_audits()`, `load_contacts()`, `load_stage_history()`, `load_email_draft_artifact()`.
- Outbound: `outbound_where_clause()`, `load_outbound_prospects()`, `load_outbound_not_approved_group()`, `load_artifact_map_for_prospects()`, `load_contact_map_for_prospects()`, `load_last_email_events()`, `load_already_sent_prospect_ids()`, `load_active_queue_rows()`, `create_step_1_send_queue()`.
- Send queue: `load_send_queue_rows()`, `prepare_send_queue_row()`, `send_dashboard_batch()`, `send_queue_screenshot_attachments()`, `mark_queue_sent()`, `mark_queue_failed()`, `mark_queue_skipped()`.
- Quotes: `quote_service.list_quotes()`, `quote_service.list_quotes_for_prospect()`, `quote_service.get_quote()`, `quote_service.create_quote_for_prospect()`, quote lifecycle routes and `insert_quote_lifecycle_outreach_event()`.
- Jobs: `dashboard_jobs.list_jobs()`, `dashboard_jobs.get_job()`, `dashboard_jobs.create_job()`, `dashboard_jobs.create_full_pipeline_job()`.
- CLI selectors when launched from dashboard: `places_pull._load_market_and_niche()`, `eligibility._select_prospects()`, `audit_site._select_audit_prospects()`, `score_leads._select_audited_prospects()`, `generate_artifacts._select_candidates()`, `contact_readiness.select_prospects()`, `public_packets._select_candidates()`, `outreach_drafts._select_candidates()`, `send_outreach._select_prospects()`.

Direct-ID routes must guard by prospect/quote/job before any read/write response. Filtering list pages is not enough.

## SECTION 6 — Current job runner behavior

- Jobs start from `/jobs/start` through `start_job_from_form()` and from `/run/full-pipeline` through `start_full_pipeline_from_form()`.
- `start_job_from_form()` validates job type against `dashboard_jobs.ALLOWED_JOBS`, builds metadata with `db_path`, then calls `dashboard_jobs.create_job()` and `dashboard_jobs.run_job_async()`.
- `start_full_pipeline_from_form()` validates configured market/niches, creates a full pipeline job, and starts it async.
- `dashboard_jobs.create_job()` stores `dashboard_jobs` rows with `job_key`, `job_type`, `market`, `niche`, `limit_count`, `dry_run`, `command_json`, `metadata_json`, and `log_path`.
- `dashboard_jobs.run_job_async()` starts a background thread; `_run_job_worker()` runs whitelisted subprocess commands with `shell=False`.
- Arbitrary shell input is not possible, but arbitrary `market` strings are accepted for many single jobs. Full pipeline requires `market in configured_market_keys()`.
- `/run` permits audit with no market if `allow_all_markets=1`; non-admin users must not be allowed to use all-market jobs.
- User/session context exists only in the Flask route. `dashboard_jobs` has no actor fields and subprocesses receive no actor info.
- Enforce ownership before `dashboard_jobs.create_job()` and before `create_full_pipeline_job()`.
- Subprocesses do not need actor info if dashboard routes force a single authorized market; record actor info in `dashboard_jobs.metadata_json` and new columns. Add CLI-level state checks only if non-admin users can run CLI commands directly.

## SECTION 7 — Required auth design

Implement:

- `src/auth.py`
  - `load_users()`
  - `verify_password(username, password)`
  - `current_user()`
  - `login_user(user)`
  - `logout_user()`
  - `login_required`
  - `admin_required`
- `config/users.yaml`
  - No passwords or hashes.
  - Map users to role, state list, and password hash env var:
    - `ADMIN`: role `admin`, states `["*"]`, hash env `AUTH_ADMIN_PASSWORD_HASH`
    - `QWHITE`: role `user`, states `["OH", "KY", "TX", "FL", "MI", "NC"]`, hash env `AUTH_QWHITE_PASSWORD_HASH`
    - `JROSS`: role `user`, states `["IN", "PA", "TN", "AR", "OK", "CO"]`, hash env `AUTH_JROSS_PASSWORD_HASH`
    - `AG`: role `user`, states `["MO", "IL", "GA", "AL", "SC"]`, hash env `AUTH_AG_PASSWORD_HASH`
- `scripts/generate_password_hash.py`
  - Use Werkzeug `generate_password_hash`.
  - Print only the hash; do not write `.env`.
- Update dashboard login routes to use `APP_SECRET_KEY`, `AUTH_*_PASSWORD_HASH`, and configured users.
- Remove plaintext `DASHBOARD_PASSWORD` support.
- Store in session: `user_id`, `role`, `states`, `authenticated_at`.
- Set `session.permanent = True` and `PERMANENT_SESSION_LIFETIME` to a practical window such as 12 hours.
- Add `admin_required` to `/admin/*` and `/send/test`.
- Add `territory_required` or explicit guards to all market/prospect/quote/job routes.

## SECTION 8 — Required territory design

Implement `src/territories.py`:

- `STATE_ALIASES`: canonical map for two-letter states and common names.
- `normalize_state(value) -> str | None`: uppercase two-letter output or `None`.
- `configured_market_states() -> dict[str, str]`: from `config/markets.yaml`.
- `get_market_state(market_key) -> str | None`: config first; fallback from DB only for legacy/unconfigured records if needed.
- `get_prospect_state(prospect) -> str | None`: `canonical_state`, then market state, then `state`, then `state_guess`.
- `user_can_access_state(user, state) -> bool`: admin bypass; non-admin requires normalized state in session/config.
- `user_can_access_market(user, market_key) -> bool`: resolves `get_market_state()`.
- `ensure_market_access_or_403(user, market_key)`: abort/return 403 with `"you do not own this market"` for unauthorized non-admin market access/add.
- `ensure_prospect_access_or_404(user, prospect)`: use 404 for direct-ID data leakage, or 403 only where product requires visible denial.
- `ensure_quote_access_or_404(user, quote)`: resolve quote prospect.
- SQL helpers:
  - `territory_state_clause(user, table_alias="prospects") -> (sql, params)`
  - `append_territory_filter(clauses, params, user, table_alias="prospects")`
  - `market_options_for_user(user, selected_market="")`

Canonical state should be the partition key. Market key is not enough because unconfigured/legacy records and direct-ID routes exist.

## SECTION 9 — Single SQLite partition strategy

- Keep one SQLite database.
- Add `prospects.canonical_state` and backfill it from market config and existing state columns.
- Keep child records linked to prospects; filter them with joins or parent guards.
- Admin bypasses every territory SQL clause and direct-ID guard.
- Non-admin list routes always append `canonical_state IN (...)`.
- Non-admin direct-ID routes load the parent prospect/quote/job and guard before rendering or mutating.
- Non-admin market dropdowns show only configured markets whose `state` is owned.
- Non-admin `UNKNOWN_MARKET_VALUE` should be hidden or show only unconfigured records whose `canonical_state` is owned.
- Non-admin market add must normalize the submitted state and reject unauthorized states with exactly `"you do not own this market"`.
- Non-admin jobs must require one authorized configured market. No all-market audit/reconcile/full-pipeline.
- Send queue and outbound queue must filter by joined `prospects.canonical_state`; `send_dashboard_batch()` must not send a queued row outside the current user's states.
- Public packet token routes can stay public; packet generation and dashboard links must be territory-guarded.

## SECTION 10 — Railway/env requirements

Required env vars:

- `APP_SECRET_KEY`
- `AUTH_ADMIN_PASSWORD_HASH`
- `AUTH_QWHITE_PASSWORD_HASH`
- `AUTH_JROSS_PASSWORD_HASH`
- `AUTH_AG_PASSWORD_HASH`

Current env/docs use `FLASK_SECRET_KEY`, `DASHBOARD_USERNAME`, and `DASHBOARD_PASSWORD_HASH`; those should be replaced or deprecated.

Railway SQLite persistence: this app uses SQLite at `DATABASE_PATH=data/leads.db` by default. Railway needs a persistent volume mounted to the DB folder or symlinked through `USE_STORAGE_SYMLINKS=1` and `STORAGE_ROOT=/app/storage`; otherwise DB, runs, artifacts, screenshots, and public packets can be lost on redeploy.

## SECTION 11 — Phased implementation plan

1. Auth core: add `src/auth.py`, `config/users.yaml`, hash generator, `APP_SECRET_KEY`, session user model.
2. Territory core: add `src/territories.py` and unit-level helper coverage for normalization, market ownership, and SQL clauses.
3. DB columns/backfill: add `prospects.canonical_state`, job actor/state columns, indexes, and deterministic backfill in schema init/migration code.
4. Dashboard login integration: replace current single-user login with configured users; add template context for current user/role/states.
5. Market scoping: filter market dropdowns and market manager; guard `/markets/add` with exact unauthorized message.
6. Route/query scoping: apply territory SQL filters and direct-ID guards to prospects, CRM, review, outbound, send, quotes, artifacts, contacts, audits, files.
7. Job scoping: enforce before job creation; disallow non-admin all-market jobs; store actor/state on jobs; filter job list/detail/status.
8. QA: verify login, admin all-data, each non-admin state set, direct URL denial, market add denial, job denial, send queue filtering, and one-DB persistence.

## SECTION 12 — Do-not-touch list

- Phase 1 auth core: do not touch `src/places_pull.py`, `src/audit_site.py`, `src/score_leads.py`, `src/send_outreach.py`, `config/markets.yaml`, or SQLite data files.
- Phase 2 territory core: do not touch templates or job runners except imports in a later integration phase.
- Phase 3 DB columns/backfill: do not touch dashboard templates, email/SMPP settings, Google Places code, or production SQLite manually.
- Phase 4 login integration: do not touch market config, pipeline modules, quote pricing catalog, or send queue logic.
- Phase 5 market scoping: do not touch Places API request code, SMTP code, quote pricing, or public packet rendering.
- Phase 6 route/query scoping: do not touch Docker/Railway files unless only env docs need updates.
- Phase 7 job scoping: do not touch the internal scoring/audit algorithms; only validate launch inputs and record/filter job metadata.
- Phase 8 QA: do not touch application code, config, SQLite, or external APIs; use local seeded/test data only.

## SECTION 13 — Acceptance criteria

- Unauthenticated users are redirected to `/login` for dashboard routes.
- `/health`, static assets, and tokenized public packet routes remain public.
- Admin sees and can act on all markets and all data.
- QWHITE sees/acts only on OH, KY, TX, FL, MI, NC.
- JROSS sees/acts only on IN, PA, TN, AR, OK, CO.
- AG sees/acts only on MO, IL, GA, AL, SC.
- Non-admin users cannot direct-open unauthorized case IDs, quote IDs, job IDs, artifacts, contacts, audits, or send queue records.
- Non-admin users cannot add an unauthorized market and see exactly `"you do not own this market"`.
- Non-admin users cannot run jobs for unauthorized markets or any all-market job.
- Send queue and outbound readiness only include owned-state prospects for non-admin users.
- Data remains in one SQLite database.
- Password hashes are loaded only from env vars.
- Plaintext passwords are not committed or supported.
