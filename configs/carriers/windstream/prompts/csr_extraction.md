You are extracting data from a Windstream Customer Service Record (CSR).

Two formats exist:

SUMMARY FORMAT:
- Header: Windstream logo + "Windstream Customer Service Record" + timestamp
- ACCOUNT section: account name (e.g., "CUSTNAME-167"), account number, address, status, type
- PRODUCT SUMMARY: counts by category (DATA, EQUIPMENT, FACILITY, INTERNET, LINES, TRUNKS)
- TELEPHONE NUMBER SUMMARY: BTN marked with "(BTN)", other numbers with type markers:
  (TF) = Toll Free, (WTN) = Working Telephone Number, (NPIN) = Number Ported In

DETAILED PROVISIONING FORMAT:
- Table with columns: Bent Phone, Sent Phone, INTER-SEL-PIC, INTRA-SEL-PIC, Asoc Id
- Service items with charges: Asoc Name, Sunit Amt, Sunit Qty
- Individual feature charges (CENTREX LINK CHARGE, IBN EXCHANGE, BUSINESS HSI INTERNET, etc.)
- Totals at bottom

EXTRACTION GUIDANCE:
- BTN is explicitly marked "(BTN)" in summary format or is the primary Bent Phone
- Each phone number = one S row
- Features/services with charges = C rows under their phone number
- "INSERVICE" status = active account
- Circuit/group identifiers (e.g., 282867WYXW) → carrier_circuit_number
- Product counts from PRODUCT SUMMARY → extract as summary rows
