# Per-File Diagnostic — Template

> Use this template to return a short, scannable note for each file a client sends for diagnosis.
> Keep each block to the same structure so a batch of 5–10 files can be skimmed quickly.
> Replace `[...]` placeholders; delete any line that doesn't apply to a given file.

---

## Cover note (send once per batch)

Hi MH,

We ran the [N] files through the platform's diagnostic view. A per-file note is below for each one. In short:

- **[filename 1]** — [one-line outcome: "new filename variant; config extended; re-run produced meaningful output."]
- **[filename 2]** — [one-line outcome]
- **[filename 3]** — [one-line outcome]

Configuration changes are permanent — future invoices matching the same variant will flow through without further attention. Happy to walk through any of these live if helpful.

---

## Per-file block (repeat for each file)

### File: `[filename]`

**Carrier (expected):** [AT&T / Windstream / Spectrum / Peerless / other]
**Provided by:** [customer name] &nbsp;·&nbsp; **Run date:** [YYYY-MM-DD]

**What the platform detected**
| | |
|---|---|
| Classification stage reached | [A — filename pattern / B — first-page signals / C — LLM fallback] |
| Carrier detected | [carrier] |
| Document type | [invoice / CSR / contract / service report / DID list] |
| Format variant | [variant name, or "unconfigured variant"] |
| Account number | [extracted / not extracted from filename — reason] |
| Overall classification confidence | [high / medium / low] |

**What differed from the configured variant**

- [Specific, concrete observation — e.g., *"Filename pattern `ATT_Invoice_<date>_<n>.pdf` didn't match the configured pattern `ATT - <account>.pdf`; account number couldn't be pulled from the filename, fell back to content scan."*]
- [Second observation if relevant — e.g., *"First-page header reads `AT&T Business Fiber Invoice` rather than `AT&T Business Invoice`; alias was missing from the first-page signals list."*]
- [Third if relevant — e.g., *"Sub-account section marker is `Charges for Service Location:` instead of `ACTIVITY FOR ACCOUNT`; chunking boundary didn't fire, so all sub-accounts were extracted as one blob."*]

**What was done**

- [ ] Extended the carrier configuration — [specific change, e.g., *"added filename pattern `ATT_Invoice_\d{8}_\d+\.pdf` and first-page alias `AT&T Business Fiber` to `configs/carriers/att/carrier.yaml`"*]
- [ ] Added a new format variant — [e.g., *"created `configs/carriers/att/formats/business_fiber.yaml` with the new section-marker chunking rule"*]
- [ ] No change required — [e.g., *"transient model timeout on first run; second run completed cleanly"*]
- [ ] Needs additional documents — [e.g., *"invoice references sub-accounts 580…362 and 580…498 that aren't in the provided bundle; once the corresponding CSRs are added, rows will link"*]

**Re-run result**

| | |
|---|---|
| Status | [meaningful output produced / partial output / needs further input] |
| Rows extracted | [N] |
| Confidence distribution | High [n%] &nbsp;·&nbsp; Medium [n%] &nbsp;·&nbsp; Low [n%] |
| Compliance flags raised | [none / N flags — list types: rate_mismatch, expired_contract, mtm_inconsistency, no_contract] |
| Reviewable output | [link to Results view, or *"attached workbook, sheet `[account#]`"*] |

---

## Example — filled block (reference)

### File: `MH Test 12 041426 -ATT - 5802861126660.pdf`

**Carrier (expected):** AT&T
**Provided by:** Digital Direction &nbsp;·&nbsp; **Run date:** 2026-04-22

**What the platform detected**
| | |
|---|---|
| Classification stage reached | B — first-page signals |
| Carrier detected | AT&T |
| Document type | Invoice |
| Format variant | `att/invoice_standard` |
| Account number | Extracted from first-page text (filename pattern did not match) |
| Overall classification confidence | High |

**What differed from the configured variant**

- Filename uses the convention `MH Test <n> <MMDDYY> -<carrier> - <account>.pdf`, which isn't part of the configured AT&T filename patterns. The classifier reached stage B (first-page signals) before recognising the carrier and account number.
- First-page header matched the `AT&T Business Invoice` signal cleanly; no new alias needed.
- Section markers for sub-accounts (`Charges for Service Location:`) matched the configured chunking rule.

**What was done**

- [x] Extended the AT&T filename patterns in `configs/carriers/att/carrier.yaml` to accept the `MH Test` naming convention for internal test invoices.
- [ ] No format variant changes required.
- [ ] No additional documents required.

**Re-run result**

| | |
|---|---|
| Status | Meaningful output produced |
| Rows extracted | [N] |
| Confidence distribution | High [n]% &nbsp;·&nbsp; Medium [n]% &nbsp;·&nbsp; Low [n]% |
| Compliance flags raised | None (no contract provided with this invoice) |
| Reviewable output | Results view — Upload `[upload_id]`, filter Account `5802861126660` |

---

## Notes for internal use

- **Be specific where it matters, generic where it doesn't.** Naming the filename pattern, first-page alias, or section marker that differed is the evidence the client is looking for. Don't paraphrase these into general terms — the specificity is the proof.
- **Frame the outcome as "configuration extended," not "bug fixed."** The platform didn't fail; it encountered a variant it hadn't seen, and the variant is now configured.
- **If a file genuinely needs more input** (missing CSR, missing contract, OCR-only scan), say so plainly. Don't stretch "configuration extension" to cover scope gaps.
- **Keep confidence numbers honest.** If a re-run still has medium/low on fuzzy fields, report it that way — the per-field confidence badges on the results page will show the same numbers anyway.
