"""SQLAlchemy ORM models — mirrors db/init.sql schema."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.database import Base


class Client(Base):
    """Customer — one per end-client organization. A client has many projects
    (uploads). Added Apr-2026 for the per-client master-data store (§2.1).
    Nullable from the Upload side so legacy uploads without a linked client
    continue to work without migration."""
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    uploads: Mapped[list["Upload"]] = relationship(back_populates="client")
    reference_data: Mapped[list["ClientReferenceData"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class ClientReferenceData(Base):
    """Analyst-confirmed authoritative facts scoped to one client. Populated
    organically from corrections + contract uploads — not pre-seeded. Consulted
    by the merger at priority 15 (above CSR=10). Empty by default; pipeline
    falls through to the existing priority matrix when no entries exist."""
    __tablename__ = "client_reference_data"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    carrier: Mapped[str | None] = mapped_column(String(100))
    account_number: Mapped[str | None] = mapped_column(String(100))
    key_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    client: Mapped["Client"] = relationship(back_populates="reference_data")


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String(255))
    client_name: Mapped[str | None] = mapped_column(String(255))  # free text, kept for backward compat
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by: Mapped[str | None] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    status: Mapped[str] = mapped_column(String(50), default="pending")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    # Soft-delete / bin
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bin_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    client: Mapped["Client | None"] = relationship(back_populates="uploads")
    documents: Mapped[list["Document"]] = relationship(back_populates="upload", cascade="all, delete-orphan")
    extraction_runs: Mapped[list["ExtractionRun"]] = relationship(back_populates="upload", cascade="all, delete-orphan")
    extracted_rows: Mapped[list["ExtractedRow"]] = relationship(back_populates="upload", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))

    # Storage
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1000))
    file_hash: Mapped[str | None] = mapped_column(String(64))
    file_type: Mapped[str | None] = mapped_column(String(20))
    file_size_bytes: Mapped[int | None] = mapped_column()
    page_count: Mapped[int | None] = mapped_column()

    # Classification
    carrier: Mapped[str | None] = mapped_column(String(100))
    document_type: Mapped[str | None] = mapped_column(String(50))
    format_variant: Mapped[str | None] = mapped_column(String(100))
    account_number: Mapped[str | None] = mapped_column(String(100))
    classification_confidence: Mapped[str | None] = mapped_column(String(20))
    classification_method: Mapped[str | None] = mapped_column(String(50))

    # Processing
    processing_status: Mapped[str] = mapped_column(String(50), default="pending")
    processing_path: Mapped[str | None] = mapped_column(String(50))
    parsed_text_path: Mapped[str | None] = mapped_column(String(1000))
    parsed_sections_path: Mapped[str | None] = mapped_column(String(1000))

    # Versioning
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    upload: Mapped["Upload"] = relationship(back_populates="documents")


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="pending")

    # Stats
    documents_processed: Mapped[int] = mapped_column(Integer, default=0)
    rows_extracted: Mapped[int] = mapped_column(Integer, default=0)
    fields_high_confidence: Mapped[int] = mapped_column(Integer, default=0)
    fields_medium_confidence: Mapped[int] = mapped_column(Integer, default=0)
    fields_low_confidence: Mapped[int] = mapped_column(Integer, default=0)
    fields_missing: Mapped[int] = mapped_column(Integer, default=0)

    # Cost tracking
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)

    # Config snapshot
    config_version: Mapped[str | None] = mapped_column(String(100))

    # Re-trigger metadata
    last_failed_stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    upload: Mapped["Upload"] = relationship(back_populates="extraction_runs")
    extracted_rows: Mapped[list["ExtractedRow"]] = relationship(back_populates="extraction_run", cascade="all, delete-orphan")
    corrections: Mapped[list["Correction"]] = relationship(back_populates="extraction_run")


class ExtractedRow(Base):
    __tablename__ = "extracted_rows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_runs.id", ondelete="CASCADE"))
    upload_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))
    primary_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    source_documents: Mapped[dict | None] = mapped_column(JSONB, default=list)

    # Account linkage
    carrier: Mapped[str | None] = mapped_column(String(100))
    account_number: Mapped[str | None] = mapped_column(String(100))
    sub_account_number: Mapped[str | None] = mapped_column(String(100))
    row_type: Mapped[str | None] = mapped_column(String(1))

    # DD2 Information Area
    status: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    contract_info_received: Mapped[str | None] = mapped_column(String(100))

    # File Information Area
    invoice_file_name: Mapped[str | None] = mapped_column(String(500))
    files_used: Mapped[str | None] = mapped_column(Text)
    billing_name: Mapped[str | None] = mapped_column(String(255))

    # Location Area — primary service address (CSR first per merge priority)
    service_address_1: Mapped[str | None] = mapped_column(String(255))
    service_address_2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(50))
    zip: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(50))

    # Sidecar: billing address from the invoice — populated during merge when
    # the invoice's address differs from the primary service address.
    billing_address_1: Mapped[str | None] = mapped_column(String(255))
    billing_city: Mapped[str | None] = mapped_column(String(100))
    billing_state: Mapped[str | None] = mapped_column(String(50))
    billing_zip: Mapped[str | None] = mapped_column(String(20))
    billing_name_from_invoice: Mapped[str | None] = mapped_column(String(255))

    # Carrier Information Area
    carrier_name: Mapped[str | None] = mapped_column(String(100))
    master_account: Mapped[str | None] = mapped_column(String(100))
    carrier_account_number: Mapped[str | None] = mapped_column(String(100))
    sub_account_number_1: Mapped[str | None] = mapped_column(String(100))
    sub_account_number_2: Mapped[str | None] = mapped_column(String(100))
    btn: Mapped[str | None] = mapped_column(String(50))

    # Service Area
    phone_number: Mapped[str | None] = mapped_column(String(50))
    carrier_circuit_number: Mapped[str | None] = mapped_column(String(100))
    additional_circuit_ids: Mapped[str | None] = mapped_column(Text)
    service_type: Mapped[str | None] = mapped_column(String(100))
    service_type_2: Mapped[str | None] = mapped_column(String(100))

    # Component Area
    usoc: Mapped[str | None] = mapped_column(String(50))
    service_or_component: Mapped[str | None] = mapped_column(String(10))
    component_or_feature_name: Mapped[str | None] = mapped_column(String(255))
    monthly_recurring_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    quantity: Mapped[int | None] = mapped_column()
    cost_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(10))
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), default=1.0)
    mrc_per_currency: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # Additional Component Area
    charge_type: Mapped[str | None] = mapped_column(String(50))
    num_calls: Mapped[int | None] = mapped_column()
    ld_minutes: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    ld_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ld_flat_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    point_to_number: Mapped[str | None] = mapped_column(String(50))

    # Circuit Speed Area
    port_speed: Mapped[str | None] = mapped_column(String(50))
    access_speed: Mapped[str | None] = mapped_column(String(50))
    upload_speed: Mapped[str | None] = mapped_column(String(50))

    # Z Location Area
    z_location_name: Mapped[str | None] = mapped_column(String(255))
    z_address_1: Mapped[str | None] = mapped_column(String(255))
    z_address_2: Mapped[str | None] = mapped_column(String(255))
    z_city: Mapped[str | None] = mapped_column(String(100))
    z_state: Mapped[str | None] = mapped_column(String(50))
    z_zip: Mapped[str | None] = mapped_column(String(20))
    z_country: Mapped[str | None] = mapped_column(String(50))

    # Contract Area
    contract_term_months: Mapped[int | None] = mapped_column()
    contract_begin_date: Mapped[date | None] = mapped_column(Date)
    contract_expiration_date: Mapped[date | None] = mapped_column(Date)
    billing_per_contract: Mapped[str | None] = mapped_column(String(255))
    currently_month_to_month: Mapped[str | None] = mapped_column(String(10))
    mtm_or_less_than_year: Mapped[str | None] = mapped_column(String(10))
    contract_file_name: Mapped[str | None] = mapped_column(String(500))
    contract_number: Mapped[str | None] = mapped_column(String(100))
    contract_number_2: Mapped[str | None] = mapped_column(String(100))
    auto_renew: Mapped[str | None] = mapped_column(String(10))
    auto_renewal_notes: Mapped[str | None] = mapped_column(Text)

    # Compliance
    compliance_flags: Mapped[dict | None] = mapped_column(JSONB, default=list)
    compliance_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Metadata
    field_confidence: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    field_sources: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    review_status: Mapped[str] = mapped_column(String(50), default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    extraction_run: Mapped["ExtractionRun"] = relationship(back_populates="extracted_rows")
    upload: Mapped["Upload"] = relationship(back_populates="extracted_rows")
    corrections: Mapped[list["Correction"]] = relationship(back_populates="extracted_row", cascade="all, delete-orphan")


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extracted_row_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extracted_rows.id", ondelete="CASCADE"))
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction_runs.id"))

    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    extracted_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    correction_type: Mapped[str | None] = mapped_column(String(50))

    carrier: Mapped[str | None] = mapped_column(String(100))
    format_variant: Mapped[str | None] = mapped_column(String(100))
    source_text_snippet: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    source_page: Mapped[int | None] = mapped_column()

    correction_context: Mapped[str | None] = mapped_column(Text)
    # embedding stored via raw SQL (pgvector type not natively supported by SQLAlchemy)
    # Column: embedding vector(768) — indexed with ivfflat for cosine similarity

    # Root-cause diagnosis — populated by feedback service after correction is saved
    root_cause: Mapped[str | None] = mapped_column(String(50))
    # One of: EXTRACTION, MERGE, ENRICHMENT, DATA_GAP, ANALYST_JUDGMENT, UNKNOWN
    diagnosis_details: Mapped[dict | None] = mapped_column(JSONB)
    # Stores: {stage, raw_extraction_value, merged_value, source_doc, explanation}

    corrected_by: Mapped[str | None] = mapped_column(String(255))
    corrected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    correction_notes: Mapped[str | None] = mapped_column(Text)

    applied_as: Mapped[str | None] = mapped_column(String(50))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Relationships
    extracted_row: Mapped["ExtractedRow"] = relationship(back_populates="corrections")
    extraction_run: Mapped["ExtractionRun"] = relationship(back_populates="corrections")
