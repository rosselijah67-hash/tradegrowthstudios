# Dashboard Run Tab QA

QA date: 2026-05-11  
Workspace: `C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads`  
Database inspected read-only: `data/leads.db`

Commands/data inspected:
- Source/templates/CSS under `src/`, `templates/dashboard/`, `static/dashboard.css`.
- Read-only SQLite queries against `data/leads.db`.
- Safe GET checks returned 200 for `/run`, `/jobs`, `/markets`, `/review`, `/leads`, `/crm`, `/case/1`.
- No non-dry-run external jobs were started. No email/SMTP was run.

## SECTION 1 — Implemented route inventory

| Route | Purpose | Reads database | Writes database | Can trigger external calls | Can send email |
|---|---:|---:|---:|---:|---:|
| `/run` | Browser control page for market/niche selection, counts, individual jobs, full pipeline, recent jobs. Implemented in `src/dashboard_app.py` `run_controls`. | Yes | No on GET | No on GET; POST forms can start external `places_pull`, `audit`, or `full_pipeline` only after explicit submit/confirmation rules. | No |
| `/jobs` | Lower-level job start/history page. Implemented in `src/dashboard_app.py` `jobs`. | Yes | No on GET; `/jobs/start` writes `dashboard_jobs`. | No on GET; `/jobs/start` can start whitelisted external jobs. | No |
| `/markets` | Market manager from `config/markets.yaml`. Implemented in `src/dashboard_app.py` `markets`. | Yes | No on GET; `/markets/add` writes `config/markets.yaml` and backup. | No | No |
| `/review` | Manual review queue for `audit_data_status='READY'`, `human_review_status='PENDING'`, `next_action='HUMAN_REVIEW'`. | Yes | No on GET | No | No |
| `/leads` | Lead table with stage/market/niche/search filters. | Yes | No | No | No |
| `/crm` | CRM stage columns filtered by market. | Yes | No | No | No |
| `/case/<id>` | Case detail, screenshots, artifacts, visual critique, CRM/contact/review controls. | Yes | No on GET; related POST routes write review/stage/contact/visual-review and can generate local outreach drafts. | No website/API calls on GET. The case page has a POST for `outreach-drafts`, but this generates drafts locally via `src.outreach_drafts`; it is not exposed from `/run`. | No SMTP/send route found |

Note: `/pipeline` is retired for execution. `GET /pipeline` redirects to `/run`; `POST /pipeline/run` redirects to `/run` with an error message and does not start a subprocess.

## SECTION 2 — Market manager QA

Verified:
- `config/markets.yaml` loads through `load_markets_document()` and `load_configured_markets()` in `src/dashboard_app.py`.
- Current configured markets include `mckinney_tx`, `cincinnati`, `dayton`, `columbus_oh`, and `akron_oh`.
- `/markets/add` validates:
  - `label` required.
  - `state` must match uppercase two-letter state via `[A-Z]{2}`.
  - blank `market_key` becomes deterministic lower snake slug via `generate_market_key(label, state)`.
  - `market_key` must match `^[a-z0-9_]+$`.
  - duplicate market keys reject before write.
  - included cities required and parsed from newline or comma input.
- Backup behavior exists in `write_markets_document()`: before writing, it copies to `config/markets.yaml.bak.<timestamp>`.
- Existing backups found:
  - `config/markets.yaml.bak.20260511001317`
  - `config/markets.yaml.bak.20260511005906`
- Market filter availability:
  - Overview uses `market_filter_context()` and `load_market_summary_rows()`.
  - Review uses `load_review_queue(selected_market)`.
  - Leads uses `filters["market"]`, `build_market_options()`, and `append_market_filter()`.
  - CRM uses `load_crm_columns(selected_market)`.
  - Run uses the same configured market options and links to filtered Leads/Review.

Issue:
- P2: `write_markets_document()` backup timestamp has second precision. Two market additions within the same second could overwrite the backup file name. Low risk for manual local use.

## SECTION 3 — Run tab QA

Verified individual actions in `templates/dashboard/run.html` and `src/dashboard_app.py`:
- `places_pull`
  - Requires selected market through hidden `market`.
  - Requires one niche dropdown.
  - Limit max 100 server-side via `PLACES_JOB_LIMIT`.
  - Dry-run checked by default.
  - Non-dry-run requires `confirm_run`.
  - Warning explicitly says Places dry-runs still use Google API quota.
- `eligibility`
  - Market optional but populated from selected market.
  - Niche optional.
  - Local-only; dry-run available but unchecked by default.
- `audit`
  - Limit max 50 server-side via `AUDIT_JOB_LIMIT`.
  - Dry-run checked by default.
  - Non-dry-run requires `confirm_run`.
  - Requires market unless `allow_all_markets` is checked.
  - Warning says it fetches live websites and may use screenshots/PageSpeed.
- `score`
  - Local-only, optional market/niche/limit, dry-run available.
- `artifacts`
  - Local file generation, optional market/niche/limit, dry-run available.
- `reconcile_statuses`
  - Dry-run checked by default.
  - Non-dry-run maps to `--apply` and requires confirmation.

Verified full pipeline:
- `start_full_pipeline_from_form()` requires configured market, selected niches, positive limits, `places_limit <= 100`, `audit_limit <= 50`.
- Non-dry-run full pipeline requires confirmation.
- `dashboard_jobs.create_full_pipeline_job()` stores fixed step commands in `metadata_json`.
- Step order: per niche `places_pull -> eligibility`, then market-level `audit -> score -> artifacts -> reconcile_statuses`.
- `_run_full_pipeline_worker()` logs each step start, command, stdout/stderr, exit code, and stops on first nonzero exit.

Security/safety:
- `send_outreach` is not in `dashboard_jobs.ALLOWED_JOBS`.
- `/jobs` excludes `full_pipeline` from generic job cards; full pipeline can only be started through `/run/full-pipeline`.
- `build_whitelisted_command()` builds commands from a static map.
- `subprocess.Popen(..., shell=False)` is used for job execution in `src/dashboard_jobs.py`.
- No arbitrary shell command input found in `/run` or `/jobs`.

## SECTION 4 — Job runner QA

Verified database:
- `dashboard_jobs` table exists in `data/leads.db`.
- Columns present: `id`, `job_key`, `job_type`, `status`, `market`, `niche`, `limit_count`, `dry_run`, `command_json`, `metadata_json`, `log_path`, `started_at`, `finished_at`, `created_at`, `updated_at`.
- Current job statuses in DB: `failed: 4`, `succeeded: 2`.

Verified active job restriction:
- `create_job()` and `create_full_pipeline_job()` both call `has_active_job()` before insert.
- `has_active_job()` checks `status IN ('queued', 'running')`.
- `_run_job_worker()` also uses process-local `RUNNER_LOCK`.

Verified logs:
- Logs are under `runs/dashboard_jobs/`.
- Recent logs found, including `places_pull_20260511055258_317036be.log`.
- Job detail renders `command_json` as JSON for paths with spaces.

Verified command transparency:
- `command_json` stores actual argument arrays.
- Example current successful job command:
  - job: `places_pull_20260511055258_317036be`
  - command includes `.venv\Scripts\python.exe -m src.places_pull --db-path ... --limit 20 --market columbus_oh --niche roofing`

Verified failure handling:
- Nonzero subprocess exit marks job `failed` with `metadata_json.error_summary`.
- Full pipeline stops at the failed step and marks parent job failed.
- If another worker is already running, runner marks the job failed and logs a block message.

Verified stale handling:
- `mark_stale_jobs()` marks old `running` jobs stale by `started_at`.
- It also marks old `queued` jobs stale by `created_at`.

Issue:
- P2: Old helper functions for the retired synchronous pipeline runner still exist in `src/dashboard_app.py` (`run_dashboard_pipeline_job`, `build_pipeline_command`, etc.). The route no longer executes them, so this is cleanup only.

## SECTION 5 — Audit selection QA

Verified in `src/audit_site.py` `_select_audit_prospects()`:
- Batch audit requires `qualification_status = 'QUALIFIED'`.
- Excludes statuses `INELIGIBLE`, `DISCARDED`, `REJECTED_REVIEW`, `CLOSED_LOST`.
- Excludes `next_action IN ('DISCARD', 'DISQUALIFIED')`.
- Requires status in `ELIGIBLE_FOR_AUDIT`, `AUDIT_READY`, `PENDING_REVIEW` OR next_action in `RUN_AUDIT`, `NEEDS_SITE_AUDIT`, `HUMAN_REVIEW`.
- Requires nonblank `website_url`.
- Skips `audit_data_status='READY'` unless `--force`.
- Dashboard command builder does not add `--force`.

Read-only DB findings:
- Total prospects: `202`.
- Markets: `mckinney_tx: 182`, `columbus_oh: 20`.
- Current status groups:
  - `DISCOVERED / NEW / PENDING / next_action NULL`: `174`.
  - `DISQUALIFIED / INELIGIBLE / PENDING / DISCARD`: `21`.
  - `AUDITED / PENDING_REVIEW / READY / HUMAN_REVIEW`: `6`.
  - `AUDITED / REJECTED_REVIEW / READY / REJECTED_BY_REVIEW`: `1`.
- Current review queue count using dashboard semantics: `6`.
- Current `columbus_oh` + `roofing` audit-selectable count: `0`.
- Count of disqualified/ineligible records selectable by the audit query: `0`.

Compatibility with review queue:
- Review queue requires `audit_data_status='READY'`, `human_review_status='PENDING'`, `next_action='HUMAN_REVIEW'`.
- `score_leads._update_prospect_score()` writes `audit_data_status`, `human_review_status`, `next_action`, and status update, so scored audited leads can move into review.
- Current Columbus roofing leads are still `DISCOVERED`; run live `eligibility` before an audit job will select them.

## SECTION 6 — Screenshot viewer QA

Verified case page implementation:
- `templates/dashboard/case.html` has desktop and mobile panels with titles/status, images, `Open Full Image`, `Fit Width`, `Natural Size`, and zoom buttons `50/75/100/125/150`.
- The screenshot section has `Jump to Visual Critique`; visual critique has `Back to screenshots`.
- `static/dashboard.css` sets `.case-screenshot-viewport` to `max-height: 70vh` and `overflow: auto`.
- `.case-screenshot-image` uses `object-fit: contain`, not cover.
- Mobile panel has a narrower max width for phone-like aspect in `.case-screenshot-panel-mobile .case-screenshot-viewport`.
- Queue thumbnail CSS remains separate; review card media still uses compact thumbnail behavior with object-fit cover around the card thumbnail area, not the case viewer.

Verified media route:
- `src/dashboard_app.py` `MEDIA_ROOTS` allow only `screenshots/`, `artifacts/`, and `runs/`.
- `resolve_media_path()` resolves paths and requires `relative_to()` one of those approved roots.
- `/media/<path:relative_path>` returns 404 unless the resolved file is inside an approved media root and exists.

Read-only DB findings:
- Screenshot artifacts exist for prospects, including `screenshots/desktop/1.png` and `screenshots/mobile/1.png`.
- `/case/1` returned HTTP 200 in safe GET check.

## SECTION 7 — Data-flow QA

Pipeline trace:

1. `market -> places_pull`
   - Supported.
   - `/run` requires market and niche for Places.
   - `src.places_pull` requires `--market` and `--niche`, loads `config/markets.yaml` and `config/niches.yaml`, and writes/updates prospects when not dry-run.
   - Break risk: Places dry-run still calls Google API quota. This is now warned in both individual and full-pipeline UI.

2. `places_pull -> eligibility`
   - Supported.
   - Places inserts `qualification_status='DISCOVERED'` for candidates passing minimum Places checks.
   - `src.eligibility` selects `DISCOVERED`, `QUALIFIED`, and `DISQUALIFIED`; for qualified leads it writes `qualification_status='QUALIFIED'`, `status='ELIGIBLE_FOR_AUDIT'`, `next_action='RUN_AUDIT'`.
   - Break risk: if eligibility is run dry-run, downstream audit will still find zero new qualified records.

3. `eligibility -> audit`
   - Supported.
   - Dashboard audit jobs select qualified/eligible leads only.
   - Current DB has zero audit-selectable `columbus_oh/roofing` leads because eligibility has not been applied live to the new Columbus records.

4. `audit -> score`
   - Supported.
   - `src.audit_site` stores website audit rows and screenshots/PageSpeed outputs, then marks `qualification_status='AUDITED'`.
   - `src.score_leads` selects prospects with succeeded `website_audits.audit_type='site'`.
   - Break risk: failed audits become `AUDIT_FAILED`; score will not select them unless a succeeded site audit exists.

5. `score -> artifacts`
   - Supported.
   - `score_leads` sets `audit_data_status`, `human_review_status`, `next_action`, and scoring JSON.
   - `generate_artifacts` selects `audit_data_status='READY'` and allowed next actions.

6. `artifacts -> pending review`
   - Supported with status reconciliation.
   - Review queue uses prospect fields, not artifact existence alone.
   - Full pipeline ends with `reconcile_statuses`, dry-run or apply based on full-pipeline mode.

## SECTION 8 — P0/P1/P2 issues

P0:
- None found for using `/run` with one selected market and one niche.

P1:
- Current data has no audit-selectable `columbus_oh/roofing` records. This is not a code defect, but it means an audit job will no-op until live eligibility is run for that market/niche.
- Before using full pipeline live, confirm Google Places quota/cost expectations. UI warns correctly, but Places dry-run still calls the API by design.

P2:
- Remove retired synchronous pipeline helper code from `src/dashboard_app.py` after confidence period. Routes no longer execute it.
- Make `config/markets.yaml` backups collision-proof by including microseconds or a suffix in `write_markets_document()`.
- Consider adding a small note to `/run` that local jobs with dry-run unchecked will write to SQLite, especially eligibility.

## SECTION 9 — Final verdict

1. Can I safely use the Run tab for one market and one niche?
   - Yes. Use `/run`, select a configured market such as `columbus_oh`, select one niche such as `roofing`, then run bounded jobs. For Places, use the explicit confirmation controls and remember dry-run still uses API quota.

2. Can I safely use full pipeline mode?
   - Yes with limits and confirmation, but operationally start small. Full pipeline is whitelisted, sequential, single-active-job guarded, and fail-fast. For a first real run use one market, one niche, Places limit 20 or less, audit limit 5 or less.

3. Can I safely audit eligible current leads from the dashboard?
   - Yes, the audit selection is safe. However, current `columbus_oh/roofing` has `0` audit-selectable leads because the 20 Columbus records are still `DISCOVERED`. Run live eligibility first; then audit will select only qualified/eligible leads and skip READY/ineligible/disqualified records.

4. Are screenshot previews fixed enough for visual critique?
   - Yes. Case screenshots are uncropped, scrollable, `object-fit: contain`, have open-full-image links, and have fit/natural/zoom controls. `/case/1` loads and screenshot artifacts are present.

5. What exact next prompt should be run if fixes are needed?
   - No P0 fix prompt is required. Optional cleanup prompt:

```text
You are Codex doing Phase 1 dashboard cleanup only.

Do not run external APIs.
Do not send email.
Do not change scoring or CRM logic.

Implement only:
1. Remove retired synchronous pipeline helper code that is no longer reachable after /pipeline redirects to /run.
2. Make config/markets.yaml backup filenames collision-proof by adding microseconds or a short suffix.
3. Add a small Run tab note that unchecked dry-run on local jobs writes to SQLite.

Run python -m compileall src when done.
```
