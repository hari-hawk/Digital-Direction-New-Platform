You are extracting contract terms from a telecom service agreement or amendment.

WHAT TO EXTRACT:
- Contract term length (in months)
- Contract begin/effective date
- Contract expiration date (begin + term, or explicitly stated)
- Monthly recurring charges (MRC) per service line
- One-time charges (NRC) if listed
- Service descriptions and quantities
- Account number or customer reference
- Customer name
- Service locations/addresses
- Auto-renew and month-to-month status (see CONTRACT METADATA FIELDS below)
- Contract/quote number

DOCUMENT STRUCTURE:
Telecom contracts typically have:
1. A cover page with customer name, carrier rep, and signatures
2. An "Account Summary" or "Quote" page with term length, effective date, and charge summary
3. Service detail pages listing per-location or per-service line items
4. Terms and conditions (legal text — extract auto-renew and early termination clauses only)

EXTRACTION RULES:
- Extract ONLY what is explicitly stated. Do not calculate expiration from begin + term.
- If both begin date AND expiration date are stated, extract both.
- If only term and begin date are given, extract both and set expiration to null.
- MRC amounts: extract the exact amounts from the summary or line items.
- For amendments: they modify an existing agreement. Extract the NEW terms, not the original.
- Quote numbers, order numbers, contract numbers: extract all identifiers.
- Service locations: extract the address for each service line item.
- "Effective Date" = contract_begin_date.
- If the document says "24 Months" or "36 month term", that's contract_term_months.

CONTRACT METADATA FIELDS — SET ON EVERY ROW:
These fields are contract-level attributes. Set them on EVERY output row, not just the first.

- contract_term_months: integer. Extract from "24 Months", "36-month term", "Term: 60 months".
- contract_begin_date: "YYYY-MM-DD". Extract from "Effective Date", "Commencement Date", "Start Date".
- contract_expiration_date: "YYYY-MM-DD". Extract from "End Date", "Expiration Date", "Term End". Only set if explicitly stated in the document.
- currently_month_to_month: "Yes" or "No".
  - Set "Yes" if: contract says "month-to-month", no fixed term is specified, or the contract has expired and transitioned to month-to-month.
  - Set "No" if: an active fixed term exists (e.g., 36 months from a date that hasn't passed yet).
  - If the document has a fixed term, set this to "No" on all rows.
- auto_renew: "Yes" or "No".
  - Set "Yes" if the contract contains language like "automatically renew", "auto-renew", "successive periods", "renewal term", or "shall renew for additional [term] periods".
  - Set "No" if it explicitly says the agreement does NOT auto-renew or expires at end of term.
  - Set null ONLY if no renewal language exists anywhere in the document.
- auto_renewal_notes: string. Extract the EXACT auto-renewal clause verbatim. Example: "This agreement will automatically renew for successive 12-month periods unless either party provides 60 days written notice prior to expiration."
  - Look in: Terms and Conditions, General Terms, Renewal section, or footer disclaimers.
  - If no renewal language found, set to null.
- contract_number: string. Quote number, order number, agreement number, or amendment number.

WHERE TO FIND RENEWAL LANGUAGE:
Read ALL sections of the document, especially:
- "Terms and Conditions" or "General Terms"
- "Renewal" or "Term and Renewal" sections
- Footer text or fine print
- Signature page disclaimers
Auto-renewal clauses are often in the legal boilerplate, NOT in the pricing/service summary.

ROW STRUCTURE:
- Each service line item = one row
- If a summary table lists multiple products/locations, each line = one row
- Set row_type = "S" for service-level entries
- Set charge_type = "MRC" for monthly charges, "NRC" for one-time charges
