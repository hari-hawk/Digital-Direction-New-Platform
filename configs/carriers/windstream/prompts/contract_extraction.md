You are extracting contract terms from a Windstream service agreement or amendment.

WINDSTREAM CONTRACT FORMATS:
Windstream contracts come in several formats:
1. **Service Agreement**: Cover page + Account Summary + Service Detail + Terms & Conditions
2. **Amendment/Renewal**: Modifies existing agreement — extract the NEW terms only
3. **Quote/Proposal**: Service pricing with term commitment
4. **SUBREPORT/MOVETO**: Internal document showing service moves or additions

DOCUMENT STRUCTURE:
- Header: Master Agreement Number (= carrier_account_number), Customer Name
- "Account Summary" or "Service Summary": Term length, Effective Date, sub-account numbers
- "Service Detail" tables: Per-location or per-service line items with MRC/NRC
- "Terms and Conditions" or "General Terms": Auto-renewal, early termination, MTM language
- Product groups: Dynamic IP, SD-WAN, UCaaS, Internet Service, LAN Services, Ethernet Access

EXTRACTION RULES:
- Extract ONLY what is explicitly stated. Do not calculate or infer.
- Master Agreement Number or top-level account = carrier_account_number
- Sub-account numbers (location-level) = sub_account_number_1
- "Effective Date", "Commencement Date" = contract_begin_date (format: YYYY-MM-DD)
- "End Date", "Expiration" = contract_expiration_date (only if explicitly stated)
- "XX Months" or "XX-month term" = contract_term_months (integer)
- For amendments: extract the NEW terms and charges, not the original agreement

CONTRACT METADATA FIELDS — SET ON EVERY ROW:
These are contract-level attributes. Set them identically on ALL output rows.

- contract_term_months: integer from "36 Months", "60 month term", etc.
- contract_begin_date: YYYY-MM-DD from "Effective Date" or "Commencement Date"
- contract_expiration_date: YYYY-MM-DD from "End Date" or "Expiration" — only if explicitly stated
- currently_month_to_month: "Yes" or "No"
  - "No" if the document specifies a fixed term (e.g., 36 months)
  - "Yes" if the document says "month-to-month" or has no fixed term
- auto_renew: "Yes" or "No"
  - "Yes" if Terms & Conditions say "automatically renew", "successive periods", "auto-renew", "renewal term"
  - "No" if it says the agreement expires at end of term without renewal
  - null only if NO renewal language exists anywhere in the document
- auto_renewal_notes: EXACT verbatim text of the renewal clause from the Terms & Conditions
  - Example: "This Agreement shall automatically renew for successive one (1) year terms unless either party gives written notice of non-renewal at least sixty (60) days prior to the expiration of the then-current term."
  - Look in: General Terms and Conditions, Term and Renewal section, footer disclaimers
  - If no renewal language found, set null
- contract_number: Agreement number, quote number, order number, or amendment number

FIELD-SPECIFIC GUIDANCE:
- service_type: Map to standard categories:
  - Dynamic IP, Internet Service → "Internet"
  - SD-WAN VMware, SD-WAN → "SDWAN"
  - UCaaS Standard, UCaaS → "UCaaS"
  - CENTREX, IBN → "Voice"
  - Ethernet Access → "Ethernet"
- component_or_feature_name: Specific service description with speed/tier
  Example: "Ethernet Access - 500 Mb", "SD-WAN Service Charge 10 Mbps"
- carrier_circuit_number: Extract FULL circuit ID string if present

ROW STRUCTURE:
- Each service line item = one row
- Set row_type = "S" for service-level entries
- Set charge_type = "MRC" for monthly charges, "NRC" for one-time charges
