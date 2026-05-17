# Contract Template Generation QA

Verification performed without DocuSign calls, envelope sends, email sends, live-data mutation, legal-language edits, or unrelated code changes.

## Template File

- `contract_templates/service_contract_template.docx` present: yes.
- Source legal document `contract_templates/service_contract.docx` present: yes.
- Local DOCX generation is no longer blocked by a missing template: yes, the primary template path exists.
- Visual render QA: not completed because the local renderer could not find LibreOffice/`soffice` (`FileNotFoundError: [WinError 2]`). Structural DOCX text inspection succeeded.

## Template Sections

The DOCX contains the expected short-form agreement structure:

- Client protections section: yes, `1. Client Protections - Trade Growth Studios LLC Promises`.
- Service agreement section: yes, `2. Service Agreement - Client, Package, Price, Term`.
- Client promises section: yes, `3. Client Promises - Payment, Property, Access, and Accuracy`.
- TGS signature section: yes.
- Client Representative #1 signature section: yes.
- Client Representative #2 optional signature section: yes.
- Client Representative #3 optional signature section: yes.

## DocuSign Anchors

All required anchor strings are present in `contract_templates/service_contract_template.docx`:

- `/tgs_provider_sign/`
- `/tgs_provider_date/`
- `/tgs_client1_sign/`
- `/tgs_client1_date/`
- `/tgs_client2_sign/`
- `/tgs_client2_date/`
- `/tgs_client3_sign/`
- `/tgs_client3_date/`

Anchor placement is near the matching signature/date lines. Optional signer anchors are present in the document, and DocuSign tabs should only be created for configured signers.

## Variable Match

- DOCX variables found: 34.
- Variables documented in `docs/CONTRACT_TEMPLATE_VARIABLES.md`: 34.
- Variables missing from DOCX: none.
- Undocumented variables in DOCX: none.

Required variable namespaces are present:

- `business.*`
- `signer_primary.*`
- `signer_2.*`
- `signer_3.*`
- `quote.*`
- `contract.*`
- `scope.*`

## Export Context

`src/contract_exports.py` passes the expected top-level objects to docxtpl:

- `business`
- `signer_primary`
- `signer_2`
- `signer_3`
- `quote`
- `contract`
- `scope`
- `signers`
- `anchor_map`
- `signature_blocks`
- `provider`

Context mapping covers the DOCX-only aliases:

- `business.billing_address` is mapped from the contract/prospect address line.
- `scope.add_ons` is mapped from optional quote line items.
- `quote.package_description` is synthesized from client-visible notes, quote title, or package name.
- `contract.duration` is synthesized from `quote.term_months` when not explicitly set.
- `contract.additional_sections` and scope list fields are converted into document-friendly text for DOCX rendering.

No variable mismatch was found that required legal-language changes.

## Generation Path

Expected generated DOCX path:

```text
runs/latest/contracts/<contract_key>/contract.docx
```

Code path:

- `generate_contract_artifacts(conn, contract_id)` builds context.
- `_resolve_docx_template()` selects `contract_templates/service_contract_template.docx`.
- `_render_docx_from_context()` writes `contract.docx` under `runs/latest/contracts/<contract_key>/`.
- `update_contract_generated_paths()` stores the generated DOCX/HTML paths.

Generation was not executed against CRM data because no existing contract rows were present in any local `data/*.db`, and no safe dev/test helper exists for creating fake contract production data. Exact dashboard steps to exercise generation safely:

1. Open an existing quote in the CRM.
2. Click Create Contract.
3. Edit and manually confirm legal business name, entity type, signer name/title/email, billing address, effective date, and start date.
4. Save the contract.
5. Click Generate Contract.
6. Confirm `runs/latest/contracts/<contract_key>/contract.docx` exists.

Current local environment note: `requirements.txt` includes `docxtpl`, but this workstation environment does not currently import `docxtpl`. DOCX generation will fail gracefully with a dependency warning until dependencies are installed in the runtime used by the app.

## Internal Data Exposure

Client-facing HTML/DOCX output does not render:

- internal notes
- floor/fallback pricing
- raw score/audit data
- private CRM notes

`src/contract_exports.py` was tightened so the redacted debug JSON also redacts `salesman_notes`, internal/private notes, floor/fallback price keys, score keys, and audit keys if they appear in render context.

## DocuSign Readiness

- Anchor strings are present: yes.
- Generated DOCX source path is configured: yes.
- Runtime envelope send can use the generated DOCX once a contract row exists and dependencies are installed.
- No DocuSign call was made during this QA.
- Send readiness still depends on dashboard preflight: generated DOCX exists, legal business name exists, primary signer title/authority exists, at least one required signer has name/email, and DocuSign env config validates.

## P0/P1/P2 Issues

P0:

- Current local runtime lacks `docxtpl`; install project requirements in the app runtime before testing real DOCX generation.

P1:

- No existing contract rows are available locally, so end-to-end generation from a CRM contract still needs to be exercised through the dashboard flow.
- Visual DOCX QA could not be completed because LibreOffice/`soffice` is not installed.

P2:

- Keep `runs/latest/contracts/**` private because it contains generated contract artifacts and debug files, even with redaction.

## Final Verdict

- Template file present: yes.
- Anchors present: yes.
- Generation path works by code inspection: yes.
- Generation path executed against CRM data: no, because no existing contract rows were available and no safe test helper exists.
- Variables missing: none.
- Variables unused/undocumented: none.
- Local DOCX generation blocked by missing template: no.
- Local DOCX generation blocked in this workstation by missing dependency: yes, `docxtpl` is not installed in the current runtime.
- DocuSign readiness: template/anchor side is ready; live/demo send still requires a generated DOCX from a real contract and valid DocuSign config.
