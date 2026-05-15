# OUTBOUND_COPY_IMPLEMENTATION_MAP

Scope: report only. Inspected repo root `ai-local-site-leads`. No application code, templates, config, SQLite data, email, SMTP, public packet deployment, or external APIs were modified.

## SECTION 1 - Current Draft Generator Architecture

Exact files involved:

- `src/outreach_drafts.py` - deterministic draft generator.
- `templates/outreach/email_1.txt.j2` through `templates/outreach/email_4.txt.j2` - four sequence templates.
- `src/db.py` - `artifacts`, `prospects`, `contacts`, `website_audits`, and `suppression_list` schema/upserts used by generator.
- `src/config.py` - `project_path()` for locating templates and output files.
- `src/cli_utils.py` - shared CLI parser/context.
- `src/dashboard_app.py` - case page preview and `POST /case/<id>/outreach-drafts` subprocess trigger.
- `src/send_outreach.py` and dashboard `/send` code - consume generated draft artifacts later, but are not part of draft generation.

Primary functions/classes in `src/outreach_drafts.py`:

- `Issue` dataclass: normalized issue with `key`, `claim`, `evidence`, `source`, `themes`, `priority`, optional `severity`, `points`, `reason`.
- `_select_candidates()`: chooses prospects for draft generation.
- `_load_contact()`, `_contact_sort_key()`, `_normalize_email()`, `_email_suppressed()`: select recipient and gate suppressed email.
- `_draft_relative_path()`, `_drafts_exist()`: draft artifact/file existence checks.
- `_load_audits()`, `_score_explanation()`: load stored evidence.
- `_build_issues()`, `_add_issue()`, `_visual_issues()`, `_pagespeed_issues()`, `_site_issues()`, `_apply_website_pain_reason_priority()`, `_issue_from_score_reason()`: issue construction and priority.
- `_step_issues()`, `_select_step_issues()`: per-email issue selection.
- `_load_templates()`, `_template_context()`, `_render_draft()`: Jinja rendering.
- `_store_draft_artifact()`: artifact row upsert.
- `_mark_outreach_drafted()`: prospect status transition.
- `_generate_for_prospect()`, `main()`: orchestration.

Candidate selection logic:

- Base filter: `human_review_decision = APPROVED`.
- Batch mode requires `next_action = APPROVED_FOR_OUTREACH`.
- Single `--prospect-id` mode also requires `id = ?`; if `--force`, allows `next_action IN (APPROVED_FOR_OUTREACH, SEND_OUTREACH)`, else only `APPROVED_FOR_OUTREACH`.
- Status must be null/blank/`APPROVED_FOR_OUTREACH`/`OUTREACH_DRAFTED`.
- Optional `--market`, `--niche`, `--limit`.
- Order: `expected_close_score DESC, website_pain_score DESC, id`.
- Per prospect gates:
  - selected contact email required unless `--include-missing-email`;
  - active email suppression skips;
  - existing four ready `email_draft` artifacts with files skip unless `--force`;
  - no grounded issues skips.

Issue-building logic:

- `_visual_issues()` reads `website_audits.audit_type = visual_review`, expects `findings.top_issues` and/or `findings.issues`, keeps only present severity >= 3 items, maps category to generic `VISUAL_CLAIM_COPY`, and stores original label/severity/note/evidence area in `evidence`.
- `_pagespeed_issues()` reads `pagespeed_mobile` and `pagespeed_desktop`, only statuses `succeeded` or `fallback_succeeded`; mobile score < 50 and desktop score < 60 create issues.
- `_site_issues()` reads `site` audit only when status `succeeded`; creates issues for missing tel link, missing conversion path, missing form, missing service pages, missing tracking, missing schema, legacy page builder, locked platform, weak title/meta.
- `_apply_website_pain_reason_priority()` reads `score_explanation_json.top_reasons` or fallback `lead_score.findings.top_reasons`; only `category = website_pain`; maps exact reason strings through `REASON_KEY_MAP`; boosts existing priorities or creates fallback issues from `_issue_from_score_reason()`.

Issue-selection logic:

- Global issues are sorted by `(-priority, key)`.
- Email 1: top 5 issues.
- Email 2: up to 4 issues matching `mobile`, `cta`, `pagespeed`, then fallback to at least 3 if available.
- Email 3: up to 4 issues matching `buyer_path`, `service`, `local_seo`, `trust`, `tracking`, then fallback to at least 3 if available.
- Email 4: top 3 issues.
- Because visual issues have priorities around `2000+`, they often dominate every step and cause repeated copy across emails.

Template rendering logic:

- `_load_templates()` uses Jinja `FileSystemLoader(project_path("templates/outreach"))`.
- Autoescape is off; whitespace options enabled.
- `_template_context()` passes fixed variables, including `audit_reference = "the case file/audit notes"`, fixed P.S. opt-out line, and `sender_name = "[Your Name]"`.
- `_render_draft()` renders, strips outer whitespace, appends one newline, creates parent dir, and writes only if text changed.

Artifact writing logic:

- Draft files go to `runs/latest/outreach_drafts/<prospect_id>/email_<step>.txt`.
- Artifacts are upserted with:
  - `artifact_key = "<prospect_id>:email_<step>"`
  - `artifact_type = "email_draft"`
  - `path = runs/latest/outreach_drafts/<id>/email_<step>.txt`
  - `status = "ready"`
  - `content_hash = stable_hash(text)`
  - `metadata_json.subject`
  - `metadata_json.step`
  - `metadata_json.recipient_email`
  - `metadata_json.selected_issues`
  - step 1 also gets `metadata_json.subject_options`.

Status updates after draft generation:

- `_mark_outreach_drafted()` sets `prospects.status = OUTREACH_DRAFTED`, `next_action = SEND_OUTREACH`, `updated_at = utc_now()`.
- This also happens when existing drafts are found and `--force` is not used.
- In dry-run mode, no draft files are written, no artifact rows are upserted, and no final commit occurs.

## SECTION 2 - Current Template Variables

Variables available from `_template_context()` for all templates:

- Prospect/contact: `business_name`, `contact_name`, `recipient_email`, `website_url`, `market`, `niche`.
- Draft: `subject`, `step`, `all_issue_count`.
- Issues: `issues`, where each item has `key`, `claim`, `evidence`, `source`, `themes`, `priority`, `severity`, `points`, `reason`.
- Fixed copy: `audit_reference`, `opt_out_line`, `sender_name`.

`templates/outreach/email_1.txt.j2`

- Variables used: `subject`, `contact_name`, `sender_name`, `business_name`, `issues[*].claim`, `audit_reference`, `opt_out_line`.
- Available but unused: `recipient_email`, `website_url`, `market`, `niche`, `all_issue_count`, `step`, issue evidence/source/themes/severity/points/reason.
- Missing for more personal copy: `public_packet_url`, `contact_first_name`, `sender_company_name`, `business_domain`, `city/state`, `niche_label`, `primary_service_guess`, `top_issue_title`, `top_issue_evidence_safe`, `top_issue_recommendation`, `public_packet_issue_titles`.

`templates/outreach/email_2.txt.j2`

- Variables used: `subject`, `contact_name`, `business_name`, `issues[*].claim`, `opt_out_line`, `sender_name`.
- Available but unused: `recipient_email`, `website_url`, `market`, `niche`, `all_issue_count`, `step`, issue evidence/source/themes/severity/points/reason, `audit_reference`.
- Missing for more personal copy: `public_packet_url`, `mobile_issue_summary`, `call_path_observation`, `phone_present`, `tap_to_call_found`, `form_found`, `market_label`, `niche_label`, `sender_company_name`.

`templates/outreach/email_3.txt.j2`

- Variables used: `subject`, `contact_name`, `business_name`, `issues[*].claim`, `opt_out_line`, `sender_name`.
- Available but unused: `recipient_email`, `website_url`, `market`, `niche`, `all_issue_count`, `step`, issue evidence/source/themes/severity/points/reason, `audit_reference`.
- Missing for more personal copy: `public_packet_url`, `service_page_status`, `service_area_status`, `tracking_status`, `local_search_observation`, `niche_label`, `market_label`, issue recommendation copy.

`templates/outreach/email_4.txt.j2`

- Variables used: `subject`, `contact_name`, `business_name`, `issues[*].claim`, `audit_reference`, `opt_out_line`, `sender_name`.
- Available but unused: `recipient_email`, `website_url`, `market`, `niche`, `all_issue_count`, `step`, issue evidence/source/themes/severity/points/reason.
- Missing for more personal copy: `public_packet_url`, `close_reason`, `sender_company_name`, `offer_positioning`, `next_step_label`, `top_issue_titles`, `packet_link_line`.

Global copy-context gaps:

- No full public packet URL is passed into the generator.
- No configured sender/company/footer data is passed into the generator.
- No prospect address/city/state/phone/rating/review count/primary type/types are passed.
- No contact role or first-name parsing is passed.
- No safe issue recommendation field is passed; templates only use `issue.claim`.
- No public packet selected issues are passed, so draft and packet may diverge.

## SECTION 3 - Current Issue Sources

Manual visual review:

- Entered on case page in `src/dashboard_app.py`.
- `parse_visual_review_form()` stores per-category `present`, `severity`, `note`, `email_safe_claim`, `evidence_area`, and `label`.
- `top_visual_issues_from_map()` keeps present severity >= 3, max 5.
- `save_visual_review()` upserts `website_audits` with `audit_type = visual_review`, `status = reviewed`, `score`, `summary`, and `findings_json`.
- Draft generator reads `visual_review.findings.top_issues` and `visual_review.findings.issues`.
- Current draft copy does not use the saved `email_safe_claim`; it maps category to `VISUAL_CLAIM_COPY`. Notes/evidence areas are stored only in `Issue.evidence`, not rendered.

PageSpeed:

- `src/pagespeed.py` stores `pagespeed_mobile` and `pagespeed_desktop` audits with `score`, `status`, and `findings.metrics`.
- Generator accepts statuses `succeeded` and `fallback_succeeded`.
- Mobile score < 50 creates `pagespeed:mobile_low`.
- Desktop score < 60 creates `pagespeed:desktop_low`.
- Evidence includes status, score, and selected metric display values.

Site audit findings:

- `src/audit_site.py` extracts `tel_links`, `mailto_emails`, `visible_emails`, `contact_page_links`, `service_page_links`, `booking_links`, `forms`, `tracking`, `schema`, `technology`, `title`, `meta_description`.
- Generator uses only `audit_type = site` with `status = succeeded`.
- Site-derived issue keys: `site:no_tel_link`, `site:no_conversion_path`, `site:no_form`, `site:no_service_pages`, `site:no_tracking`, `site:no_schema`, `site:legacy_builder`, `site:locked_platform`, `site:weak_title_meta`.

Lead-score website pain reasons:

- `src/score_leads.py` calculates `website_pain_score` and stores `score_explanation_json.top_reasons`.
- It also upserts `website_audits.audit_type = lead_score` with the same explanation in `findings_json`.
- Generator uses prospect `score_explanation_json` first, then fallback `lead_score.findings`.
- Only reasons matching exact strings in `REASON_KEY_MAP` affect draft issues.
- Score reasons boost priority and can create fallback issues, but they do not add richer prospect-specific copy.

Public packet artifacts:

- `src/public_packets.py` builds packet issues independently from visual review, site audit, PageSpeed, and score reasons.
- It upserts a `public_packet` artifact with `artifact_url = "/p/<token>/"` and metadata `token`, `relative_url`, `selected_issues`, `desktop_screenshot`, `mobile_screenshot`.
- It also stores `prospects.metadata_json.public_packet.public_token/local_path/relative_url/generated_at`.
- Draft generation does not load public packet artifacts or packet selected issues.

Contact readiness:

- `src/contact_readiness.py` grades emails from score signals, website audits, and existing contacts, then writes contacts and prospect metadata.
- Draft generator does not call contact readiness. It only selects from existing `contacts`.
- `_load_contact()` chooses first normalized email, preferring metadata primary flags, `source = dashboard_manual`, higher confidence, then lower id.
- Contact metadata category/source/confidence is not passed to templates.

Prospect metadata:

- Generator uses `business_name`, `website_url`, `market`, `niche`, scores for ordering, review/action/status gates.
- Generator does not parse `prospects.metadata_json`.
- It does not use city/state/address/phone/rating/review count/primary type/types in copy.
- It does not use `metadata_json.public_packet`.

## SECTION 4 - Current Weak Copy Patterns

Robotic/internal-system-oriented phrases:

- `"[Your Name]"` from `DEFAULT_SENDER_NAME` and generated signatures.
- `"the case file/audit notes"` from `audit_reference`.
- `"The audit notes that support that:"` in email 3.
- `"The full audit has the rest of the evidence"` in email 3.
- `"The site audit did not..."` in generated issue claims.
- `"The lead-score audit recorded..."` in fallback issue claims.
- `"detected"` in analytics/schema/platform/builder claims.
- `"stored lead score reason"` and similar packet language are not currently in emails, but are present in packet issue evidence.

Generic or templated structures:

- `Hi {{ contact_name or "there" }}` often becomes `Hi there`.
- `A few items stood out:` followed by generic bullets.
- `The main friction I would tighten:` followed by generic bullets.
- `The short version:` followed by repeated top issues.
- Same top visual issues repeat across emails because visual priorities dominate.
- Email 1 can include 5 bullets; email 2 and 3 can include 4 bullets, making the sequence feel list-heavy.

Too harsh or vague:

- `"creates clear friction"`
- `"not prominent enough"`
- `"not obvious enough"`
- `"too many competing elements"`
- `"thin"`
- `"weakens perceived polish"`
- `"concerning mobile conversion issue"`
- `"harder than necessary"`

Too long or repetitive:

- `"conversion issue"` appears in PageSpeed issue claims and fallback claims.
- `"high-intent visitor"` appears in visual/content/CTA claims and email 2 body.
- `"call/request path"`, `"buyer path"`, `"mobile path"`, and `"full issue list"` repeat.

Potentially aggressive:

- Subject option `"Private website teardown for {business_name}"`.
- `"teardown"` also appears elsewhere in sales packet copy; for cold outbound it can sound adversarial.

Legally/commercially risky or overclaimed:

- No guaranteed ranking/revenue/lead claims were found in current outreach templates.
- No unsourced statistics were found in current outreach templates.
- `"straightforward fixes"` can overstate implementation certainty.
- `"search, ads, or a referral"` assumes traffic sources not necessarily evidenced.
- `"I took a second pass"` implies a second human review; only use if the workflow actually supports that representation.
- `"detected"` can imply a definitive technical finding even when audits are heuristic.

## SECTION 5 - Public Packet URL Handling

Availability during draft generation:

- Not available in `src/outreach_drafts.py`.
- The generator loads audits but does not load `artifacts` or `prospects.metadata_json.public_packet`.
- `_template_context()` does not include `public_packet_url`.

Whether draft files include it:

- Current `runs/latest/outreach_drafts/.../email_*.txt` files do not include public packet URLs.
- Outreach templates do not reference a packet URL.
- Draft artifact metadata does not include packet artifact id or packet URL.

Whether `send_outreach.py` appends it later:

- CLI `src/send_outreach.py` does not append public packet URLs.
- It only loads the `email_draft` artifact and sends the draft body plus footer.
- It does not require or query `public_packet`.

Whether dashboard sending appends it later:

- Dashboard `/outbound` requires a public packet before queue creation.
- `create_step_1_send_queue()` writes `outreach_queue.public_packet_artifact_id` and queue metadata `public_packet_url`.
- `/send` uses `prepare_send_queue_row()` to compose `public_packet_url`.
- `body_with_public_packet()` appends: `I put the short audit draft here: {packet_url}` if the URL is not already in the draft body.

Whether readiness requires it:

- Dashboard readiness requires it:
  - `build_outbound_row()` marks `packet_ready` only when a ready `public_packet` artifact exists and `artifact_public_packet_url()` returns a URL.
  - `prepare_send_queue_row()` blocks on missing `public_packet_artifact_id`, non-ready packet, or missing packet URL.
- CLI `src/send_outreach.py` does not require public packet readiness.

Exact artifact/queue/event fields involved:

- `artifacts.artifact_type = "public_packet"`.
- `artifacts.artifact_key = "<prospect_id>:public_packet"`.
- `artifacts.path = "public_outreach/p/<token>/index.html"`.
- `artifacts.artifact_url = "/p/<token>/"`.
- `artifacts.metadata_json.relative_url = "/p/<token>/"`.
- `artifacts.metadata_json.token`.
- `artifacts.metadata_json.selected_issues`.
- `prospects.metadata_json.public_packet.public_token`.
- `prospects.metadata_json.public_packet.local_path`.
- `prospects.metadata_json.public_packet.relative_url`.
- `outreach_queue.public_packet_artifact_id`.
- `outreach_queue.metadata_json.public_packet_url`.
- Dashboard send `outreach_events.metadata_json.public_packet_url`.

Base URL handling:

- `src/dashboard_app.py.load_public_packet_base_url()` reads `PUBLIC_PACKET_BASE_URL` env first, then `config/outreach.yaml.defaults.public_packet_base_url`, then root config keys.
- `.env.example` currently documents `ARTIFACT_BASE_URL`, not `PUBLIC_PACKET_BASE_URL`.
- `config/outreach.yaml` currently has no `public_packet_base_url` value.

## SECTION 6 - Sender/Default Variable Handling

Draft generator sender:

- `src/outreach_drafts.py` hardcodes `DEFAULT_SENDER_NAME = "[Your Name]"`.
- `_template_context()` sets `sender_name` to that placeholder.
- It does not read `config/outreach.yaml` or env sender values.
- Templates include `-- {{ sender_name }}`, so generated drafts include `-- [Your Name]`.

Company name:

- `config/outreach.yaml.defaults.business_name` exists but is blank.
- `.env.example` has `OUTREACH_BUSINESS_NAME=`.
- Draft generator does not use either.
- `src/send_outreach.py` and dashboard `/send` can include business name in the send footer if configured.

Opt-out line:

- Draft generator uses fixed `opt_out_line = 'P.S. If this is not relevant, reply "not interested" and I will not follow up.'`.
- CLI send footer adds unsubscribe language at send time.
- Dashboard send footer adds `unsubscribe_instruction` from config or generated unsubscribe language at send time.
- Result risk: draft body can contain a P.S. not-interested line and final send footer can also contain unsubscribe instructions.

Physical address/footer:

- Draft generation does not add physical address.
- CLI `src/send_outreach.py._footer()` appends sender name, optional business name, optional physical address, and unsubscribe line.
- Dashboard `send_email_footer()` does the same using `load_send_config()`.
- Dashboard batch send requires sender business name, physical address, and unsubscribe email before real send.

Subject metadata:

- Draft files include a first line `Subject: {{ subject }}`.
- Draft artifact metadata stores `subject`.
- Step 1 artifact metadata also stores `subject_options`.
- `src/send_outreach.py` sends metadata subject, not the file header.
- Dashboard queue stores `outreach_queue.subject`.
- Dashboard send stores `outreach_events.subject`.

Important footer mismatch:

- CLI `src/send_outreach.py._body_without_subject_or_placeholder_footer()` strips `-- [Your Name]` or `-- {{ sender_name }}` before sending.
- Dashboard send path uses `split_email_draft_text()` and does not strip the placeholder signature before appending its footer.
- Therefore current dashboard-sent email can include `-- [Your Name]` plus the real footer unless the draft file is manually edited.

## SECTION 7 - Exact Recommended Implementation Plan

Smallest safe target: make generated drafts contain the final intended cold-email body preview, with packet link and no placeholders, while leaving SMTP, queueing, suppression, packet generation, and SQLite schema untouched.

1. Generator changes in `src/outreach_drafts.py`.

- Load outreach defaults once using existing `load_yaml_config("outreach.yaml")` plus env overrides for `OUTREACH_FROM_NAME`, `OUTREACH_BUSINESS_NAME`, `OUTREACH_UNSUBSCRIBE_EMAIL`, and `PUBLIC_PACKET_BASE_URL`.
- Add `_load_public_packet()` or `_public_packet_url()` that reads the ready `public_packet` artifact for the prospect and composes the full URL the same way `dashboard_app.artifact_public_packet_url()` does.
- Add `public_packet_url` and `public_packet_available` to `_template_context()`.
- Add `sender_company_name`, `sender_name`, and optionally `unsubscribe_instruction` to `_template_context()`.
- Add `contact_first_name` from contact name.
- Add `business_domain`, `city`, `state`, `formatted_address`, `phone`, and cleaner `market_label`/`niche_label` when available.
- Limit email 1 to 3-4 issues, not 5.
- Prefer packet selected issue titles/recommendations when a public packet exists, so email and packet match.

2. Copy helper.

- No new helper file is required for the first pass; add small private helpers inside `src/outreach_drafts.py`.
- If copy logic grows, create `src/outbound_copy.py` for:
  - banned phrase checks;
  - first-name parsing;
  - public packet URL composition;
  - issue claim humanization;
  - deterministic phrase variation.

3. Template changes in `templates/outreach/email_1.txt.j2` through `email_4.txt.j2`.

- Remove `audit_reference`, `case file`, `audit notes`, `full audit`, and `teardown`.
- Remove template signatures (`-- {{ sender_name }}`) or make the sender footer preview consistent with the actual send path. Preferred: no signature in templates; send footer owns sender/compliance lines.
- Include the public packet link naturally in email 1, not only appended during dashboard send.
- Use 3-4 bullets max; bullets should be issue titles plus plain-language implications/recommendations, not raw `issue.claim` only.
- Replace "detected", "lead-score audit recorded", and "site audit did not" with softer phrasing such as "I did not see..." only when evidence supports it.
- Align positioning with Trade Growth Studio:
  - existing website replacement;
  - audit-backed redesign;
  - mobile-first call/request paths;
  - conversion infrastructure;
  - service-page/service-area architecture;
  - tracking-ready managed web operations.
- Do not mention "AI websites."

4. Config keys.

- Prefer existing keys where possible:
  - `defaults.from_name`
  - `defaults.business_name`
  - `defaults.unsubscribe_instruction`
  - `defaults.unsubscribe_email`
- Add/document `defaults.public_packet_base_url` if env-only `PUBLIC_PACKET_BASE_URL` is not enough.
- Add `PUBLIC_PACKET_BASE_URL=` to `.env.example` because dashboard/email infra already expects it.
- Avoid adding new schema or database fields for copy alone.

5. Dashboard preview changes, if needed.

- In `src/dashboard_app.py`, case page preview should show the same composed body that will be sent:
  - subject;
  - body;
  - public packet link;
  - footer/opt-out handling.
- At minimum, update preview to flag placeholders and missing packet URL.
- Also consider stripping placeholder signatures in dashboard send if old drafts remain.

6. QA/linter.

- Add a small deterministic copy QA script or unit test, preferably not coupled to SMTP.
- Fail generated drafts if they contain:
  - `[Your Name]`
  - `{{` or `}}`
  - `case file`
  - `audit notes`
  - `lead-score audit recorded`
  - `teardown`
  - guaranteed ranking/revenue/lead claims
  - missing public packet URL when packet is required
  - more than 4 issue bullets
  - duplicate signature/footer markers.
- Add snapshot tests for at least one prospect context with packet URL and one without contact name.

## SECTION 8 - Do-Not-Touch List

Do not edit these files during copy improvements unless the scope explicitly expands beyond draft copy/preview:

- `src/db.py` - no schema/data model changes needed.
- `src/send_outreach.py` - leave SMTP transport, suppression, duplicate-send, daily cap, and event logic alone.
- `src/contact_readiness.py` - contact grading is not a copy problem.
- `src/public_packets.py` - packet token/path/artifact generation is already the source of truth.
- `src/pagespeed.py` - no measurement changes needed.
- `src/audit_site.py` - no crawler/audit changes needed.
- `src/score_leads.py` - no scoring changes needed.
- `src/email_infra_check.py` - no infra behavior changes needed, except documentation alignment if adding `PUBLIC_PACKET_BASE_URL` to examples.
- `scripts/deploy_public_packets_cloudflare.ps1` and `.bat` - no deployment changes.
- `data/*.db` - do not mutate SQLite for copy work.
- `public_outreach/**` - do not redeploy or rewrite existing packets as part of copy-only changes.
- `runs/latest/outreach_queue`/send artifacts if present - do not alter queued send state for copy QA.

Files expected to be edited for copy improvements:

- `src/outreach_drafts.py`
- `templates/outreach/email_1.txt.j2`
- `templates/outreach/email_2.txt.j2`
- `templates/outreach/email_3.txt.j2`
- `templates/outreach/email_4.txt.j2`
- Optional: `src/dashboard_app.py` only for preview/old-placeholder detection.
- Optional: `.env.example` and `config/outreach.yaml` only to document/configure public packet base URL and sender identity.

## SECTION 9 - Acceptance Criteria For Improved Outbound Copy

Pass/fail criteria:

- No placeholders: no `[Your Name]`, empty sender names, raw Jinja braces, or unfilled subject/body variables.
- Public packet link is included naturally in generated email 1 body when a ready packet exists; it is also stored in draft metadata.
- Dashboard send does not need to append a surprise packet sentence if the draft already includes the link.
- No duplicate signature/footer when sent from dashboard or CLI.
- Each email uses at most 3-4 issues.
- Issues are specific enough to tie back to stored evidence, but do not expose internal audit jargon.
- Business name is used when available.
- Contact first name is used only when available.
- Niche/market/city/service context is used when available.
- No fake relationship or fake prior conversation.
- No claims of guaranteed ranking, revenue, lead volume, conversion lift, licensing, insurance, warranties, or affiliation.
- No unsourced statistics.
- No internal phrases: `case file`, `audit notes`, `lead-score audit recorded`, `stored lead score reason`, `detected` as the main proof word, or `teardown`.
- No generic body that could apply unchanged to any business after only swapping the business name.
- Tone matches Trade Growth Studio positioning: audit-backed website replacement/redesign, mobile call/request paths, conversion infrastructure, service-page/service-area architecture, tracking-ready managed operations.
- The copy never sells "AI websites."
- Language is deterministic but varied across steps; repeated top issues should not produce repeated paragraphs.
- Subject metadata, draft file subject header, queue subject, and send event subject remain consistent.
- Copy QA/linter can be run without sending email, calling external APIs, mutating SQLite, or deploying public packets.
