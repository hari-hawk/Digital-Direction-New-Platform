"""Loads carrier YAML configs into typed Pydantic models."""

import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from backend.settings import settings


# ============================================
# Config Models
# ============================================


class FilenamePattern(BaseModel):
    pattern: str
    confidence: float = 0.8
    case_insensitive: bool = False

    def matches(self, filename: str) -> bool:
        flags = re.IGNORECASE if self.case_insensitive else 0
        return bool(re.search(self.pattern, filename, flags))


class AccountNumberPattern(BaseModel):
    pattern: str
    format_desc: str = ""
    example: str = ""

    def extract(self, text: str) -> list[str]:
        return re.findall(self.pattern, text)


class FirstPageSignals(BaseModel):
    required_any: list[str] = Field(default_factory=list)
    doc_type_markers: dict[str, list[str]] = Field(default_factory=dict)


class AccountNormalizationConfig(BaseModel):
    """How to normalize account numbers for merge key matching."""
    char_class: str = "digits"            # "digits" | "alphanumeric"
    canonical_length: Optional[int] = None  # null = no truncation
    check_digit_position: Optional[str] = None  # "trailing" | null


class PhoneNormalizationConfig(BaseModel):
    """How to normalize phone numbers for merge key matching."""
    pad_short_phones: bool = False
    pad_source: Optional[str] = None       # "account_prefix" | null
    pad_digits: int = 3


class MergeRulesConfig(BaseModel):
    """Per-carrier merge behavior — loaded from carrier.yaml merge_rules section."""
    account_normalization: AccountNormalizationConfig = Field(default_factory=AccountNormalizationConfig)
    phone_normalization: PhoneNormalizationConfig = Field(default_factory=PhoneNormalizationConfig)
    field_priority_overrides: dict[str, dict[str, int]] = Field(default_factory=dict)
    doc_type_priority_overrides: dict[str, int] = Field(default_factory=dict)
    # Cross-document merge roles: "primary" | "enrichment" | "supplemental"
    # primary: forms merge base, matches via Tier 1/2 keys
    # enrichment: propagates fields to matching account rows, never creates new rows
    # supplemental: appended to output if no Tier 1/2 match found
    doc_type_roles: dict[str, str] = Field(default_factory=dict)
    # Additional fields beyond defaults that should propagate at account level
    enrichment_fields: list[str] = Field(default_factory=list)
    # Fields used to build account equivalence classes
    account_equivalence_fields: list[str] = Field(
        default_factory=lambda: ["carrier_account_number", "master_account"]
    )
    # Whether to include sub_account_number_1 in the merge key.
    # Set to False when invoice and subscription use incompatible sub-account
    # schemes (e.g., Peerless: invoice has no sub-accounts, subscription uses
    # composite IDs). With False, all rows group by account only and service
    # identity matching pairs rows within the group.
    sub_account_in_merge_key: bool = True
    # Fields to EXCLUDE from account-level propagation. For multi-location
    # accounts (e.g., AT&T Centrex with 10+ locations under one account),
    # address fields should NOT propagate at account level — each phone
    # group has its own service location. Phone-level propagation (within
    # merge key groups) handles address correctly.
    non_propagating_fields: list[str] = Field(default_factory=list)
    # Additional fields to include in account-level propagation.
    # Contract fields vary: AT&T Centrex has one contract per account (safe to
    # propagate), but Peerless has per-service contracts (NOT safe). This lets
    # each carrier opt in to contract propagation.
    extra_propagation_fields: list[str] = Field(default_factory=list)


class CarrierConfig(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    filename_patterns: dict[str, list[FilenamePattern]] = Field(default_factory=dict)
    account_number_patterns: list[AccountNumberPattern] = Field(default_factory=list)
    first_page_signals: Optional[FirstPageSignals] = None
    merge_rules: MergeRulesConfig = Field(default_factory=MergeRulesConfig)
    # known_identifiers removed — client-specific data lives in known_accounts DB table


class SignatureConfig(BaseModel):
    required_patterns: list[str] = Field(default_factory=list)
    any_of_patterns: list[str] = Field(default_factory=list)

    def matches(self, text: str) -> bool:
        for pattern in self.required_patterns:
            if not re.search(pattern, text, re.IGNORECASE):
                return False
        if self.any_of_patterns:
            return any(re.search(p, text, re.IGNORECASE) for p in self.any_of_patterns)
        return True


class ChunkingConfig(BaseModel):
    global_context_source: str = "first_page"
    section_markers: list[str] = Field(default_factory=list)
    boundary_pattern: Optional[str] = None
    validation_section: Optional[str] = None
    pages_per_chunk: Optional[int] = None  # Group N pages per section
    scanned_pages_per_group: Optional[int] = None  # Pages per group for scanned PDF multimodal extraction
    column_aware: bool = False  # Use column-aware text extraction for multi-column layouts


class FormatConfig(BaseModel):
    name: str
    version: str = "1.0"
    signature: SignatureConfig = Field(default_factory=SignatureConfig)
    processing_path: str = "docling"  # docling, raw_text, pandas
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    extractable_fields: dict[str, list[str]] = Field(default_factory=dict)
    field_rules: dict = Field(default_factory=dict)
    examples: list[str] = Field(default_factory=list)


class DomainKnowledge(BaseModel):
    usoc_codes: dict[str, str] = Field(default_factory=dict)
    field_codes: dict[str, str] = Field(default_factory=dict)
    line_types: dict[str, str] = Field(default_factory=dict)
    service_types: dict[str, str] = Field(default_factory=dict)


# ============================================
# Loader
# ============================================


class ConfigStore:
    """Loads and caches all carrier configs from YAML files."""

    def __init__(self, configs_dir: str | None = None):
        self.configs_dir = Path(configs_dir or settings.configs_dir)
        self._carriers: dict[str, CarrierConfig] = {}
        self._formats: dict[str, dict[str, FormatConfig]] = {}  # carrier -> {format_name -> config}
        self._knowledge: dict[str, DomainKnowledge] = {}
        self._prompts: dict[str, dict[str, str]] = {}  # carrier -> {prompt_name -> text}
        self._loaded = False

    def load_all(self) -> None:
        carriers_dir = self.configs_dir / "carriers"
        if not carriers_dir.exists():
            return

        for carrier_dir in sorted(carriers_dir.iterdir()):
            if not carrier_dir.is_dir():
                continue
            carrier_name = carrier_dir.name
            self._load_carrier(carrier_name, carrier_dir)

        self._loaded = True

    def _load_carrier(self, name: str, carrier_dir: Path) -> None:
        # carrier.yaml
        carrier_yaml = carrier_dir / "carrier.yaml"
        if carrier_yaml.exists():
            data = yaml.safe_load(carrier_yaml.read_text())
            # Filter out None values in filename_patterns (from YAML comments without list items)
            if "filename_patterns" in data:
                data["filename_patterns"] = {
                    k: v for k, v in data["filename_patterns"].items() if v is not None
                }
            self._carriers[name] = CarrierConfig(**data)

        # formats/
        formats_dir = carrier_dir / "formats"
        if formats_dir.exists():
            self._formats[name] = {}
            for fmt_file in sorted(formats_dir.glob("*.yaml")):
                data = yaml.safe_load(fmt_file.read_text())
                fmt_name = fmt_file.stem
                self._formats[name][fmt_name] = FormatConfig(**data)

        # domain_knowledge/
        dk_dir = carrier_dir / "domain_knowledge"
        knowledge = DomainKnowledge()
        if dk_dir.exists():
            for dk_file in sorted(dk_dir.glob("*.yaml")):
                data = yaml.safe_load(dk_file.read_text()) or {}
                field_name = dk_file.stem
                if hasattr(knowledge, field_name):
                    setattr(knowledge, field_name, data)
        self._knowledge[name] = knowledge

        # prompts/
        prompts_dir = carrier_dir / "prompts"
        if prompts_dir.exists():
            self._prompts[name] = {}
            for prompt_file in sorted(prompts_dir.glob("*.md")):
                self._prompts[name][prompt_file.stem] = prompt_file.read_text()

    def get_carrier(self, name: str) -> CarrierConfig | None:
        if not self._loaded:
            self.load_all()
        return self._carriers.get(name)

    def get_all_carriers(self) -> dict[str, CarrierConfig]:
        if not self._loaded:
            self.load_all()
        return self._carriers

    def get_formats(self, carrier: str) -> dict[str, FormatConfig]:
        if not self._loaded:
            self.load_all()
        return self._formats.get(carrier, {})

    def get_format(self, carrier: str, format_name: str) -> FormatConfig | None:
        return self.get_formats(carrier).get(format_name)

    def get_knowledge(self, carrier: str) -> DomainKnowledge:
        if not self._loaded:
            self.load_all()
        return self._knowledge.get(carrier, DomainKnowledge())

    def get_prompt(self, carrier: str, prompt_name: str) -> str | None:
        if not self._loaded:
            self.load_all()
        return self._prompts.get(carrier, {}).get(prompt_name)

    def get_merge_rules(self, carrier: str) -> MergeRulesConfig:
        """Get merge rules for a carrier. Returns safe defaults if carrier unknown."""
        config = self.get_carrier(carrier)
        if config:
            return config.merge_rules
        return MergeRulesConfig()

    def find_carrier_by_alias(self, text: str) -> str | None:
        """Find carrier name by matching aliases in text."""
        if not self._loaded:
            self.load_all()
        text_lower = text.lower()
        for name, config in self._carriers.items():
            for alias in config.aliases:
                if alias.lower() in text_lower:
                    return name
        return None

    def match_format_variant(self, carrier: str, text: str) -> FormatConfig | None:
        """Find the format variant that matches the document text."""
        formats = self.get_formats(carrier)
        for fmt_name, fmt_config in formats.items():
            if fmt_config.signature.matches(text):
                return fmt_config
        return None


# Singleton
_store: ConfigStore | None = None


def get_config_store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
        _store.load_all()
    return _store


def reset_config_store() -> None:
    """Drop the singleton so the next get_config_store() re-reads from disk.

    Used after auto-registering a newly-discovered carrier so the next
    classify/extract call sees the new carrier.yaml without a process restart.
    """
    global _store
    _store = None
