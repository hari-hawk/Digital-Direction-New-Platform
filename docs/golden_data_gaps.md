# Golden Data Gaps — Items for Client Discussion

## AT&T: Per-TN Address Mapping (Account 614 336-1586)

**Impact**: 276 rows in golden data, ~35% of AT&T eval volume

**Finding**: The golden data assigns 16 different service addresses to TNs under account 614 336-1586, but the AT&T CSR for this account contains only ONE address (6271 COSGRAY RD) with zero SLA (Station Location Address) entries. The CSR has 16 sub-accounts (FULL BILL UNDER ACCOUNT references), but no per-sub-account address information.

**Root cause**: The golden analyst used an external master location database to map individual TNs to their physical service locations. This mapping is NOT present in the AT&T CSR document.

**What works**: For CSRs that DO have SLA tables (e.g., 614 761-5500 with 10 SLA entries), our extraction correctly reads the SLA table and assigns per-TN addresses using the `/SLA NNN` references in USOC lines. This mechanism is sound and config-driven.

**What's needed from client**:
1. Confirm whether the per-TN address mapping for account 1586 came from an external system (master location DB, service inventory, etc.)
2. If so, provide the TN→address mapping reference as supplemental input to the pipeline
3. Alternatively, accept that CSR-only extraction yields the default CSR address for multi-location accounts without SLA data

**Similar pattern — Account 5500**: The 5500 CSR has SLA data and our extraction uses it. However, the golden data sometimes assigns the default address (5200 EMERALD PKWY) to TNs that have explicit SLA-specific addresses in the CSR. Need client clarification on whether the SLA-specific address or the master/default address is correct.

---

## AT&T: Inconsistent component_or_feature_name Between Accounts

**Impact**: At least 120 of 398 "wrong" component names in eval

**Finding**: The golden data uses two different naming conventions for the same USOC codes across accounts:
- Account 614 336-1586: Uses CSR USOC descriptions (e.g., CPXHX → "Standard Centrex Feature", CPXHF → "Standard Centrex Feature", CPXHE → "Ctx Icom Billing Access Area")
- Accounts 614 761-5500 and 614 718-4339: Uses invoice format with price prefix (e.g., CPXHX → "2.00 NETWORK ACCESS", CPXHF → "2.00 CENTRAL-OFFICE TERMINATION!", CPXHE → "2.00 MESSAGE UNIT PACKAGE")

**Root cause**: The analyst chose different source documents (CSR vs invoice) for component naming across accounts. Our extraction uses USOC-based descriptions from the CSR ACCOUNT SUMMARY and domain_knowledge lookup table — consistent with account 1586's golden data but not accounts 5500/4339.

**What's needed from client**:
1. Standardize component_or_feature_name format — either always use USOC descriptions or always use invoice line names
2. If invoice format is preferred, include USOCs so we can map between formats

---

## Windstream: carrier_circuit_number Format Mismatch (Account 2389882)

**Impact**: 762 "wrong" circuit numbers, affecting fuzzy accuracy

**Finding**: For the large Windstream enterprise account (2389882), the golden data uses two different circuit ID formats:
- 801 rows: customer-facing SDWAN service IDs (e.g., "2389874-SDWAN-1")
- ~200 rows: internal CLLI circuit format (e.g., "34/GEDO/908827/110/PUA /GED")

Our extraction consistently uses the CLLI format from subscription exports and invoices, matching the second format but not the first.

**Root cause**: The SDWAN service IDs ("2389874-SDWAN-1") appear to come from a service management portal or service order system, not from the invoice/subscription documents our pipeline extracts from.

**What's needed from client**:
1. Identify the source of SDWAN service IDs — if from a separate system, provide as supplemental input
2. Alternatively, standardize on CLLI format which is consistently present in billing documents

---

## How to use this document

- Each entry represents a case where golden data expectations cannot be met from document extraction alone
- Before adding: verify the data genuinely isn't in the source documents (not just a parsing gap)
- Tag with impact (row count, % of carrier eval) so we can prioritize client discussions
- After client clarification, either update the golden data or add supplemental input sources
