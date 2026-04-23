You are extracting data from an AT&T invoice.

OUTPUT ORDER (CRITICAL):
Emit rows in the EXACT top-to-bottom visual reading order of the document. Do NOT group or sort by any field. Per-phone subtotals ("TotalBilledfor…"), section totals ("Total Monthly Service", "Total Company Fees and Surcharges", "Total Amount Due") must appear in the same position they occupy on the page — not collected at the end of the output.

AT&T invoices have these sections:
- Bill-At-A-Glance: summary of charges, billing name, account number, billing date
- Plans and Services: per-line/per-account charge breakdown
- Call Charges: itemized long distance calls with dates, numbers called, minutes, amounts
- Surcharges and Other Fees: Federal Regulatory Fee, Federal Universal Service Fee
- Taxes: Federal, State, Municipal, Non Home State
- Terms/notices on last pages: may contain contract terms, rate changes

CRITICAL — PAGE 1 HANDLING:
The first page typically contains BOTH a "Bill-At-A-Glance" summary AND the start of "Plans and Services" detail charges. You MUST handle both:
- SKIP the Bill-At-A-Glance section entirely (Previous Bill, Payment, Adjustments, Past Due, Total Amount Due, Billing Summary). These are account-level summaries, NOT extractable line items.
- SKIP "Detail of Payments and Adjustments" — these are adjustment records, not service charges.
- EXTRACT FROM "Plans and Services" / "Monthly Service" section which lists per-phone-number service charges. Look for "Billed for" or "Charges for" followed by phone numbers — these ARE the extractable line items with real MRC amounts.
- Do NOT be confused by large summary amounts ($10K+, $20K+) in the Bill-At-A-Glance — they are totals, not individual service charges. The actual per-line charges are typically $3-$150 each.

EXTRACTION GUIDANCE:
- "Chargesfor" or "Billedfor" followed by a phone number = start of a sub-account section. The phone number after "Billedfor"/"Chargesfor" is the phone_number for ALL rows in that section.
- "BusLocalCallingUnlimitedB $105.00" = S row (service-level BLC package charge)
- Individual features within (LineCharge, COTermination, CallerID, etc.) = C rows
- "FederalAccessCharge $11.65" = C row with charge_type "MRC" (this is an FCC-mandated service charge, not a surcharge)
- "Surcharges and Other Fees" section items (Federal Regulatory Fee, Federal Universal Service Fee) = C rows with charge_type "Surcharge"
- "Taxes" section items (Federal, State, Municipal taxes) = C rows with charge_type "Tax"
- IMPORTANT: Only service charges are "MRC". Regulatory surcharges and government taxes are separate categories.
- Items with $0.00 or no amount listed but named (CallingNameDisplay, CallerIdentification) = C rows included in package
- "TotalBilledfor" lines (per-phone subtotals) AND section totals ("Total Monthly Service", "Total Company Fees and Surcharges", "Total Government Fees and Taxes", "Total Plans and Services", "Total Current Charges", "Total Amount Due") — EXTRACT each as its own row with charge_type="Subtotal", row_type="C", component_or_feature_name=the label as printed, monthly_recurring_cost=the amount. Set phone_number only for per-phone totals ("TotalBilledfor 614-..."), null otherwise. Even $0.00 totals like "Total Government Fees and Taxes .00" must be extracted.
- Call charges section: extract summary (total calls, total minutes, total amount) per phone number
- Contract terms in notices: "Your contract term is from X to Y" → extract contract_begin_date, contract_expiration_date
- Rate change notices: informational, do not extract as line items

EVERY ROW MUST HAVE:
- phone_number: from "Billedfor"/"Chargesfor" section header. If no phone sections exist (single-line account), leave phone_number null — it will be filled in post-processing.
- billing_name: from the top of the invoice (the company/customer name printed at the top)
- service_address_1, city, state, zip, country: from the billing address at the top of the invoice
- carrier_name: "AT&T"
- carrier_account_number: the full account number from the invoice header
- charge_type: "MRC" for service charges, "Surcharge" for regulatory fees, "Tax" for taxes
- currency: "USD"

AT&T-SPECIFIC:
- Account number format: XXX XXX-XXXX XXX X (e.g., "555 123-4567 890 1")
- Two-column layout: left and right columns may interleave in text. Process each "Chargesfor/Billedfor" block independently.
- "MonthlyService-Continued" means charges continue from previous page
