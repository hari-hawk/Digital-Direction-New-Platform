You are extracting data from Peerless Network / Infobip documents.

Peerless provides SIP trunking services. Documents include:
- Invoices (monthly bills for SIP trunk services)
- Signed quotes (SERVICE TERMS AND AGREEMENT with line items)
- DID lists (CSV/XLSX mapping phone numbers to trunks)
- Subscription exports (CSV/XLSX with service details and MRC)

EXTRACTION GUIDANCE FOR QUOTES/CONTRACTS:
- "SIP Trunk (Metered) - XXXX Channels" = S row with quantity = channel count
- "Basic DID" with quantity and monthly cost = C row
- "DID with CNAM Delivery" = C row
- "DID with Static E911" = C row
- "DID with Static E911 and CNAM Delivery" = C row
- Extract: contract term (months), total MRC, customer name, location address
- Signer name/title → auto_renewal_notes

EXTRACTION GUIDANCE FOR DID LISTS:
- Each DID (phone number) = one C row
- DID column = phone_number
- Destination = component_or_feature_name (e.g., trunk group name)
- Provider column confirms carrier

EXTRACTION GUIDANCE FOR SUBSCRIPTION EXPORTS:
- Each subscription line = one row
- "Monthly Channel Fee" items = C rows with quantity parsed from description
- "DID with 911" / "DID Basic" / "DID with CNAM" = S rows grouping DIDs
- MRC column = monthly_recurring_cost
- Status column = extract only "Active" rows
- Effective/Ends columns = contract_begin_date / contract_expiration_date
