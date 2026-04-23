You are extracting telecom billing data from a carrier invoice.

The carrier is NOT pre-configured — you must detect the carrier name directly from the document. Look for a recognizable carrier/provider name printed on the bill (Frontier, Lumen, Verizon, T-Mobile, Comcast, Cox, Altice, Spectrum, AT&T, Windstream, Peerless, CenturyLink, etc.) and populate the `carrier_name` field with what the document says.

OUTPUT ORDER (CRITICAL):
Emit rows in the EXACT top-to-bottom visual reading order of the source document. Do NOT group, sort, or reorder by any field. If a "Total Monthly Service" line appears right after the service items (before surcharges and taxes), it must appear in that position in your JSON array — not at the end. The final JSON array's row sequence must match the PDF's visual sequence.

Invoices typically contain:
- A header / bill summary (billing name, account number, invoice date, total due)
- A breakdown of service charges per line, circuit, or location
- Surcharges and regulatory fees (Federal USF, State USF, Regulatory Fee, E911)
- Taxes (Federal, State, Municipal, County)
- Optionally: long-distance usage, call detail, contract terms, notices

EXTRACTION GUIDANCE:
- EACH billable line item is a separate output row.
  - A service summary line (e.g., "Business Phone Line $45.00") = row_type "S"
  - Individual features/components under a service (e.g., "Call Waiting $3.00", "Voicemail $5.00") = row_type "C"
  - Stand-alone charges with no parent = row_type "C"
- phone_number: the line/BTN the charge belongs to. Extract exactly as printed.
- carrier_account_number: the primary account number on the invoice header. Preserve formatting (spaces, dashes).
- sub_account_number_1: a location/line-level sub-account if the invoice groups charges under sub-accounts.
- billing_name: the customer/company name on the invoice header.
- service_address_1 / city / state / zip / country: the SERVICE address, not the remit-to / payment address.
- monthly_recurring_cost: exact dollar amount as shown. Do not round or recompute.
- charge_type: "MRC" for recurring service charges · "NRC" for one-time fees · "Usage" for per-call/per-minute · "Surcharge" for regulatory fees · "Tax" for government taxes.
- currency: "USD" unless the invoice clearly states otherwise.

TAX AND SURCHARGE ROWS:
- Taxes and regulatory fees typically appear in a dedicated section near the bottom. Each named line item becomes its own C row with the matching charge_type.
- If the invoice shows a per-line tax breakdown AND an invoice-level tax summary, prefer the per-line breakdown. Do NOT emit both (they would duplicate).

ZERO-COST ITEMS:
- If a feature is listed with $0.00, extract it with `monthly_recurring_cost: 0.00`. These are real included features.

NAMES:
- If text appears with spaces removed ("BusinessPhoneLine"), reconstruct natural spacing: "Business Phone Line".
- Extract the complete component name including tier descriptors ("Pro", "Plus", "Gig", "Ultra").

WHAT NOT TO EXTRACT:
- Previous-balance, payments-received, adjustment lines from the bill summary — these are not billable services.
- Carrier-side helpline/support phone numbers — these are not customer phone_number values.
- Marketing notices, upgrade offers, page footers.

SECTION AND DOCUMENT TOTALS (ALSO EXTRACT):
In addition to the per-line items above, capture each explicitly-labeled "Total …" line that aggregates a section or the whole bill. These sit alongside — not in place of — the individual item rows.

Emit each total as its own output row with:
- `row_type`: "C"
- `charge_type`: "Subtotal"
- `component_or_feature_name`: the label exactly as printed (e.g., "Total Monthly Service", "Total Company Fees and Surcharges", "Total Government Fees and Taxes", "Total Plans and Services", "Total Current Charges", "Total Amount Due", "Total Billed for 614-555-1234")
- `monthly_recurring_cost`: the dollar amount shown on that total line
- `phone_number`: populate ONLY if the total is scoped to a specific phone line (e.g., "Total Billed for 614-555-1234"). Otherwise leave null — document-level totals have no phone_number.
- `carrier_account_number`: same as the per-line rows.

Capture each labeled total even if the value is $0.00 (e.g., "Total Government Fees and Taxes: .00"). Do NOT invent totals that are not explicitly labeled in the document.

Return a JSON array. One object per extracted row. If the document has no extractable billing data, return [].
