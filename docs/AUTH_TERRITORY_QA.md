# Auth and Territory QA

Report-only audit. No SQLite mutation, no external APIs, no Places jobs, no audits, no email, and no deployment actions were run. This report is based on static code/config inspection plus read-only config parsing.

## SECTION 1 - Auth inventory

### Login routes

- `GET/POST /login` is defined in `src/dashboard_app.py:2114`.
- Login uses `auth_service.verify_password()` from `src/auth.py:166`, then `flask_login_user(user)` and stores only session identity/context fields in `src/dashboard_app.py:2124-2134`.
- Failed login displays the generic message `Invalid username or password.` from `src/dashboard_app.py:2136`; it does not reveal missing user vs bad password vs missing hash.

### Logout route

- `GET/POST /logout` is defined in `src/dashboard_app.py:2143`.
- It is protected with `@login_required`, calls `flask_logout_user()`, clears the Flask session, and redirects to `/login`.

### Login protection coverage

- `src/dashboard_app.py:2106` has a `before_request` gate that redirects unauthenticated users to `/login`.
- Public endpoints are limited by `PUBLIC_AUTH_ENDPOINTS` in `src/dashboard_app.py:98`: `login`, `health`, `static`, `public_packet_page`, and `public_packet_asset`.
- Public packet token routes `/p/<token>/...` and `/assets/<path>` are intentionally public in `src/dashboard_app.py:2150-2164`.
- Admin-only routes use `auth_service.admin_required`, including `/admin/database`, `/admin/media`, `/admin/users`, `/admin/territories`, and `/send/test`.

### Session secret handling

- `src/dashboard_app.py:1840` loads `APP_SECRET_KEY`.
- `src/dashboard_app.py:1845` raises in production/Railway-like environments when `APP_SECRET_KEY` is missing.
- `src/dashboard_app.py:1847-1851` generates a temporary local development secret and logs a warning when not in production.
- `PERMANENT_SESSION_LIFETIME` is set to 12 hours in `src/dashboard_app.py:2045`.

### Password hash env handling

- `config/users.yaml` contains password hash env var names only, not hashes.
- `src/auth.py:125-147` hydrates each configured user from the named env var.
- `src/auth.py:175-181` rejects login when the env hash is absent and logs the missing env var on the server side.
- `src/auth.py:184-190` uses Werkzeug `check_password_hash()` and rejects invalid hash formats.

### Missing env behavior

- If a user's `AUTH_*_PASSWORD_HASH` env var is absent, that user cannot log in.
- The login page still shows only the generic invalid login message.
- Admin diagnostics show only yes/no hash presence via `templates/dashboard/admin_users.html:44-53`; no hash values are rendered.

## SECTION 2 - User config inventory

`config/users.yaml` matches the requested users:

| User | Role | States |
|---|---|---|
| `ADMIN` | `admin` | `*` / all states |
| `QWHITE` | `user` | `OH`, `KY`, `TX`, `FL`, `MI`, `NC` |
| `JROSS` | `user` | `IN`, `PA`, `TN`, `AR`, `OK`, `CO` |
| `AG` | `user` | `MO`, `IL`, `GA`, `AL`, `SC` |

Verification notes:

- `src/auth.py:73-116` validates roles, display names, allowed states, and hash env names.
- Read-only parsing with `territories.validate_exclusive_territories(config/users.yaml)` returned `[]`; no duplicate or invalid state ownership conflicts were found.
- No plaintext dashboard passwords or committed auth hashes were found in `config/users.yaml`, `.env.example`, or templates.
- `.env.example:7-11` contains empty placeholders for `APP_SECRET_KEY` and the four `AUTH_*_PASSWORD_HASH` variables.
- `scripts/generate_password_hashes.py` generates hashes on demand; no generated hashes are committed.

## SECTION 3 - Market/territory audit

### Configured markets

Read-only parsing of `config/markets.yaml` resolved all configured markets to canonical states:

| Market | State |
|---|---|
| `mckinney_tx` | `TX` |
| `cincinnati` | `OH` |
| `dayton` | `OH` |
| `columbus_oh` | `OH` |
| `akron_oh` | `OH` |
| `cleveland_oh` | `OH` |

### Market visibility and unknown-state behavior

- `src/dashboard_app.py:452-474` normalizes market state and sets `state_warning` when unrecognized.
- `src/dashboard_app.py:543-551` lets admin see all configured markets and hides missing/unknown-state markets from non-admin users.
- `templates/dashboard/markets.html:66-69` displays `Missing/unknown state` warnings for admin-visible unknown-state markets.

### Add/edit/delete

- Market add is implemented at `POST /markets/add` in `src/dashboard_app.py:3016`.
- `src/dashboard_app.py:1420-1458` normalizes state with `territories.normalize_state()`, rejects unauthorized states through `current_user_can_access_state()`, and writes canonical two-letter state.
- The exact denial text comes from `src/territories.py:390-391`: `you do not own this market`.
- Market edit/delete routes were not found; existing UI says to edit `config/markets.yaml` directly.

## SECTION 4 - Data model audit

Schema/migration code confirms the requested territory columns:

| Table | Columns | Evidence |
|---|---|---|
| `prospects` | `market_state`, `owner_username` | `src/db.py:19-35`, migrations at `src/db.py:372-376` |
| `dashboard_jobs` | `requested_by_user`, `market_state` | `src/db.py:204-214`, migrations at `src/db.py:377-380` |
| `outreach_queue` | `owner_username`, `market_state` | `src/db.py:157-167`, dashboard schema at `src/dashboard_app.py:1502-1512` |
| `quotes` | `owner_username`, `market_state` | `src/db.py:233-238`, migrations at `src/db.py:385-388` |

Other notes:

- `src/db.py:521-552` adds indexes for the territory columns.
- `src/db.py:559-638` writes `market_state` and `owner_username` during prospect upsert.
- `src/quotes.py:796-807` stores quote `owner_username` and `market_state` at quote creation.
- `src/dashboard_app.py:4050-4061` can sync quote owner/state fields from the guarded prospect.

### Backfill/reconcile command

- `src/reconcile_territories.py:402-403` supports `--dry-run` and `--apply`.
- Dry run opens SQLite read-only through `_connect_readonly()` at `src/reconcile_territories.py:24-30` and `src/reconcile_territories.py:344`.
- Apply uses `db.init_db()` and updates only the territory/owner fields for `prospects`, `dashboard_jobs`, `outreach_queue`, and `quotes`.
- No destructive delete/truncate behavior was found in `src/reconcile_territories.py`.

## SECTION 5 - Route-scope audit

All non-public dashboard routes are authentication-protected by the `before_request` gate in `src/dashboard_app.py:2106-2112`.

| Route/surface | Scope | Direct-ID protected? | POST mutation protected? | Evidence |
|---|---|---:|---:|---|
| `/` | authenticated, territory scoped counts | N/A | N/A | `load_stage_counts()` and related count helpers use `append_visible_market_scope()`, e.g. `src/dashboard_app.py:3332`, `3369`, `3401` |
| `/markets` | authenticated; admin all, non-admin visible states only | N/A | yes for add | `load_market_manager_rows()` uses `visible_configured_markets()` at `src/dashboard_app.py:3779-3803`; add guard at `src/dashboard_app.py:1420-1458` |
| `/run` | authenticated, selected market normalized to user | N/A | full-pipeline guarded | `src/dashboard_app.py:2867-2898`; full-pipeline route `src/dashboard_app.py:2904`; guard `src/dashboard_app.py:7762-7814` |
| `/jobs` | authenticated; list/detail/status scoped by job state | yes | start guarded | list uses `list_dashboard_jobs_for_current_user()` at `src/dashboard_app.py:2912-2938`; detail/status guard at `src/dashboard_app.py:2940-2976`; start guard at `src/dashboard_app.py:7697-7758` |
| `/leads` | authenticated, territory scoped | N/A | N/A | `load_leads()` uses `append_visible_market_scope()` at `src/dashboard_app.py:3810` |
| `/review` | authenticated, territory scoped | delete route guarded | yes | list at `src/dashboard_app.py:2328`; delete route calls `require_prospect_access()` at `src/dashboard_app.py:2340-2342` |
| `/case/<id>` | authenticated, territory direct-object guard | yes | all case POSTs call guard | GET guard at `src/dashboard_app.py:3036-3038`; review/stage/contact/visual/draft routes call `require_prospect_access()` at `src/dashboard_app.py:3101`, `3147`, `3168`, `3190`, `3210`, `3239`, `3268` |
| `/crm` and `/crm/stage/<stage>` | authenticated, territory scoped | N/A | stage changes guarded under case route | `/crm` at `src/dashboard_app.py:2416`; stage route at `src/dashboard_app.py:2803`; list helpers apply visible market scope |
| `/outbound` | authenticated, territory scoped | queue creation uses guarded prospects | yes | outbound where clause uses `append_visible_market_scope()` at `src/dashboard_app.py:5068-5076`; queue creation calls `require_prospect_access()` at `src/dashboard_app.py:5593` |
| `/send` | authenticated, queue rows scoped by joined prospect | queued rows only | send batch scoped | queue query joins prospects and applies `apply_prospect_scope()` at `src/dashboard_app.py:5962-6014`; send batch uses those rows at `src/dashboard_app.py:6264-6332`; `/send/test` admin-only at `src/dashboard_app.py:2477-2479` |
| `/quotes` | authenticated, territory scoped by joined prospect | quote direct routes guarded | yes | quote list applies `apply_prospect_scope()` at `src/dashboard_app.py:4013-4041`; create uses `require_prospect_access()` at `src/dashboard_app.py:2534` and `2557`; detail/edit/export/status/revision/delete use `require_quote_access()` at `src/dashboard_app.py:2593-2787` |

Direct object helpers:

- `require_prospect_access()` in `src/dashboard_app.py:765-772` returns 403 with `you do not own this market` for unauthorized prospects.
- `require_quote_access()` in `src/dashboard_app.py:774-779` resolves the quote's prospect and reuses prospect access.
- `require_queue_access()` in `src/dashboard_app.py:782-807` resolves the queue row's prospect and reuses prospect access.
- Artifact/file access for non-admin users is tied back to artifact prospect ids in `src/dashboard_app.py:8180-8216`.

## SECTION 6 - Job-scope audit

### Dashboard job creation

- Non-admin users cannot create global unrestricted jobs: `ensure_current_user_can_create_market_job()` rejects missing market for non-admin in `src/dashboard_app.py:7647-7656`.
- Non-admin users cannot create jobs for unauthorized markets: the same guard checks `current_user_can_access_market()`.
- `reconcile_statuses` is hidden/rejected for non-admin users at `src/dashboard_app.py:7701-7706` and `src/dashboard_app.py:2929-2934`.
- Full pipeline requires a configured selected market and then runs `ensure_current_user_can_create_market_job()` at `src/dashboard_app.py:7762-7770`.
- The old `/pipeline/run` route is retired and only redirects with an error in `src/dashboard_app.py:2998-3004`.
- `send_outreach` is not in `dashboard_jobs.ALLOWED_JOBS`; `/run` and `/jobs/start` do not expose it.

### Job metadata and actor env

- `dashboard_jobs` stores `market_state` and `requested_by_user` in `src/dashboard_jobs.py:200-218` and `src/dashboard_jobs.py:287-304`.
- `src/dashboard_jobs.py:482-510` builds:
  - `APP_ACTOR_USERNAME`
  - `APP_ACTOR_ROLE`
  - `APP_ACTOR_ALLOWED_STATES`
  - `APP_ACTOR_MARKET`
  - `APP_ACTOR_MARKET_STATE`
- `src/dashboard_jobs.py:747` and `src/dashboard_jobs.py:800` pass the env into subprocesses.
- Full-pipeline steps inherit the same actor env via `_run_full_pipeline_worker()`.

### CLI actor-scope coverage

Actor-aware modules:

- `src/places_pull.py:462-473` validates market access, requires `--market`, and writes `market_state`/`owner_username`.
- `src/eligibility.py:279` scopes prospect selection and `src/eligibility.py:421` validates market/global scope.
- `src/audit_site.py:523`, `545`, and `667` scope and validate actor access.
- `src/score_leads.py:99` and `754` scope and validate actor access.
- `src/generate_artifacts.py:152`, `169`, and `525` scope and validate actor access.
- `src/reconcile_statuses.py:153` and `214` scope and validate actor access.

Not actor-aware yet:

- `src/contact_readiness.py:363-405` selects prospects by id/market/global without `actor_context`.
- `src/public_packets.py:80-115` selects prospects without `actor_context`.
- `src/outreach_drafts.py:261-315` selects prospects without `actor_context`.
- `src/send_outreach.py:215-263` selects prospects without `actor_context` and can send when invoked with `--send`.

Dashboard validation is strong for the dashboard-exposed job set. Defense-in-depth is incomplete for the standalone outbound/support CLI modules above.

## SECTION 7 - Outbound/quote safety

### Outbound

- `/outbound` list queries are territory-scoped through `outbound_where_clause()` in `src/dashboard_app.py:5068-5076`.
- `/outbound/queue` uses the scoped readiness set and calls `require_prospect_access()` before inserting each queue row at `src/dashboard_app.py:5593`.
- Queue rows store `owner_username` and `market_state` at insert time in `src/dashboard_app.py:5600-5624`.
- `/send` queue rows are loaded with a `JOIN prospects` and `apply_prospect_scope()` in `src/dashboard_app.py:5947-6014`.
- `/send/batch` sends only rows returned by `load_send_queue_rows()` and preserves normal gates:
  - infra report ready check
  - send config/compliance check
  - daily cap
  - queue status
  - human review approval
  - allowed prospect status/next action
  - suppression list
  - draft/public packet presence
  - duplicate send check
- `/send/test` is admin-only and does not touch prospect records.

### Quotes

- Quote list is scoped by joined prospect in `src/dashboard_app.py:4013-4041`.
- Quote create requires an accessible prospect in `src/dashboard_app.py:2534` and `2557`.
- Quote detail/edit/export/status/revision/delete all use `require_quote_access()` before acting.
- Admin can see all because `prospect_scope_clause()` returns `1 = 1` for admin users.
- `src/quotes.py` itself is a service layer and is not internally territory-scoped; dashboard callers are guarded, but future callers must keep using `require_quote_access()` or an equivalent guard.

## SECTION 8 - Railway readiness

Positive findings:

- `.env.example:7` includes `APP_SECRET_KEY`.
- `.env.example:8-11` includes all four `AUTH_*_PASSWORD_HASH` variables.
- `docs/AUTH_ENV_SETUP.md:20-24` documents the new auth env variables.
- `.env.example:15-21`, `docs/RAILWAY_DEPLOYMENT.md:11`, and `scripts/docker-entrypoint.sh:4-22` document/support a Railway persistent volume with `USE_STORAGE_SYMLINKS=1` and `STORAGE_ROOT=/app/storage`.
- `railway.json` exists and points health checks to `/health`.
- `Dockerfile` runs Gunicorn against `src.dashboard_app:create_app()`.

Blocking/stale finding:

- `docs/RAILWAY_DEPLOYMENT.md:57-60` still instructs Railway users to set old auth variables:
  - `DASHBOARD_AUTH_ENABLED`
  - `DASHBOARD_USERNAME`
  - `DASHBOARD_PASSWORD_HASH`
  - `FLASK_SECRET_KEY`
- Current code uses `APP_SECRET_KEY` and `AUTH_*_PASSWORD_HASH`. Following the stale Railway doc literally can produce a failed production boot (`APP_SECRET_KEY` missing) or configured users with no usable login hashes.

## SECTION 9 - P0/P1/P2 issues

### P0 - must fix before multiple users use Railway app

1. `docs/RAILWAY_DEPLOYMENT.md:57-60` is stale for the new auth system. Replace the old `DASHBOARD_*` and `FLASK_SECRET_KEY` instructions with `APP_SECRET_KEY` and all `AUTH_*_PASSWORD_HASH` variables. This is a deployment blocker because current production code requires `APP_SECRET_KEY` and configured user hash env vars.

No P0 dashboard-route data-partitioning bug was found in the inspected routes.

### P1 - fix before real outbound by non-admin users

1. Standalone outbound/support CLI modules do not honor `APP_ACTOR_*`:
   - `src/contact_readiness.py:363-405`
   - `src/public_packets.py:80-115`
   - `src/outreach_drafts.py:261-315`
   - `src/send_outreach.py:215-263`
   Add `actor_context` validation/scoping before these are run on behalf of non-admin users or exposed through dashboard jobs.
2. `src/send_outreach.py` can send real email when invoked with `--send` and does not enforce actor territory. It is not exposed through `/run`, but it should require admin/local mode or actor scope before any non-admin outbound workflow uses it.
3. `src/quotes.py` service functions are not internally territory-scoped. Current dashboard routes guard them correctly, but future API/CLI callers should not call quote mutations without `require_quote_access()` or equivalent.

### P2 - cleanup

1. `src/dashboard_app.py:7679-7722` still contains legacy `run_dashboard_pipeline_job()` code even though `/pipeline/run` is retired. It is not currently routed to execution, but removing or clearly marking it as dead code would reduce future mistakes.
2. `src/dashboard_jobs.py:list_jobs()` and `get_job()` are unscoped service helpers and rely on dashboard caller filtering via `list_dashboard_jobs_for_current_user()` / `current_user_can_access_job()`. This is acceptable now, but future callers should use scoped wrappers.
3. `docs/AUTH_TERRITORY_IMPLEMENTATION_MAP.md` still contains historical "current state" notes from before implementation. Keep it as a planning artifact or update it to avoid confusing future implementation work.

## SECTION 10 - Final verdict

1. Can admin see all data?
   Yes for dashboard routes. Admin bypasses prospect/job scope and sees all configured markets, jobs, leads, quotes, outbound/send rows, and diagnostics.

2. Can each non-admin see only assigned states?
   Yes for the inspected dashboard views and direct-object routes. Caveat: several standalone CLI modules still lack actor-environment scoping.

3. Can non-admin add unauthorized market?
   No. `add_market_from_form()` rejects unauthorized state with exactly `you do not own this market`.

4. Can non-admin direct-open unauthorized case?
   No. `/case/<id>` calls `require_prospect_access()` and returns a territory denial for unauthorized prospects.

5. Can non-admin run unauthorized Places/audit job?
   No through the dashboard. Dashboard job creation validates market ownership, and `places_pull`/`audit_site` also validate `APP_ACTOR_*` as defense-in-depth.

6. Can non-admin queue/send unauthorized lead?
   No through the dashboard. Outbound readiness, queue creation, send queue display, and send batch are prospect/territory scoped. Caveat: standalone `src/send_outreach.py --send` is not actor-scoped and should not be used as a non-admin execution path yet.

7. Is single SQLite partitioning implemented safely enough for Phase 2?
   Yes for dashboard-mediated multi-user use, assuming the reconcile/backfill is run as needed and future callers preserve the direct-object guards. The single-DB model is supported with state/owner columns and joined child-record filtering.

8. Is it ready for Railway deployment?
   Not quite. The code and `.env.example` are mostly ready, but `docs/RAILWAY_DEPLOYMENT.md` still documents obsolete auth env vars. Fix that P0 documentation/deployment mismatch before relying on Railway setup for multiple users.
