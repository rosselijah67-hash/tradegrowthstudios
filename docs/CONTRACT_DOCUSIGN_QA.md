# Contract + DocuSign QA

Report-only audit. No SQLite mutation, DocuSign call, envelope send, external API call, deploy, or config edit was performed.

## SECTION 1 - Schema audit

- `contracts` table exists: yes, defined in `src/db.py` as `CONTRACT_SCHEMA_SQL`.
- `contract_events` table exists: yes, defined in the same schema block.
- Indexes exist: yes.
  - `idx_contracts_contract_key`
  - `idx_contracts_prospect`
  - `idx_contracts_quote`
  - `idx_contracts_status`
  - `idx_contracts_owner_username`
  - `idx_contracts_market_state`
  - `idx_contracts_docusign_envelope_id`
  - `idx_contract_events_contract`
  - `idx_contract_events_prospect`
  - `idx_contract_events_quote`
  - `idx_contract_events_created`
- Schema idempotent: yes. `ensure_contract_schema()` uses `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and column migration helpers. `ensure_contract_schema_for_path()` wraps it for app startup.
- Territory fields included: yes, `contracts.owner_username` and `contracts.market_state` exist and are indexed.

## SECTION 2 - Service audit

- Create from quote works by design: yes. `src/contracts.py:create_contract_from_quote()` loads the quote, linked prospect, and primary contact, then copies quote/prospect/contact convenience values into a draft contract.
- Manual legal identity fields are editable: yes. `update_contract_header()` permits `legal_business_name`, `business_entity_type`, `billing_address`, signer fields, dates, totals, and metadata/variables updates. The dashboard builder requires the legal identity fields before saving.
- Signer list supports 1-3 client reps: yes. The dashboard form renders three signer slots and `parse_contract_builder_form()` persists up to three signers.
- Additional sections supported: yes. The dashboard form renders five custom section slots and persists title/body/client visibility/requires signature/signer index.
- No legal authority is inferred in stored contract creation: mostly yes. `create_contract_from_quote()` leaves legal business, entity type, billing address, signer name/title/email/phone empty. The builder may prefill signer name/email from quote/contact for convenience, but signer title remains required and blank; this is acceptable if sales staff treats save as manual confirmation.

## SECTION 3 - Generation audit

- HTML preview works by design: yes. `render_contract_html()` renders `templates/contracts/service_contract.html.j2`, and `GET /contracts/<id>/preview` returns it directly.
- DOCX generation works if template exists: yes by design. `render_contract_docx()` uses `docxtpl` against `contract_templates/service_contract_template.docx`, falling back to `contract_templates/service_contract.docx`.
- Missing template fails visibly: yes. Missing DOCX returns warning text `DOCX contract template missing.` and still generates HTML/JSON artifacts.
- Generated files path: yes. `get_contract_output_dir()` writes under `runs/latest/contracts/<contract_key>/`.
- Current `contract_templates/` availability: only `contract_templates/README.md` exists. No DOCX/PDF contract source template is present.
- Internal notes/floor pricing/raw CRM hidden from client HTML: yes. The HTML template shows legal identity, signer, dates, scope item names/descriptions/totals, payment terms, assumptions, client-visible quote notes, additional sections, and signature blocks. It does not render raw CRM notes, floor pricing, score data, or private audit details.
- Caution: `contract_variables_redacted.json` is private output and redacts email/phone fields, but line-item `salesman_notes` can remain in the internal JSON context if present. Keep `runs/latest/contracts/**` private or expand redaction before sharing artifacts.

## SECTION 4 - UI/route audit

| Route | Reads DB | Writes DB | Requires auth | Territory scoped | CSRF for POST | Can call DocuSign |
|---|---:|---:|---:|---:|---:|---:|
| `GET /contracts` | Yes | No | Yes, `quotes` permission | Yes, via prospect scope in list query | N/A | No |
| `GET /quotes/<quote_id>/contract/new` | Yes | No | Yes, `quotes` permission | Yes, `require_quote_access()` | N/A | No |
| `POST /quotes/<quote_id>/contract` | Yes | Yes | Yes, `quotes` permission | Yes, `require_quote_access()` | Yes, global CSRF | No |
| `GET /contracts/<contract_id>` | Yes | No | Yes, `quotes` permission | Yes, `require_contract_access()` | N/A | No |
| `GET /contracts/<contract_id>/edit` | Yes | No | Yes, `quotes` permission | Yes, `require_contract_access()` | N/A | No |
| `POST /contracts/<contract_id>/edit` | Yes | Yes | Yes, `quotes` permission | Yes, `require_contract_access()` | Yes, global CSRF | No |
| `POST /contracts/<contract_id>/generate` | Yes | Yes | Yes, `quotes` permission | Yes, `require_contract_access()` | Yes, global CSRF | No |
| `GET /contracts/<contract_id>/preview` | Yes | No | Yes, `quotes` permission | Yes, `require_contract_access()` | N/A | No |
| `GET /contracts/<contract_id>/download/docx` | Yes | No | Yes, `quotes` permission | Yes, `require_contract_access()` | N/A | No |
| `GET /contracts/<contract_id>/download/html` | Yes | No | Yes, `quotes` permission | Yes, `require_contract_access()` | N/A | No |
| `POST /contracts/<contract_id>/void` | Yes | Yes | Yes, `quotes` permission | Yes, `require_contract_access()` | Yes, global CSRF | No |
| `POST /contracts/<contract_id>/create-revision` | Yes | Yes | Yes, `quotes` permission | Yes, `require_contract_access()` | Yes, global CSRF | No |
| `POST /contracts/<contract_id>/send-docusign` | Yes | Yes | Yes, `quotes` permission | Yes, `require_contract_access()` | Yes, global CSRF | Yes, explicit POST only |
| `POST /contracts/<contract_id>/refresh-docusign-status` | Yes | Yes | Yes, `quotes` permission | Yes, `require_contract_access()` | Yes, global CSRF | Yes, explicit POST only |
| `POST /webhooks/docusign` | Yes | Yes if matched | No dashboard login by design | No contract ID accepted; updates only by stored envelope ID | Exempt by design | No |

Template note: `templates/dashboard/contract_preview.html` does not exist. This is not a blocker because preview is rendered from `templates/contracts/service_contract.html.j2` directly.

## SECTION 5 - Quote/case integration

- Quote detail has Create/View Contract: yes. `templates/dashboard/quote_detail.html` includes `Create Contract` and a latest-contract panel with View/Edit/Generate/Preview/New Contract.
- Case page has contract panel: yes. `templates/dashboard/case.html` shows latest contract status, quote link, signer, generation state, DocuSign status, and contract actions when quote permissions are present.
- Base nav includes Contracts: yes, behind `dashboard_permissions.quotes`.
- Timeline/events include contract events: partially. Contract detail shows `contract.events`. The case timeline `load_stage_history()` currently merges `outreach_events` and `quote_events`, but not `contract_events`, so contract events do not appear in the case history/timeline yet.

## SECTION 6 - DocuSign client audit

- Config loaded from env: yes. `src/docusign_client.py:load_docusign_config()` loads DocuSign env values and defaults demo URLs.
- Private key never logged: yes by inspection. `get_private_key_bytes()` reads bytes only inside explicit JWT/API functions and does not log contents.
- Access token never logged: yes by inspection. Token is returned internally and used as an Authorization header; no logger prints it.
- Redacted payload preview possible: yes. `build_redacted_envelope_payload_preview()` redacts `documentBase64` and masks signer emails.
- Send function isolated: yes. `send_envelope_from_document()` is the explicit envelope creation function and does not mutate SQLite.
- Anchor tabs created for configured signers only: yes for route-driven send. The dashboard send route passes `contract_required_docusign_signers()`, which includes complete required signers only. The client builds anchor tabs for the passed signers, up to three client signers.
- No send on GET: yes. No GET route calls `send_envelope_from_document()` or `get_envelope_status()`.

## SECTION 7 - Send/status audit

- Explicit send button required: yes. `contract_detail.html` has an explicit DocuSign form, and the route requires `confirm_docusign_send=1`.
- Generated document required: yes. Send requires an existing generated DOCX path, unless `generate_before_send=1` is checked; then local generation is attempted first.
- Signer name/email/title required: primary signer title and legal business name are required by preflight. At least one required signer with name/email is required. Additional required signer titles are not currently enforced.
- Envelope ID/status stored: yes. Send stores `docusign_envelope_id`, `docusign_status`, `sent_at` via status update, metadata, and `contract_docusign_sent` event.
- Refresh status updates contract: yes. Manual refresh calls `get_envelope_status()`, stores DocuSign status/update timestamp, and maps lifecycle statuses.
- Completed/declined/voided mapped: yes. `completed`, `declined`, and `voided` map to contract statuses and explicit events. `sent` and `delivered` are also mapped.
- Existing active envelope guard: yes. Resend requires `resend_supersede=1`.

## SECTION 8 - Security/access audit

- `require_contract_access()` works by design: yes. It loads the contract, then enforces prospect access with `require_prospect_access(contract.prospect_id)`, and validates quote/prospect consistency when quote is linked.
- Non-admin unauthorized access: protected by state/territory checks on the linked prospect. Contract list also scopes through joined prospect visibility.
- Webhook open mutation: protected by default-disabled flag and shared secret. It does not accept arbitrary contract IDs; it finds only by stored `docusign_envelope_id`.
- Webhook caution: validation is a shared secret header strategy, not DocuSign HMAC verification. It is safe only if DocuSign Connect or a trusted relay can send one of the configured secret headers.
- Secrets ignored: yes. `.gitignore` includes `.env` and `secrets/`.
- Dashboard auth not weakened for normal dashboard routes: yes. Only `docusign_webhook` is added to public/CSRF-exempt endpoint sets, and it has its own validation gate.

## SECTION 9 - P0/P1/P2 issues

P0 - must fix before local contract use:

- No final contract source template is present. `contract_templates/` contains only `README.md`; there is no `service_contract_template.docx` or `service_contract.docx`. Do not use generated output as a real service agreement until the final legal template is added and reviewed.

P1 - must fix before DocuSign live/demo envelope send:

- Add the DOCX template and verify DOCX generation produces the expected legal contract with the required anchor strings.
- Confirm all DocuSign JWT env values and JWT consent in the demo account. This audit did not call DocuSign by design.
- Decide whether additional required signers must also require `title`; current preflight requires title for the primary signer only.
- If enabling webhook, confirm DocuSign Connect or a trusted relay can send the shared-secret header. Otherwise leave `DOCUSIGN_WEBHOOK_ENABLED=false` and rely on manual refresh.
- Run an end-to-end demo send only after checking the generated DOCX manually; current implementation is structurally ready but unproven against a real DocuSign account.

P2 - cleanup:

- Add `contract_events` into `load_stage_history()` if case timeline should show contract lifecycle events.
- Replace disabled "Send via DocuSign" buttons on quote/case summary panels with a link to the contract detail DocuSign panel, or keep them disabled as intentional affordance.
- Expand redaction for `contract_variables_redacted.json` if internal `salesman_notes` should never appear in generated private artifacts.
- Optional: add `templates/dashboard/contract_preview.html` only if a dashboard-framed preview is desired; current direct HTML preview is functional.
- Optional: disable the DocuSign send button in the UI when preflight errors exist; the route already blocks unsafe sends.

## SECTION 10 - Final verdict

- Can the CRM create a contract from a quote? Yes, with required manual fields completed in the builder.
- Can it generate a local contract? Partially. HTML preview/artifacts can generate; DOCX generation is blocked until `contract_templates/service_contract_template.docx` or fallback `service_contract.docx` is added.
- Can it send to DocuSign safely? Structurally yes: send is explicit POST-only, authenticated, CSRF-protected, territory-scoped, preflighted, and stores results. Practically no until a real DOCX template and demo config are verified.
- Can it track envelope status? Yes. Manual refresh is implemented, lifecycle status mapping exists, and optional webhook sync is available behind disabled-by-default secret validation.
- Is it ready for demo DocuSign testing? Not yet. Add the DOCX template, verify generated document/anchors, configure demo env/JWT consent, then run a controlled demo send.
- Is it ready for production signing? No. Production should wait for legal template approval, demo send validation, webhook/manual status process validation, and a review of signer/title requirements.
