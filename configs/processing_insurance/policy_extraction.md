# Insurance Policy Extraction

You are extracting structured data from an insurance document (policy
declaration, endorsement, certificate of insurance, renewal notice, or
claim summary).

Return a JSON array. Each element is one *coverage line* or *insured
property* on the policy. A single policy declaration page typically yields
1–10 lines (auto policy: one per vehicle; commercial property: one per
location; life: usually one).

## Output schema

Return a JSON array of objects. For each object include any field
visible in the document — leave others null:

```
[
  {
    "policy_holder":       "string — named insured (person, business, or trust)",
    "policy_number":       "string — exact identifier as printed",
    "policy_type":         "string — 'auto' | 'home' | 'life' | 'health' | 'commercial property' | 'commercial liability' | 'umbrella' | 'other'",
    "carrier_name":        "string — insurance company name (e.g., 'State Farm', 'Allstate')",
    "effective_date":      "YYYY-MM-DD",
    "expiration_date":     "YYYY-MM-DD",
    "premium_amount":      "number — monetary value, no currency symbol",
    "premium_frequency":   "string — 'monthly' | 'quarterly' | 'semi-annual' | 'annual' | 'single payment'",
    "coverage_amount":     "number — face amount / coverage limit for THIS line",
    "deductible":          "number — applicable deductible for THIS line",
    "coverage_type":       "string — specific coverage label as printed (e.g., 'Comprehensive', 'Collision', 'Dwelling A', 'Term Life')",
    "insured_property":    "string — vehicle VIN / property address / insured name as printed",
    "agent_name":          "string — listed insurance agent or broker",
    "agent_phone":         "string — agent's phone, digits-and-dashes format",
    "billing_address_1":   "string — billing street address",
    "city":                "string",
    "state":               "string — 2-letter US code where applicable",
    "zip":                 "string",
    "country":             "string — 3-letter code, default 'USA'",
    "currency":            "string — ISO-4217, default 'USD'",
    "renewal_terms":       "string — short summary if the doc describes renewal behavior",
    "auto_renew":          "string — 'Yes' | 'No' | null if not stated",
    "notes":               "string — anything the analyst should know (riders, exclusions, claim status, etc.)"
  }
]
```

## Rules

1. Return **only** the JSON array — no prose, no commentary, no
   markdown fences.
2. One row per coverage line / insured property. Do NOT collapse multiple
   vehicles on one auto policy into a single row.
3. Money values are numeric — `1500.00` not `"$1,500.00"`.
4. Dates are ISO-8601 `YYYY-MM-DD`. If the document only shows a year,
   use `YYYY-01-01` and note in `notes`.
5. When a field is unambiguously absent (the document doesn't list it),
   set to `null`. Never hallucinate.
6. `policy_type` is your inference based on what's covered. If the doc
   doesn't say, look at the coverages listed: vehicles → auto, dwelling
   → home, term/whole/UL → life, etc.
7. If multiple insured parties exist (e.g., joint policy), put primary in
   `policy_holder` and additionals in `notes`.

## Example 1 — Auto policy declaration (2 vehicles)

Input (excerpt from the declarations page):
```
NAMED INSURED:    JOHN P SMITH
POLICY NUMBER:    AP-44719-002
EFFECTIVE:        2026-03-15
EXPIRATION:       2027-03-15
PREMIUM:          $1,840.00 / 6-month term
CARRIER:          PROGRESSIVE
DEDUCTIBLE (COMP/COLL): $500 / $1,000

VEHICLE 1: 2021 HONDA CIVIC  VIN 19XFC2F58ME201234
  Comprehensive: $40,000
  Collision: $40,000
VEHICLE 2: 2018 TOYOTA TACOMA  VIN 5TFAX5GN8JX112233
  Comprehensive: $25,000
  Collision: $25,000
```

Output:
```json
[
  {"policy_holder": "JOHN P SMITH", "policy_number": "AP-44719-002", "policy_type": "auto", "carrier_name": "Progressive", "effective_date": "2026-03-15", "expiration_date": "2027-03-15", "premium_amount": 1840.00, "premium_frequency": "semi-annual", "coverage_amount": 40000, "deductible": 500, "coverage_type": "Comprehensive + Collision", "insured_property": "2021 HONDA CIVIC VIN 19XFC2F58ME201234", "currency": "USD", "auto_renew": null, "notes": null},
  {"policy_holder": "JOHN P SMITH", "policy_number": "AP-44719-002", "policy_type": "auto", "carrier_name": "Progressive", "effective_date": "2026-03-15", "expiration_date": "2027-03-15", "premium_amount": 1840.00, "premium_frequency": "semi-annual", "coverage_amount": 25000, "deductible": 500, "coverage_type": "Comprehensive + Collision", "insured_property": "2018 TOYOTA TACOMA VIN 5TFAX5GN8JX112233", "currency": "USD", "auto_renew": null, "notes": null}
]
```

## Example 2 — Homeowners declaration

Input:
```
ALLSTATE INSURANCE
NAMED INSURED:    Susan Lee
POLICY NUMBER:    HO-882144-7
COVERAGE PERIOD:  1/1/2026 – 1/1/2027
PROPERTY:         42 Oak Street, Lindon, UT 84042

Coverage A (Dwelling):   $385,000
Coverage B (Other Struct): $38,500
Coverage C (Personal Prop): $192,500
Coverage D (Loss of Use):  $77,000
Deductible:                $2,500
Annual Premium:            $1,210.00
Agent: Marco Diaz   (801) 555-7720
```

Output:
```json
[
  {"policy_holder": "Susan Lee", "policy_number": "HO-882144-7", "policy_type": "home", "carrier_name": "Allstate", "effective_date": "2026-01-01", "expiration_date": "2027-01-01", "premium_amount": 1210.00, "premium_frequency": "annual", "coverage_amount": 385000, "deductible": 2500, "coverage_type": "Dwelling A", "insured_property": "42 Oak Street, Lindon, UT 84042", "agent_name": "Marco Diaz", "agent_phone": "801-555-7720", "billing_address_1": "42 Oak Street", "city": "Lindon", "state": "UT", "zip": "84042", "country": "USA", "currency": "USD", "auto_renew": null, "notes": "Coverage B $38,500; C $192,500; D $77,000"}
]
```

## Example 3 — Term life

Input:
```
NEW YORK LIFE  -  TERM LIFE POLICY
INSURED: Maria K. Chen   POLICY #: TL-9912-AAQ
TERM: 20 years  EFF: 04/01/2025  EXP: 04/01/2045
FACE AMOUNT: $750,000
ANNUAL PREMIUM: $612.40
RIDERS: Accelerated Death Benefit; Waiver of Premium
RENEWAL: Not automatic — policy expires at end of term
```

Output:
```json
[
  {"policy_holder": "Maria K. Chen", "policy_number": "TL-9912-AAQ", "policy_type": "life", "carrier_name": "New York Life", "effective_date": "2025-04-01", "expiration_date": "2045-04-01", "premium_amount": 612.40, "premium_frequency": "annual", "coverage_amount": 750000, "deductible": null, "coverage_type": "Term Life — 20yr", "insured_property": "Maria K. Chen", "currency": "USD", "auto_renew": "No", "renewal_terms": "Not automatic — policy expires at end of term", "notes": "Riders: Accelerated Death Benefit; Waiver of Premium"}
]
```

Now extract the JSON array from this document:
