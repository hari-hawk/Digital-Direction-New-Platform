You are extracting service inventory from a telecom Customer Service Record (CSR).

The carrier is NOT pre-configured — detect the carrier name directly from the document and populate `carrier_name`. CSRs usually show the carrier as a logo/header or in a "Carrier:" field.

A CSR is an inventory of a customer's services, not a bill. It lists each line, circuit, or feature assigned to the account. Charges may or may not be present.

EXTRACTION GUIDANCE:
- One output row per service/feature entry:
  - A service package / primary line = row_type "S"
  - A feature under that service (Call Forwarding, Voicemail, etc.) = row_type "C"
- usoc: capture Universal Service Order Code if the CSR lists one per feature.
- phone_number: the line number. Preserve formatting.
- carrier_account_number: the primary account on the CSR header.
- billing_name / service_address: customer name and service location (not billing address).
- monthly_recurring_cost: CSRs may or may not show prices. Omit if absent; don't fabricate.
- component_or_feature_name: the exact feature name as printed (reconstruct spacing if run together).

CSRs often include "ACT DATE" / "EFF DATE" columns — these are activation dates, not contract dates. Don't map them to contract_begin_date.

Return a JSON array. Omit fields with no value.
