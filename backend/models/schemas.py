"""60-field output schema + supporting Pydantic models."""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MISSING = "missing"


class RowType(str, Enum):
    SERVICE = "S"
    COMPONENT = "C"


class FieldCategory(str, Enum):
    STRUCTURED = "structured"           # account#, phone#, amounts, dates — target >98%
    SEMI_STRUCTURED = "semi_structured"  # address, billing name — target >90%
    FUZZY = "fuzzy"                      # service type, component name — target >80%
    CONTRACT = "contract"                # term, dates, renewal — target >75%


# Maps each of the 60 fields to its category for eval scoring
FIELD_CATEGORIES: dict[str, FieldCategory] = {
    # Structured (exact match eval)
    "carrier_account_number": FieldCategory.STRUCTURED,
    "sub_account_number_1": FieldCategory.STRUCTURED,
    "sub_account_number_2": FieldCategory.STRUCTURED,
    "master_account": FieldCategory.STRUCTURED,
    "btn": FieldCategory.STRUCTURED,
    "phone_number": FieldCategory.STRUCTURED,
    "carrier_circuit_number": FieldCategory.STRUCTURED,
    "monthly_recurring_cost": FieldCategory.STRUCTURED,
    "quantity": FieldCategory.STRUCTURED,
    "cost_per_unit": FieldCategory.STRUCTURED,
    "mrc_per_currency": FieldCategory.STRUCTURED,
    "num_calls": FieldCategory.STRUCTURED,
    "ld_minutes": FieldCategory.STRUCTURED,
    "ld_cost": FieldCategory.STRUCTURED,
    "rate": FieldCategory.STRUCTURED,
    "ld_flat_rate": FieldCategory.STRUCTURED,
    "zip": FieldCategory.STRUCTURED,
    "z_zip": FieldCategory.STRUCTURED,

    # Semi-structured (normalized match eval)
    "billing_name": FieldCategory.SEMI_STRUCTURED,
    "service_address_1": FieldCategory.SEMI_STRUCTURED,
    "service_address_2": FieldCategory.SEMI_STRUCTURED,
    "city": FieldCategory.SEMI_STRUCTURED,
    "state": FieldCategory.SEMI_STRUCTURED,
    "country": FieldCategory.SEMI_STRUCTURED,
    "carrier_name": FieldCategory.SEMI_STRUCTURED,
    "invoice_file_name": FieldCategory.SEMI_STRUCTURED,
    "z_location_name": FieldCategory.SEMI_STRUCTURED,
    "z_address_1": FieldCategory.SEMI_STRUCTURED,
    "z_address_2": FieldCategory.SEMI_STRUCTURED,
    "z_city": FieldCategory.SEMI_STRUCTURED,
    "z_state": FieldCategory.SEMI_STRUCTURED,
    "z_country": FieldCategory.SEMI_STRUCTURED,
    "currency": FieldCategory.SEMI_STRUCTURED,
    "point_to_number": FieldCategory.SEMI_STRUCTURED,
    "port_speed": FieldCategory.SEMI_STRUCTURED,
    "access_speed": FieldCategory.SEMI_STRUCTURED,
    "upload_speed": FieldCategory.SEMI_STRUCTURED,

    # Fuzzy (LLM judge eval)
    "service_type": FieldCategory.FUZZY,
    "service_type_2": FieldCategory.FUZZY,
    "usoc": FieldCategory.FUZZY,
    "service_or_component": FieldCategory.FUZZY,
    "component_or_feature_name": FieldCategory.FUZZY,
    "charge_type": FieldCategory.FUZZY,
    "additional_circuit_ids": FieldCategory.FUZZY,
    "status": FieldCategory.FUZZY,
    "notes": FieldCategory.FUZZY,
    "files_used": FieldCategory.FUZZY,

    # Contract (often missing, LLM judge eval)
    "contract_info_received": FieldCategory.CONTRACT,
    "contract_term_months": FieldCategory.CONTRACT,
    "contract_begin_date": FieldCategory.CONTRACT,
    "contract_expiration_date": FieldCategory.CONTRACT,
    "billing_per_contract": FieldCategory.CONTRACT,
    "currently_month_to_month": FieldCategory.CONTRACT,
    "mtm_or_less_than_year": FieldCategory.CONTRACT,
    "contract_file_name": FieldCategory.CONTRACT,
    "contract_number": FieldCategory.CONTRACT,
    "contract_number_2": FieldCategory.CONTRACT,
    "auto_renew": FieldCategory.CONTRACT,
    "auto_renewal_notes": FieldCategory.CONTRACT,
    "conversion_rate": FieldCategory.CONTRACT,
    "row_type": FieldCategory.FUZZY,
}


class ExtractedRow(BaseModel):
    """One row of the 60-field output."""

    # Row type
    row_type: Optional[RowType] = None

    # DD2 Information Area
    status: Optional[str] = None
    notes: Optional[str] = None
    contract_info_received: Optional[str] = None

    # File Information Area
    invoice_file_name: Optional[str] = None
    files_used: Optional[str] = None
    billing_name: Optional[str] = None

    # Location Area — service address (primary, from CSR per merge priority)
    service_address_1: Optional[str] = None
    service_address_2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None

    # Sidecar: billing address as extracted from the invoice.
    # Populated during merge when the invoice's address differs from the CSR's
    # service address (invoices typically show the remit-to / corporate HQ).
    # Row.status is flipped to "Needs Review" when these diverge.
    billing_address_1: Optional[str] = None
    billing_city: Optional[str] = None
    billing_state: Optional[str] = None
    billing_zip: Optional[str] = None
    billing_name_from_invoice: Optional[str] = None

    # Carrier Information Area
    carrier_name: Optional[str] = None
    master_account: Optional[str] = None
    carrier_account_number: Optional[str] = None
    sub_account_number_1: Optional[str] = None
    sub_account_number_2: Optional[str] = None
    btn: Optional[str] = None

    # Service Area
    phone_number: Optional[str] = None
    carrier_circuit_number: Optional[str] = None
    additional_circuit_ids: Optional[str] = None
    service_type: Optional[str] = None
    service_type_2: Optional[str] = None

    # Component Area
    usoc: Optional[str] = None
    service_or_component: Optional[str] = None
    component_or_feature_name: Optional[str] = None
    monthly_recurring_cost: Optional[Decimal] = None
    quantity: Optional[int] = None
    cost_per_unit: Optional[Decimal] = None
    currency: Optional[str] = None
    conversion_rate: Optional[Decimal] = None
    mrc_per_currency: Optional[Decimal] = None

    # Additional Component Area
    charge_type: Optional[str] = None
    num_calls: Optional[int] = None
    ld_minutes: Optional[Decimal] = None
    ld_cost: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    ld_flat_rate: Optional[Decimal] = None
    point_to_number: Optional[str] = None

    # Circuit Speed Area
    port_speed: Optional[str] = None
    access_speed: Optional[str] = None
    upload_speed: Optional[str] = None

    # Z Location Area
    z_location_name: Optional[str] = None
    z_address_1: Optional[str] = None
    z_address_2: Optional[str] = None
    z_city: Optional[str] = None
    z_state: Optional[str] = None
    z_zip: Optional[str] = None
    z_country: Optional[str] = None

    # Contract Area
    contract_term_months: Optional[int] = None
    contract_begin_date: Optional[date] = None
    contract_expiration_date: Optional[date] = None
    billing_per_contract: Optional[str] = None
    currently_month_to_month: Optional[str] = None
    mtm_or_less_than_year: Optional[str] = None
    contract_file_name: Optional[str] = None
    contract_number: Optional[str] = None
    contract_number_2: Optional[str] = None
    auto_renew: Optional[str] = None
    auto_renewal_notes: Optional[str] = None

    # Compliance (populated post-merge by compliance.py)
    compliance_flags: Optional[list[dict]] = None


class FieldConfidence(BaseModel):
    """Confidence metadata for a single field."""
    field_name: str
    confidence: ConfidenceLevel
    source_document_id: Optional[str] = None
    source_page: Optional[int] = None
    extraction_method: Optional[str] = None  # regex, llm, table, manual


class ClassificationResult(BaseModel):
    """Result of document classification."""
    carrier: Optional[str] = None
    document_type: Optional[str] = None
    format_variant: Optional[str] = None
    account_number: Optional[str] = None
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    method: str = "unknown"  # filename, first_page, llm, human
    file_type: Optional[str] = None  # pdf, xlsx, csv, msg, eml, docx


class ParsedSection(BaseModel):
    """One chunk/section from a parsed document."""
    text: str
    tables: list[list[list[str]]] = Field(default_factory=list)  # list of tables, each is rows of cells
    page_numbers: list[int] = Field(default_factory=list)
    section_type: Optional[str] = None  # monthly_charges, usage, surcharges, call_detail, etc.
    sub_account: Optional[str] = None
    global_context: Optional[str] = None


class ParsedDocument(BaseModel):
    """Result of document parsing — sections ready for LLM extraction."""
    document_id: Optional[str] = None
    file_path: str
    carrier: str
    document_type: str = "unknown"
    format_variant: str = "unknown"
    total_pages: int = 0
    sections: list[ParsedSection] = Field(default_factory=list)
    validation_data: Optional[dict] = None  # e.g., Windstream Location Summary totals
