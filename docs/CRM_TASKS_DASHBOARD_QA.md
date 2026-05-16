# CRM Tasks + Dashboard QA

Static report-only audit. I inspected the requested source, template, CSS, and task-template files as text. I did not open or mutate SQLite, send email, call external APIs, run jobs, deploy, or execute app workflows.

## SECTION 1 — Task Schema Audit

- `crm_tasks` exists in schema definition: yes. `src/db.py` defines `CRM_TASK_SCHEMA_SQL` with `CREATE TABLE IF NOT EXISTS crm_tasks`.
- Actual live SQLite table existence: not verified, by design. This audit did not open the database.
- Columns present in schema: yes. The schema includes `id`, `task_key`, `prospect_id`, `quote_id`, `contact_id`, assignment/ownership fields, `market_state`, `task_type`, `title`, `status`, `priority`, due fields, contact snapshot fields, notes/outcome fields, snooze/completion/cancel timestamps, `metadata_json`, `created_at`, and `updated_at`.
- Constraints look appropriate: task type, status, and priority have CHECK constraints in the create-table SQL; `prospect_id`, `quote_id`, and `contact_id` have foreign keys.
- Column migrations present: yes. `CRM_TASK_COLUMN_MIGRATIONS` covers all task columns for older DBs.
- Indexes present: yes. Indexes exist for prospect, quote, assignee, owner, market state, status, due, type, and priority.
- Schema init idempotent: yes. It uses `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and additive column migration.
- Quote/money unrelated code: no quote math or money formatting logic is changed by the task schema. `quote_id` is a nullable FK/link only.
- Task templates exist: yes. `config/task_templates.yaml` maps task templates to valid `TASK_TYPES`, including follow-up, quote, call, proposal follow-up, collect assets, access needed, outreach, contract/deposit, handoff, and QA tasks.

## SECTION 2 — Task Route Inventory

| Route | Reads DB? | Writes DB? | Requires auth? | Territory scoped? | Can send email? |
| --- | --- | --- | --- | --- | --- |
| `GET /tasks` | Yes: task rows, summary rows, market options | No | Yes, via global dashboard login guard | Yes, via `load_global_tasks()` and `append_visible_market_scope()` | No |
| `GET /tasks/<task_id>` | Yes: task, prospect, contacts | No | Yes | Yes, via `require_task_access()` -> `require_prospect_access()` | No |
| `POST /case/<prospect_id>/tasks/create` | Yes: prospect, contact snapshot, optional quote ownership | Yes: creates `crm_tasks`, may log task event | Yes | Yes, explicit `require_prospect_access(prospect_id)` | No |
| `POST /tasks/<task_id>/update` | Yes: task/prospect, contact snapshot | Yes: updates `crm_tasks` | Yes | Yes, via `require_task_access()` | No |
| `POST /tasks/<task_id>/complete` | Yes: task/prospect | Yes: marks done and logs event | Yes | Yes, via `require_task_access()` | No |
| `POST /tasks/<task_id>/cancel` | Yes: task/prospect | Yes: marks cancelled and logs event | Yes | Yes, via `require_task_access()` | No |
| `POST /tasks/<task_id>/snooze` | Yes: task/prospect | Yes: sets waiting/snooze target and logs event | Yes | Yes, via `require_task_access()` | No |

Notes:
- The task routes do not call SMTP/send helpers.
- Auth is enforced globally by `require_dashboard_login()`, not by individual decorators on each task route.
- Unauthorized direct task access resolves through the associated prospect access check, preserving the existing territory denial path.

## SECTION 3 — Case Page Integration

- Tasks card exists: yes, `templates/dashboard/case.html` has `id="case-tasks"`.
- Next task visible: yes. It shows title, type, due label, priority, assigned user, and quick Done/Snooze/Edit actions.
- Add task form exists: yes, posts to `create_case_task`.
- Quick add buttons exist: yes. Follow Up, Needs Quote, Call, Proposal Follow-Up, Collect Assets, and Custom are present.
- Complete/snooze/cancel actions exist: yes for open and waiting tasks; completed tasks remain editable.
- Contact selector exists: yes, saved contact selector uses `contact_id`.
- Manual contact snapshot fields exist: yes, `contact_name`, `contact_email`, and `contact_phone`.
- Auto-created task badge exists: yes, `Auto` is rendered when task metadata has `auto_task_key`.
- Layout assessment: acceptable. The form is compact-grid styled, but it is always visible; if case pages start feeling busy during sales calls, making only the add form collapsible would be a good P2 cleanup.

## SECTION 4 — Global Tasks Page

- `/tasks` exists: yes.
- Filters exist: status, task type, priority, market, assigned_to, due bucket, and search.
- Summary cards exist: overdue, today, upcoming, waiting, and done this week.
- Grouping exists: overdue, due today, upcoming, waiting, and completed. Completed is collapsed by default with native `<details>`.
- Territory scoping works by design: `load_global_tasks()` calls `append_visible_market_scope()` before querying.
- Admin/non-admin behavior: admin users should see all scoped rows; non-admin users are restricted by visible market/state scope. Direct task actions also re-check via prospect access.
- Quick actions on global rows: Case, Done, Snooze, and Edit exist. Cancel is available from the case task lists and task detail/update flow, not directly from the global row.

## SECTION 5 — Task Automation Hooks

Implemented hooks found:

- `CONTACT_MADE`: yes. Creates `call_scheduled` task titled `Schedule call with {business_name}` with high priority and idempotency key `contact_made_schedule_call:<prospect_id>`.
- `CALL_BOOKED`: yes. Creates prepare-for-call task with idempotency key `call_booked_prepare_call:<prospect_id>`.
- Quote marked sent: yes. Creates `proposal_follow_up` task with key `proposal_sent_followup:<prospect_id>:<quote_id>`.
- Quote accepted / `CLOSED_WON`: yes. Creates `contract_deposit` task with key `closed_won_project_handoff:<prospect_id>` and `collect_assets` task with key `closed_won_collect_assets:<prospect_id>`.
- `PROJECT_ACTIVE`: yes. Creates `client_access_needed` task with key `project_active_access:<prospect_id>`.
- Dashboard outreach sent: yes. `mark_queue_sent()` creates `follow_up` task with key `outreach_sent_followup:<prospect_id>:<step>` after a successful dashboard send mark.
- Idempotency: present. `existing_auto_task_id()` checks open/in-progress/waiting tasks for matching `metadata_json.auto_task_key` before creating a new auto task.
- Duplicate caveat: idempotency is application-level, not backed by a DB uniqueness constraint on `auto_task_key`. A simultaneous double-submit race could still theoretically create duplicates.
- Page-view safety: no task creation was found in GET page-render paths.

## SECTION 6 — Overview Declutter Audit

- Flat tile wall replaced: yes. The old flat stage grid is replaced by a compact command strip and grouped `<details>` sections.
- Collapsible groups work without external JS: yes, native `<details>/<summary>` is used.
- Required groups exist:
  - Acquisition
  - Audit + Review
  - Outreach
  - Sales + Projects
  - Trash / Cleanup
- All original counts remain visible somewhere: yes for the known grouped stages, with an `Other` group created for nonzero ungrouped stages.
- Market filter still works: yes, the overview market filter remains at the top.
- Territory scoping preserved: yes, the overview still uses the existing scoped count loaders and market filter context.
- Empty state exists: yes, for a selected market with zero active prospects.
- Market Summary table remains: yes, below the grouped overview metrics.

## SECTION 7 — CRM Balance Audit

- The app now feels more balanced between lead-gen and CRM: yes. Overview is less lead-gen-dashboard-heavy, CRM has a working sales-board structure, and tasks are surfaced on case, CRM, and global task views.
- Tasks are visible enough without overwhelming: mostly yes. Case pages show a task card and next-task banner; CRM cards show task count/next task; `/tasks` provides the global operating view.
- Obvious next action for 10-20 leads: yes. CRM cards expose next action, latest quote, next due task, and links for Open Case/Create Quote/Add Task.
- CRM page caveat: the new inactive handling supports `REJECTED_REVIEW`, which exists in pipeline status definitions but was not part of the original `CRM_STAGES`. This was handled with board-specific labels rather than changing status semantics.

## SECTION 8 — P0/P1/P2 Issues

### P0

- None found in static audit.

### P1

- No per-form CSRF tokens were observed on task POST actions. The app has login enforcement, but if Railway testing exposes the app beyond a tightly trusted operator group, add CSRF protection before an active sales cycle.

### P2

- Auto-task idempotency is not DB-enforced. Current application-level checks are probably fine for single-user/manual flows, but a unique generated key strategy or metadata-key index would be cleaner.
- Case task add form is compact but always visible. Consider putting the add form behind a native `<details>` if case pages feel crowded during calls.
- Global task rows offer Done/Snooze/Edit but not Cancel directly. Cancel is available elsewhere, so this is usability cleanup, not a blocker.
- Overview empty state only appears when a specific selected market has zero active prospects; all-markets empty state could be added later.

## SECTION 9 — Final Verdict

- Can a user create and manage follow-up tasks from a case? Yes.
- Can a user see all open tasks globally? Yes, within their territory scope; admins can see all scoped tasks.
- Are unauthorized users blocked from unauthorized tasks? Yes, direct task access and actions verify through the associated prospect.
- Is overview meaningfully decluttered? Yes.
- Is it ready for Railway testing? Yes, for authenticated/internal testing. Before broader real-user sales usage, address CSRF as a P1 hardening item.
