# Contract Template Variables

The source legal agreement is:

```text
contract_templates/service_contract.docx
```

The CRM/docxtpl working template is:

```text
contract_templates/service_contract_template.docx
```

The generated template preserves the short-form agreement language and adds only docxtpl placeholders, minor field labels, and DocuSign anchor strings.

## Variables Added To The DOCX

Client and business:

```text
{{ business.legal_name }}
{{ business.display_name }}
{{ business.entity_type }}
{{ business.website_url }}
{{ business.billing_address }}
{{ business.phone }}
```

Primary signer:

```text
{{ signer_primary.name }}
{{ signer_primary.title }}
{{ signer_primary.email }}
{{ signer_primary.phone }}
```

Optional client representatives:

```text
{{ signer_2.name }}
{{ signer_2.title }}
{{ signer_2.email }}
{{ signer_3.name }}
{{ signer_3.title }}
{{ signer_3.email }}
```

Quote and service agreement:

```text
{{ quote.quote_key }}
{{ quote.package_name }}
{{ quote.package_description }}
{{ quote.one_time_total }}
{{ quote.recurring_monthly_total }}
{{ quote.deposit_due }}
{{ quote.balance_due }}
{{ quote.term_months }}
{{ quote.valid_until }}
```

Contract:

```text
{{ contract.contract_key }}
{{ contract.effective_date }}
{{ contract.start_date }}
{{ contract.duration }}
{{ contract.additional_sections }}
```

Scope:

```text
{{ scope.included_items }}
{{ scope.add_ons }}
{{ scope.recurring_items }}
{{ scope.assumptions }}
```

## DocuSign Anchor Strings

Provider:

```text
/tgs_provider_sign/
/tgs_provider_date/
```

Client Representative #1:

```text
/tgs_client1_sign/
/tgs_client1_date/
```

Client Representative #2:

```text
/tgs_client2_sign/
/tgs_client2_date/
```

Client Representative #3:

```text
/tgs_client3_sign/
/tgs_client3_date/
```

The anchors are placed near the matching signature/date lines and formatted as unobtrusive 1 pt white text. Optional signer lines may remain visible in the template; the DocuSign payload should only create tabs for configured signers.

## Auto-Filled Variables

These are populated from quote, prospect, contact, or stored contract data when available:

```text
business.display_name
business.website_url
business.billing_address
business.phone
quote.quote_key
quote.package_name
quote.package_description
quote.one_time_total
quote.recurring_monthly_total
quote.deposit_due
quote.balance_due
quote.term_months
quote.valid_until
contract.contract_key
contract.duration
scope.included_items
scope.add_ons
scope.recurring_items
scope.assumptions
```

## Manual Confirmation Required

Sales must manually confirm these before generation/signature use:

```text
business.legal_name
business.entity_type
signer_primary.name
signer_primary.title
signer_primary.email
signer_primary.phone
signer_2.name
signer_2.title
signer_2.email
signer_3.name
signer_3.title
signer_3.email
contract.effective_date
contract.start_date
contract.additional_sections
```

## Required Before DocuSign Send

The current dashboard preflight requires:

```text
business.legal_name
signer_primary.title
at least one required signer with name and email
generated_docx_path exists
DocuSign configuration validates
```

The primary signer should have name, title, and email confirmed. Optional signers #2 and #3 are not mandatory unless the salesman marks/configures them as required for that client.
