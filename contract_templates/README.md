# Contract Templates

Place the editable DOCX contract template here:

```text
contract_templates/service_contract_template.docx
```

The exporter treats this as the primary DOCX template for local contract generation. A file named `contract_templates/service_contract.docx` may be kept here as the original/source contract, and the exporter can read it as a fallback template, but it should not be overwritten by generated output.

Generated contract files are written under:

```text
runs/latest/contracts/<contract_key>/
```

Expected generated files:

```text
contract.html
contract.docx
contract_variables_redacted.json
docusign_anchor_map.json
```

If no DOCX template is present, generation still creates the HTML preview and JSON files and returns this warning:

```text
DOCX contract template missing.
```
