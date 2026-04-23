You are extracting data from a Windstream invoice.

Windstream invoices have two formats:

ENTERPRISE FORMAT (large multi-location bills):
- LOCATION SUMMARY table: lists all sub-accounts with totals
- Per sub-account ACCOUNT ACTIVITY sections with:
  - MONTHLY CHARGES table: Period, Description, Quantity, Cost Per Unit, Amount
  - OTHER CHARGES AND CREDITS
  - SURCHARGES AND TAXES
  - USAGE CHARGES: Calls, Minutes, Amount
  - ITEMIZED CALL DETAIL (some accounts)
- Product groups: Dynamic IP, SD-WAN VMware, UCaaS Standard, Internet Service, LAN Services

KINETIC FORMAT (small single-location bills):
- Account summary with telephone number, invoice date, billing period
- Service line items: CENTREX LINK CHARGE, IBN EXCHANGE ACCESS, BUSINESS INTERNET, etc.
- Surcharges and taxes section

EXTRACTION GUIDANCE:
- "ACTIVITY FOR ACCOUNT - XXXXXX LOCATION-NAME - (PIN: XXXX): ADDRESS" header contains:
  - sub_account_number_1 = the account number after the dash
  - carrier_account_number = the MASTER account (from page header, appears on every page)
  - service_address from the address after the colon
  - billing_name = location name (e.g., "CUSTNAME-232")
- Each product group (Dynamic IP, SD-WAN VMware, UCaaS Standard) = S row with the group total
- Each line item within a product group = C row
- "Included" items with no dollar amount = C row with monthly_recurring_cost = 0
- Circuit IDs appear in parentheses: "(Circuit ID: XX/XXXX/XXXXXX/XXX/XXX)"
  - Extract the FULL circuit ID string into carrier_circuit_number
- Administrative Services Fee (ASF) = C row under "Other Charges"
- Surcharges (USF, E911, Regulatory Assessment) = C rows with charge_type "Surcharge"
- Taxes (Federal, State, Local, Sales Tax) = C rows with charge_type "Tax"
- IMPORTANT: Do NOT tag surcharges or taxes as "MRC". MRC is ONLY for monthly recurring SERVICE charges (Internet, Voice, SD-WAN, UCaaS, etc.)
- Usage charges with Calls/Minutes/Amount = C rows with charge_type "Usage"

FIELD-SPECIFIC GUIDANCE:
- service_type: Map Windstream product groups to standard categories:
  - Dynamic IP, Internet Service, Business Internet → "Internet"
  - SD-WAN VMware, SD-WAN → "SDWAN"
  - UCaaS Standard, UCaaS → "UCaaS"
  - CENTREX, IBN → "Voice"
  - Ethernet Access → "Ethernet"
  - MPLS VPN → "MPLS"
  - LAN Services → "LAN"
  - Toll-Free → "Toll-Free"
- component_or_feature_name: The SPECIFIC service description from the line item, including speed/tier.
  Examples: "Ethernet Access - 500 Mb", "SD-WAN Service Charge 10 Mbps", "IP Addresses Block of 4 Charge"
  Do NOT put generic labels like "Monthly Charges" or surcharge names here.
- cost_per_unit: The per-unit cost when quantity > 1. If only a total amount is shown, leave blank.
- monthly_recurring_cost: The total MRC for THIS line item (quantity × cost_per_unit if both shown).

WINDSTREAM-SPECIFIC:
- Master account number appears in page header on every page — use this for carrier_account_number
- Sub-account numbers are in the ACTIVITY FOR ACCOUNT headers — use for sub_account_number_1
- PIN codes are NOT account numbers
- "Included" means $0 cost but service is provisioned (still extract)
