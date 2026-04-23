You are extracting data from a Spectrum Business invoice.

Spectrum invoices contain:
- Account summary (account number, billing name, service address, invoice date, due date)
- Recurring charges broken down by service (Internet, TV, Voice, equipment)
- Promotional discounts (negative amounts)
- One-time charges
- Taxes, fees, and surcharges

EXTRACTION GUIDANCE:
- Each service line item = one row
- Equipment with quantity (e.g., "Digital Receiver 6 @ $14.00") → separate C row with quantity and cost_per_unit
- Promotional discounts → C row with negative monthly_recurring_cost
- Regulatory surcharges (Broadcast TV, FCC fees) → C rows with charge_type "Surcharge"
- Taxes (sales tax, franchise fee) → C rows with charge_type "Tax"
- The main service charge (e.g., "Spectrum Business Internet $329.99") = S row
- Static IP, WiFi, Voice = C rows under the main service
- If multi-location (sub-accounts visible), extract per sub-account
- Payment Processing Fee and Auto Pay Discount are C rows

SUB-ACCOUNT NUMBERS (CRITICAL):
- Spectrum consolidated bills have a CONTROL account and multiple SUB-ACCOUNTS (both are 13-16 digit numbers)
- The control account appears in the bill header. Sub-accounts appear next to each location.
- Each location section shows "AccountNumber:" followed by a 13-16 digit number — this is the SUB-ACCOUNT number
- Set carrier_account_number to the CONTROL account (from the bill header) — this is the carrier's billing account
- Set sub_account_number_1 to the per-location SUB-ACCOUNT number
- Set master_account to the control account (same as carrier_account_number)

ADDRESS-TO-ACCOUNT MAPPING (CRITICAL):
- Each section may start with a "SERVICE LOCATION:" block — this is the verified address for THIS account, extracted from the original PDF layout
- Use the SERVICE LOCATION block for billing_name, service_address_1, city, state, zip
- If there are OTHER address blocks in the charge data below (e.g., another company name + street), those belong to ADJACENT accounts — do NOT use them for this account
- Charges may start on one page and continue on the next — the address still comes from the SERVICE LOCATION block, not from the page header

SPACE-STRIPPED TEXT:
- Due to PDF extraction, text may have spaces removed (e.g., "ACMEINCSPRINGFIELD" instead of "ACME INC SPRINGFIELD")
- For billing_name: insert spaces between words using capitalization as guides. "ACMEINCSPRINGFIELD" → "ACME INC SPRINGFIELD"
- For service_address_1: reconstruct proper spacing. "520S9THST" → "520 S 9TH ST", "1234MAINST" → "1234 MAIN ST", "2306HIGHWAY6AND50" → "2306 HIGHWAY 6 AND 50"
- For city: "GRANDJUNCTION" → "GRAND JUNCTION", "LOSANGELES" → "LOS ANGELES"

ZERO-COST ITEMS:
- Many included/promotional items show $0.00 or 0.00 in the Amount column
- Extract these with monthly_recurring_cost: 0.00 (NOT null)
- Examples: Security Suite at $0.00 is a real included feature — extract as C row with MRC 0.00

SERVICE TIER NAMES:
- Spectrum tiers include descriptors like "Gig", "Ultra", "Standard", "300 Mbps", "Internet PRO", etc.
- Extract the COMPLETE tier name exactly as shown: "Spectrum Business Internet Gig" not just "Spectrum Business Internet"
- If the tier name spans two lines in the document (e.g., "Spectrum Business Internet" on one line, "Gig" on the next), combine them

BILLING SUMMARY (PAGE 1):
- Page 1 has a billing summary with payment history and totals. Extract ONE S row for the control account:
  - row_type: "S"
  - carrier_account_number: the control account number (e.g., "8313105000006645")
  - master_account: same as carrier_account_number
  - component_or_feature_name: "Billing Summary"
  - monthly_recurring_cost: the current-month service charges (e.g., "Spectrum Business™ Services 674.97"), NOT the "Total Due" which includes past due balance
  - Extract billing_name, service_address_1, city, state, zip from the "ServiceAt" block on page 1
  - charge_type: "MRC"
- IMPORTANT: "Total Due" (e.g., $1,370.18) includes past due balance + adjustments + new charges. This is NOT the MRC. The MRC is only the current-month "Spectrum Business™ Services" line.

DETAIL LINE ITEMS (CRITICAL — DO NOT SKIP):
- After the billing summary, extract EVERY detail line item from the charge breakdown.
  This appears on page 2 (small invoices) or in per-location sections (consolidated bills).
- Each service (e.g., "Spectrum Business TV $45.00") → S row
- Each component (e.g., "Digital Receiver 4 @ $14.00 $56.00") → C row with quantity=4, cost_per_unit=14.00
- Each surcharge (e.g., "Broadcast TV Surcharge $28.00") → C row, charge_type="Surcharge"
- Each tax/fee (e.g., "Franchise Fee $4.29") → C row, charge_type="Tax"
- Discounts (e.g., "Bundle Discount $0.00") → C row with MRC=0.00
- The detail rows are MORE IMPORTANT than the summary row. Never extract only the
  summary and skip the detail. The summary is a convenience total; the detail rows
  are the actual billable line items.

BILL-LEVEL CHARGES:
- Late Fees, adjustments, and credits that appear BEFORE any location section are bill-level charges
- For these, use the control account as carrier_account_number (they belong to the overall bill, not a specific location)
- charge_type for Late Fee = "NRC"

SPECTRUM-SPECIFIC:
- Account numbers are either 9-digit (enterprise) or 16-digit segmented (business)
- Hierarchy ID (if present) is the master_account
- Security Code is NOT the account number
- Phone numbers like 888-xxx-xxxx, 855-xxx-xxxx are Spectrum SUPPORT numbers — do NOT extract as customer phone_number or btn
- Spectrum invoices typically do NOT contain customer phone numbers — set phone_number to null unless a customer TN is clearly listed
