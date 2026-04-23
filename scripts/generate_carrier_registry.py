"""Generate lightweight carrier folders for the Digital Direction carrier registry.

The 4 existing carriers (AT&T, Windstream, Spectrum, Peerless) stay untouched —
they have tuned prompts + domain knowledge. Every other carrier gets a minimal
`configs/carriers/{slug}/carrier.yaml` with just `name` + `aliases` so the
classifier's content-scan can match them by alias. Extraction falls back to the
generic prompt at `configs/processing/invoice_extraction.md`.

Idempotent: existing carriers are skipped, not overwritten.

Run:
    python scripts/generate_carrier_registry.py
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Carrier lists ─────────────────────────────────────────────────────────

# Digital Direction's primary carrier inventory. These are the carriers the
# platform should always recognize from uploaded documents.
DD_MAJOR_CARRIERS = [
    "Comcast", "GTT", "TPx Communications", "Daisy Communications", "AT&T",
    "Telmex", "Lumen", "Telstra", "Verizon", "Fusion Connect", "Fusion LLC",
    "Surf Internet", "FIO Networks", "Nitel", "ACC Business", "Spectrum",
    "Consolidated Communications", "Frontier", "HongKong Broadband",
    "HongKong Telecom", "Singtel", "China Telecom", "China Mobile", "Cogent",
    "Arelion", "Baltneta", "Globe Business", "SaskTel", "LG U+", "PacketFabric",
    "Plusnet", "SK Broadband", "VNPT", "Eazit", "Chunghwa Telecom",
    "China Unicom", "DOKOM89", "China Mobile Communications",
    "Momentum Telecom Inc", "Nextiva", "WVT Fiber", "Windstream",
]

# Additional carriers from outside sources — may or may not appear in uploaded
# documents, but we want the platform to never label them "Unknown".
ADDITIONAL_CARRIERS = [
    "NTT Communications", "Tata Communications", "Orange Business Services",
    "BT Global Services", "Deutsche Telekom Global Carrier",
    "Telecom Italia Sparkle", "Zayo Group", "Colt Technology Services",
    "Cox Communications", "Altice USA", "CenturyLink", "Segra", "Lightpath",
    "Bluebird Network", "Unite Private Networks", "Vodafone Business",
    "Telefonica", "Swisscom", "Proximus", "Elisa Oyj", "Fastweb",
    "Hurricane Electric", "Equinix Fabric", "Megaport",
]


# ── Hand-curated aliases for carriers where auto-generation would miss common
# variants. Everything else gets auto-generated from the display name. ────

MANUAL_ALIASES: dict[str, list[str]] = {
    "AT&T": [],  # already exists — left untouched
    "Spectrum": [],  # already exists
    "Windstream": [],  # already exists
    "Comcast": ["Comcast Business", "Xfinity"],
    "GTT": ["GTT Communications", "global telecom and technology"],
    "TPx Communications": ["TPx", "TelePacific"],
    "Daisy Communications": ["Daisy"],
    "Lumen": ["Lumen Technologies"],  # CenturyLink is its own entry (historical bills)
    "Verizon": ["Verizon Business", "Verizon Wireless", "VZ"],
    "Fusion Connect": ["Fusion Connect Inc", "fusion-connect"],
    "Fusion LLC": ["Fusion LLC", "Fusion Cloud"],
    "Surf Internet": ["Surf"],
    "FIO Networks": ["FIO"],
    "Nitel": ["Network Innovations Telecom"],
    "ACC Business": ["ACC"],
    "Consolidated Communications": ["Consolidated", "ConsolidatedComm"],
    "Frontier": ["Frontier Communications", "FTR"],
    "HongKong Broadband": ["HKBN", "Hong Kong Broadband"],
    "HongKong Telecom": ["HKT", "Hong Kong Telecom", "PCCW"],
    "Singtel": ["Singapore Telecommunications", "SingTel"],
    "China Telecom": ["CT", "CHINATELECOM"],
    "China Mobile": ["CMCC", "CHINAMOBILE"],
    "Cogent": ["Cogent Communications"],
    "Arelion": ["Telia Carrier"],  # Telia Carrier rebranded to Arelion
    "Baltneta": ["BaltNeta Communications"],
    "Globe Business": ["Globe Telecom", "Globe"],
    "SaskTel": ["Saskatchewan Telecommunications"],
    "LG U+": ["LGU+", "LG Uplus"],
    "PacketFabric": ["Packet Fabric"],
    "Plusnet": ["Plusnet plc"],
    "SK Broadband": ["SKB", "SK Telecom"],
    "VNPT": ["Vietnam Posts and Telecommunications"],
    "Eazit": [],
    "Chunghwa Telecom": ["CHT"],
    "China Unicom": ["CU", "ChinaUnicom"],
    "DOKOM89": ["DOKOM"],
    "China Mobile Communications": ["CMCC", "China Mobile Corporation"],
    "Momentum Telecom Inc": ["Momentum Telecom", "Momentum"],
    "Nextiva": [],
    "WVT Fiber": ["WVT", "Warwick Valley Telephone"],
    "NTT Communications": ["NTT Com", "NTT", "NTT Ltd"],
    "Tata Communications": ["Tata Comm", "Tata"],
    "Orange Business Services": ["Orange Business", "OBS", "Orange"],
    "BT Global Services": ["BT", "British Telecom", "BT Global"],
    "Deutsche Telekom Global Carrier": ["Deutsche Telekom", "DT Global", "T-Mobile"],
    "Telecom Italia Sparkle": ["TI Sparkle", "Sparkle"],
    "Zayo Group": ["Zayo"],
    "Colt Technology Services": ["Colt", "Colt Technology"],
    "Cox Communications": ["Cox", "Cox Business"],
    "Altice USA": ["Altice", "Optimum", "Suddenlink"],
    "CenturyLink": ["CenturyLink Business"],  # historical brand; Lumen is separate entry
    "Segra": ["Segra Communications"],
    "Lightpath": ["Lightpath Fiber"],
    "Bluebird Network": ["Bluebird"],
    "Unite Private Networks": ["UPN", "Unite"],
    "Vodafone Business": ["Vodafone", "VF"],
    "Telefonica": ["Telefónica", "Movistar"],
    "Swisscom": ["Swisscom AG"],
    "Proximus": ["Proximus Group"],
    "Elisa Oyj": ["Elisa"],
    "Fastweb": ["Fastweb S.p.A."],
    "Hurricane Electric": ["HE", "he.net"],
    "Equinix Fabric": ["Equinix", "EquinixFabric"],
    "Megaport": ["Megaport Pty"],
}


def slugify(name: str) -> str:
    """Turn a carrier name into a safe folder slug.

    Examples:
      "AT&T"                        → "att"
      "Orange Business Services"    → "orange_business_services"
      "LG U+"                       → "lg_uplus"
      "Elisa Oyj"                   → "elisa_oyj"
    """
    s = name.lower()
    s = s.replace("&", " and ")
    s = s.replace("+", " plus ")
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s


def default_aliases(name: str) -> list[str]:
    """Auto-generate common variants: original, lower, no-punct, joined."""
    variants = set()
    variants.add(name)
    variants.add(name.lower())
    no_punct = re.sub(r"[^a-zA-Z0-9\s]", "", name)
    variants.add(no_punct)
    variants.add(no_punct.replace(" ", ""))
    # Remove duplicates and empties, preserve the display name first
    ordered = [name]
    for v in sorted(variants):
        if v and v != name and v not in ordered:
            ordered.append(v)
    return ordered


def carrier_yaml(name: str) -> str:
    """Build a minimal carrier.yaml body for a registry entry."""
    aliases = default_aliases(name)
    for extra in MANUAL_ALIASES.get(name, []):
        if extra not in aliases:
            aliases.append(extra)
    lines = [
        f'name: "{name}"',
        "aliases:",
    ]
    for a in aliases:
        # Quote values that contain ':' or other YAML-sensitive chars
        if any(ch in a for ch in ":&'\"#"):
            lines.append(f'  - "{a}"')
        else:
            lines.append(f"  - {a}")
    lines.extend([
        "",
        "# No filename_patterns — classifier falls back to content-based alias match.",
        "# No account_number_patterns — extractor uses format-agnostic regex.",
        "# No prompts directory — extraction uses the generic invoice prompt at",
        "# configs/processing/invoice_extraction.md (auto-detects carrier from document).",
        "",
    ])
    return "\n".join(lines)


def existing_carrier_names(carriers_root: Path) -> dict[str, str]:
    """Map existing carrier display names → slugs by reading every carrier.yaml.

    Prevents generating a duplicate folder when a carrier already exists under
    a different slug (e.g., "AT&T" stored as `att/` but slugify → `at_and_t`).
    """
    found: dict[str, str] = {}
    for folder in carriers_root.iterdir():
        if not folder.is_dir():
            continue
        yaml_path = folder / "carrier.yaml"
        if not yaml_path.exists():
            continue
        for line in yaml_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"').strip("'")
                if name:
                    found[name.lower()] = folder.name
                break
    return found


def main():
    carriers_root = Path(__file__).parent.parent / "configs" / "carriers"
    all_names = DD_MAJOR_CARRIERS + ADDITIONAL_CARRIERS
    by_name = existing_carrier_names(carriers_root)

    created = []
    skipped = []
    for name in all_names:
        slug = slugify(name)
        path = carriers_root / slug / "carrier.yaml"
        # Skip if already present by slug OR by display name (any case).
        if path.exists() or name.lower() in by_name:
            skipped.append(by_name.get(name.lower(), slug))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(carrier_yaml(name))
        created.append(slug)

    print(f"Created {len(created)} new carrier configs.")
    for s in created:
        print(f"  + {s}")
    if skipped:
        print(f"\nSkipped {len(skipped)} already-configured carriers:")
        for s in skipped:
            print(f"  = {s}")


if __name__ == "__main__":
    main()
