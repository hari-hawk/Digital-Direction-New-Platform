"""Pattern detectors — the deterministic analytics layer powering the
"brain" surface and the chat assistant.

Five detectors implement Matt's prioritized insights (Apr-30):
  1. recurring_vendors            — carriers across a client's portfolio
  2. pricing_anomalies            — USOC rows >Nσ from the mean
  3. contract_expirations         — clusters of contracts ending same month
  4. multi_carrier_same_account   — same phone/account on multiple carriers
  5. m2m_needing_contracts        — services flagged M2M with no contract term

Design principles:
- Pure functions over plain dicts. No LLM. Deterministic.
- Each returns a `Finding` list with consistent shape so the frontend renders
  cards uniformly and the chat layer can cite specific rows.
- Severity is a coarse {info, warning, error}. Pattern dashboards filter by it.
- Inputs are the same JSONB shape returned by /api/uploads/{id}/results so we
  can run detectors against either a single project or the union of all
  projects for a client without reshaping data.

Adding a new detector: define the function, append to `ALL_DETECTORS`, write
findings the same way. The chat context assembler picks up everything in
that list automatically.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ── Finding shape ────────────────────────────────────────────────────────────
#
# Kept as a plain dict (not Pydantic) so detectors stay zero-dep and findings
# serialize directly to JSON. The frontend treats `id` as a stable key, `kind`
# as the detector name (for icon/color routing), and `evidence_row_ids` as the
# hook for "show me which rows this is about."
def _finding(
    *,
    kind: str,
    severity: str,
    title: str,
    detail: str,
    evidence_row_ids: list[str] | None = None,
    metric: dict | None = None,
) -> dict:
    return {
        "kind": kind,
        "severity": severity,  # "info" | "warning" | "error"
        "title": title,
        "detail": detail,
        "evidence_row_ids": evidence_row_ids or [],
        "metric": metric or {},
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

_DIGITS = re.compile(r"\D")


def _norm_phone(s: Any) -> str:
    if s is None:
        return ""
    return _DIGITS.sub("", str(s))


def _norm_acct(s: Any) -> str:
    return _norm_phone(s)


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        # Strip currency symbols + commas
        try:
            return float(re.sub(r"[^\d.\-]", "", str(v)))
        except (TypeError, ValueError):
            return None


def _to_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_yes(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower() in ("yes", "y", "true")


# ── Detector 1: recurring vendors ────────────────────────────────────────────


def recurring_vendors(uploads: list[dict]) -> list[dict]:
    """For a client portfolio, count distinct carriers and surface ones
    appearing on multiple projects. The signal Matt cares about: "this customer
    has a Comcast project AND a Verizon project — they're a multi-carrier shop,
    so cross-doc merge needs to bridge."

    Input: list of upload summaries (dict). Each upload has
        - client_name (or empty)
        - computed_carriers (list[str]) or carriers extracted during run
        - upload_id, project_name, total_rows

    Returns one finding per carrier seen on ≥2 projects within a single client,
    plus an info finding listing all carriers if there are 3+.
    """
    findings: list[dict] = []
    by_client: dict[str, list[dict]] = defaultdict(list)
    for u in uploads:
        cn = (u.get("client_name") or "").strip().lower()
        if cn:
            by_client[cn].append(u)

    for client, ups in by_client.items():
        carrier_to_projects: dict[str, list[dict]] = defaultdict(list)
        for u in ups:
            for c in u.get("computed_carriers") or []:
                if c:
                    carrier_to_projects[c].append(u)

        if not carrier_to_projects:
            continue

        # Carriers appearing on multiple projects — strong signal
        for carrier, projects in carrier_to_projects.items():
            if len(projects) >= 2:
                findings.append(_finding(
                    kind="recurring_vendor",
                    severity="info",
                    title=f"{carrier} appears on {len(projects)} {client.title()} projects",
                    detail=(
                        f"Customer {client.title()} has {len(projects)} separate "
                        f"projects involving {carrier}. Consider consolidating into "
                        f"a single client-wide inventory view via Append rather "
                        f"than re-uploading."
                    ),
                    metric={
                        "client": client,
                        "carrier": carrier,
                        "project_count": len(projects),
                        "project_ids": [p.get("upload_id") for p in projects],
                    },
                ))

        # Multi-carrier customer summary
        if len(carrier_to_projects) >= 3:
            carriers_sorted = sorted(carrier_to_projects.keys())
            findings.append(_finding(
                kind="recurring_vendor",
                severity="info",
                title=f"{client.title()} is a multi-carrier customer ({len(carriers_sorted)} carriers)",
                detail=(
                    f"Carriers: {', '.join(carriers_sorted)}. Cross-doc merge "
                    f"across these accounts depends on consistent customer-level "
                    f"keys (account aliases, BTNs)."
                ),
                metric={"client": client, "carriers": carriers_sorted},
            ))
    return findings


# ── Detector 2: pricing anomalies ────────────────────────────────────────────


def pricing_anomalies(
    rows: list[dict],
    *,
    min_group_size: int = 4,
    z_threshold: float = 2.0,
) -> list[dict]:
    """Group rows by USOC code (or service_type as fallback) and flag rows
    whose monthly_recurring_cost (or rate) is more than `z_threshold` standard
    deviations above the group's mean. Skip groups smaller than
    `min_group_size` — small samples produce noisy z-scores.

    The customer-facing claim this enables: "BTN 614-555-0123 pays $X for USOC
    AB1, which is 3× the average $Y across N other lines on this account."
    """
    findings: list[dict] = []

    # Group rows by USOC, fall back to service_type when USOC is blank
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        usoc = (r.get("usoc") or "").strip()
        st = (r.get("service_type") or "").strip()
        key = usoc or (f"svc:{st}" if st else None)
        if not key:
            continue
        rate = _to_float(r.get("monthly_recurring_cost") or r.get("rate"))
        if rate is None or rate <= 0:
            continue
        groups[key].append({**r, "_rate": rate})

    for key, members in groups.items():
        if len(members) < min_group_size:
            continue
        rates = [m["_rate"] for m in members]
        mu = statistics.mean(rates)
        sd = statistics.pstdev(rates)
        if sd == 0:
            continue  # all rates identical — nothing anomalous
        for m in members:
            z = (m["_rate"] - mu) / sd
            if z >= z_threshold:
                multiple = m["_rate"] / mu if mu > 0 else None
                phone = m.get("phone_number") or ""
                findings.append(_finding(
                    kind="pricing_anomaly",
                    severity="warning",
                    title=(
                        f"BTN {phone or '(no phone)'} pays ${m['_rate']:.2f} "
                        f"for {key} — {f'{multiple:.1f}× ' if multiple else ''}group avg ${mu:.2f}"
                    ),
                    detail=(
                        f"Across {len(members)} rows with USOC/service '{key}', "
                        f"the mean monthly cost is ${mu:.2f} (σ=${sd:.2f}). "
                        f"This row sits at z={z:.1f}, indicating a likely "
                        f"pricing error or premium service tier worth verifying."
                    ),
                    evidence_row_ids=[m.get("id")] if m.get("id") else [],
                    metric={
                        "key": key,
                        "row_rate": round(m["_rate"], 2),
                        "group_mean": round(mu, 2),
                        "group_stdev": round(sd, 2),
                        "z_score": round(z, 2),
                        "multiple": round(multiple, 2) if multiple else None,
                        "group_size": len(members),
                    },
                ))
    return findings


# ── Detector 3: contract expiration clusters ─────────────────────────────────


def contract_expirations(
    rows: list[dict],
    *,
    cluster_window_days: int = 30,
    min_cluster_size: int = 3,
) -> list[dict]:
    """Find groups of services whose contract_expiration_date falls within the
    same N-day window. A cluster is a billing-renegotiation moment.

    Returns one finding per cluster. Severity escalates to 'warning' when the
    cluster is in the next 90 days, 'error' when it's already past due.
    """
    findings: list[dict] = []
    today = date.today()

    # Build (date, row) pairs
    dated: list[tuple[date, dict]] = []
    for r in rows:
        d = _to_date(r.get("contract_expiration_date"))
        if d:
            dated.append((d, r))
    if len(dated) < min_cluster_size:
        return findings
    dated.sort(key=lambda t: t[0])

    # Sweep — group dates within `cluster_window_days` of the first member
    i = 0
    while i < len(dated):
        cluster = [dated[i]]
        j = i + 1
        while j < len(dated):
            delta = (dated[j][0] - cluster[0][0]).days
            if delta <= cluster_window_days:
                cluster.append(dated[j])
                j += 1
            else:
                break
        if len(cluster) >= min_cluster_size:
            first = cluster[0][0]
            last = cluster[-1][0]
            days_until = (first - today).days
            if days_until < 0:
                sev = "error"
                horizon = f"{abs(days_until)}d ago"
            elif days_until <= 90:
                sev = "warning"
                horizon = f"in {days_until}d"
            else:
                sev = "info"
                horizon = f"in {days_until}d"
            findings.append(_finding(
                kind="contract_cluster",
                severity=sev,
                title=f"{len(cluster)} contracts expiring {first.isoformat()} → {last.isoformat()} ({horizon})",
                detail=(
                    f"{len(cluster)} services have contract expiration dates "
                    f"clustered between {first} and {last}. This is a "
                    f"renegotiation pressure point — bundling these services "
                    f"into a single agreement may yield consolidation savings."
                ),
                evidence_row_ids=[r.get("id") for _d, r in cluster if r.get("id")],
                metric={
                    "first_expiration": first.isoformat(),
                    "last_expiration": last.isoformat(),
                    "cluster_size": len(cluster),
                    "days_until_first": days_until,
                },
            ))
        i = j if j > i + 1 else i + 1
    return findings


# ── Detector 4: multi-carrier same account ───────────────────────────────────


def multi_carrier_same_account(rows: list[dict]) -> list[dict]:
    """Find phone numbers (or account numbers) that appear on more than one
    carrier. Usually means either (a) a recent migration the analyst hasn't
    noted, or (b) a billing error where the customer is paying two carriers
    for the same service.
    """
    findings: list[dict] = []

    # Group by normalized phone — exact account match across carriers is rarer
    by_phone: dict[str, set[str]] = defaultdict(set)
    by_phone_rows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        ph = _norm_phone(r.get("phone_number"))
        carrier = (r.get("carrier_name") or r.get("carrier") or "").strip()
        if len(ph) >= 7 and carrier:
            by_phone[ph].add(carrier)
            by_phone_rows[ph].append(r)

    for ph, carriers in by_phone.items():
        if len(carriers) < 2:
            continue
        evidence = by_phone_rows[ph]
        formatted = f"{ph[:3]}-{ph[3:6]}-{ph[6:10]}" if len(ph) == 10 else ph
        findings.append(_finding(
            kind="multi_carrier_account",
            severity="warning",
            title=f"{formatted} appears on {', '.join(sorted(carriers))}",
            detail=(
                f"This phone number appears in {len(evidence)} rows across "
                f"{len(carriers)} different carriers. Either a migration in "
                f"flight (single source of truth needed) or duplicate billing."
            ),
            evidence_row_ids=[r.get("id") for r in evidence if r.get("id")],
            metric={
                "phone": formatted,
                "carriers": sorted(carriers),
                "row_count": len(evidence),
            },
        ))
    return findings


# ── Detector 5: M2M services that should have contracts ──────────────────────


def m2m_needing_contracts(rows: list[dict]) -> list[dict]:
    """Surface rows flagged month-to-month with no contract_term_months — the
    "money on the table" set, since M2M rates typically run 15–30% above
    contracted rates. Severity escalates by total monthly spend at risk.
    """
    findings: list[dict] = []
    candidates: list[dict] = []
    for r in rows:
        mtm = _is_yes(r.get("currently_month_to_month"))
        term = r.get("contract_term_months")
        has_term = term not in (None, "", 0)
        if mtm and not has_term:
            candidates.append(r)
    if not candidates:
        return findings

    total_mrc = sum(_to_float(r.get("monthly_recurring_cost") or r.get("rate")) or 0 for r in candidates)
    by_carrier: Counter = Counter()
    for r in candidates:
        c = (r.get("carrier_name") or r.get("carrier") or "Unknown").strip()
        by_carrier[c] += 1

    sev = "info"
    if total_mrc >= 5000:
        sev = "error"
    elif total_mrc >= 1000:
        sev = "warning"
    findings.append(_finding(
        kind="m2m_no_contract",
        severity=sev,
        title=(
            f"{len(candidates)} services on month-to-month with no contract — "
            f"≈${total_mrc:,.0f}/mo at risk"
        ),
        detail=(
            f"Carriers with M2M exposure: "
            f"{', '.join(f'{c} ({n})' for c, n in by_carrier.most_common())}. "
            f"Migrating these to term contracts typically reduces MRC by "
            f"15–30%, recovering ${total_mrc * 0.20:,.0f}/mo (mid-band estimate)."
        ),
        evidence_row_ids=[r.get("id") for r in candidates if r.get("id")],
        metric={
            "row_count": len(candidates),
            "total_mrc": round(total_mrc, 2),
            "by_carrier": dict(by_carrier),
            "estimated_savings_mid": round(total_mrc * 0.20, 2),
        },
    ))
    return findings


# ── Aggregator ───────────────────────────────────────────────────────────────


ALL_ROW_DETECTORS = (
    pricing_anomalies,
    contract_expirations,
    multi_carrier_same_account,
    m2m_needing_contracts,
)


def detect_all(rows: list[dict], uploads: list[dict] | None = None) -> list[dict]:
    """Run every detector and return the combined finding list. `uploads`
    only matters for the cross-project detectors (recurring_vendors); pass
    `None` for project-scoped runs."""
    findings: list[dict] = []
    for fn in ALL_ROW_DETECTORS:
        try:
            findings.extend(fn(rows))
        except Exception as e:
            logger.exception(f"detector {fn.__name__} failed: {e}")
    if uploads:
        try:
            findings.extend(recurring_vendors(uploads))
        except Exception as e:
            logger.exception(f"detector recurring_vendors failed: {e}")
    # Stable order so the UI doesn't churn — by severity then title
    sev_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (sev_order.get(f.get("severity"), 9), f.get("title", "")))
    return findings


def summarize(findings: list[dict]) -> dict:
    """Compact summary the chat assembler can drop into a system prompt."""
    by_kind: Counter = Counter()
    by_severity: Counter = Counter()
    for f in findings:
        by_kind[f["kind"]] += 1
        by_severity[f["severity"]] += 1
    return {
        "total": len(findings),
        "by_kind": dict(by_kind),
        "by_severity": dict(by_severity),
    }
