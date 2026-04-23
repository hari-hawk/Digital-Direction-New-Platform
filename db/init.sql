-- Digital Direction Database Schema
-- PostgreSQL 16 + pgvector
-- Note: LangFuse database (langfuse) is created by docker-compose langfuse service

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- CORE TABLES
-- ============================================

CREATE TABLE uploads (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255),
    client_name VARCHAR(255),
    uploaded_by VARCHAR(255),
    uploaded_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(50) DEFAULT 'pending',
    file_count INT DEFAULT 0,
    notes TEXT,
    -- Soft-delete / bin
    deleted_at TIMESTAMP NULL,
    bin_retention_days INT DEFAULT 30,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_uploads_deleted_at ON uploads(deleted_at);

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    upload_id UUID REFERENCES uploads(id) ON DELETE CASCADE,

    -- Storage
    original_filename VARCHAR(500) NOT NULL,
    storage_path VARCHAR(1000),
    file_hash VARCHAR(64),
    file_type VARCHAR(20),
    file_size_bytes BIGINT,
    page_count INT,

    -- Classification
    carrier VARCHAR(100),
    document_type VARCHAR(50),
    format_variant VARCHAR(100),
    account_number VARCHAR(100),
    classification_confidence VARCHAR(20),
    classification_method VARCHAR(50),

    -- Processing
    processing_status VARCHAR(50) DEFAULT 'pending',
    processing_path VARCHAR(50),
    parsed_text_path VARCHAR(1000),
    parsed_sections_path VARCHAR(1000),

    -- Versioning (for re-uploads)
    supersedes_id UUID REFERENCES documents(id),

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_documents_upload ON documents(upload_id);
CREATE INDEX idx_documents_carrier ON documents(carrier);
CREATE INDEX idx_documents_hash ON documents(file_hash);
CREATE INDEX idx_documents_account ON documents(account_number);

CREATE TABLE extraction_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    upload_id UUID REFERENCES uploads(id) ON DELETE CASCADE,

    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status VARCHAR(50) DEFAULT 'pending',

    -- Stats
    documents_processed INT DEFAULT 0,
    rows_extracted INT DEFAULT 0,
    fields_high_confidence INT DEFAULT 0,
    fields_medium_confidence INT DEFAULT 0,
    fields_low_confidence INT DEFAULT 0,
    fields_missing INT DEFAULT 0,

    -- Cost tracking
    total_input_tokens INT DEFAULT 0,
    total_output_tokens INT DEFAULT 0,
    estimated_cost_usd DECIMAL(10, 4) DEFAULT 0,

    -- Config snapshot
    config_version VARCHAR(100),

    -- Re-trigger metadata
    last_failed_stage VARCHAR(50) NULL,   -- classify | parse | extract | merge | validate
    retry_count INT DEFAULT 0,
    last_error_message TEXT NULL,

    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================
-- 60-FIELD OUTPUT TABLE
-- ============================================

CREATE TABLE extracted_rows (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    extraction_run_id UUID REFERENCES extraction_runs(id) ON DELETE CASCADE,
    upload_id UUID REFERENCES uploads(id) ON DELETE CASCADE,
    primary_document_id UUID REFERENCES documents(id),
    source_documents JSONB DEFAULT '[]',

    -- Account linkage
    carrier VARCHAR(100),
    account_number VARCHAR(100),
    sub_account_number VARCHAR(100),
    row_type VARCHAR(1),

    -- DD2 Information Area
    status VARCHAR(50),
    notes TEXT,
    contract_info_received VARCHAR(100),

    -- File Information Area
    invoice_file_name VARCHAR(500),
    files_used TEXT,
    billing_name VARCHAR(255),

    -- Location Area
    service_address_1 VARCHAR(255),
    service_address_2 VARCHAR(255),
    city VARCHAR(100),
    state VARCHAR(50),
    zip VARCHAR(20),
    country VARCHAR(50),

    -- Carrier Information Area
    carrier_name VARCHAR(100),
    master_account VARCHAR(100),
    carrier_account_number VARCHAR(100),
    sub_account_number_1 VARCHAR(100),
    sub_account_number_2 VARCHAR(100),
    btn VARCHAR(50),

    -- Service Area
    phone_number VARCHAR(50),
    carrier_circuit_number VARCHAR(100),
    additional_circuit_ids TEXT,
    service_type VARCHAR(100),
    service_type_2 VARCHAR(100),

    -- Component Area
    usoc VARCHAR(50),
    service_or_component VARCHAR(10),
    component_or_feature_name VARCHAR(255),
    monthly_recurring_cost DECIMAL(12, 2),
    quantity INT,
    cost_per_unit DECIMAL(12, 2),
    currency VARCHAR(10),
    conversion_rate DECIMAL(10, 4) DEFAULT 1.0,
    mrc_per_currency DECIMAL(12, 2),

    -- Additional Component Area
    charge_type VARCHAR(50),
    num_calls INT,
    ld_minutes DECIMAL(10, 2),
    ld_cost DECIMAL(12, 2),
    rate DECIMAL(12, 6),
    ld_flat_rate DECIMAL(12, 2),
    point_to_number VARCHAR(50),

    -- Circuit Speed Area
    port_speed VARCHAR(50),
    access_speed VARCHAR(50),
    upload_speed VARCHAR(50),

    -- Z Location Area
    z_location_name VARCHAR(255),
    z_address_1 VARCHAR(255),
    z_address_2 VARCHAR(255),
    z_city VARCHAR(100),
    z_state VARCHAR(50),
    z_zip VARCHAR(20),
    z_country VARCHAR(50),

    -- Contract Area
    contract_term_months INT,
    contract_begin_date DATE,
    contract_expiration_date DATE,
    billing_per_contract VARCHAR(255),
    currently_month_to_month VARCHAR(10),
    mtm_or_less_than_year VARCHAR(10),
    contract_file_name VARCHAR(500),
    contract_number VARCHAR(100),
    contract_number_2 VARCHAR(100),
    auto_renew VARCHAR(10),
    auto_renewal_notes TEXT,

    -- Compliance
    compliance_flags JSONB DEFAULT '[]',
    compliance_checked_at TIMESTAMP,

    -- Metadata
    field_confidence JSONB DEFAULT '{}',
    field_sources JSONB DEFAULT '{}',
    review_status VARCHAR(50) DEFAULT 'pending',
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMP,

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_extracted_rows_run ON extracted_rows(extraction_run_id);
CREATE INDEX idx_extracted_rows_upload ON extracted_rows(upload_id);
CREATE INDEX idx_extracted_rows_carrier ON extracted_rows(carrier);
CREATE INDEX idx_extracted_rows_review ON extracted_rows(review_status);

-- ============================================
-- LEARNED CLASSIFICATION TABLE
-- ============================================

-- Maps client-specific identifiers to carriers (learned from data, not hardcoded)
CREATE TABLE known_accounts (
    id SERIAL PRIMARY KEY,
    identifier VARCHAR(255) NOT NULL,      -- e.g., master agreement number, account prefix
    identifier_type VARCHAR(50) NOT NULL,  -- master_agreement, account_number, customer_name
    carrier VARCHAR(100) NOT NULL,
    learned_from_document_id UUID REFERENCES documents(id),
    confidence FLOAT DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(identifier, identifier_type)
);

CREATE INDEX idx_known_accounts_identifier ON known_accounts(identifier);

-- ============================================
-- CORRECTION & SELF-HEALING TABLES
-- ============================================

CREATE TABLE corrections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    extracted_row_id UUID REFERENCES extracted_rows(id) ON DELETE CASCADE,
    extraction_run_id UUID REFERENCES extraction_runs(id),

    field_name VARCHAR(100) NOT NULL,
    extracted_value TEXT,
    corrected_value TEXT,
    correction_type VARCHAR(50),

    carrier VARCHAR(100),
    format_variant VARCHAR(100),
    source_text_snippet TEXT,
    source_document_id UUID REFERENCES documents(id),
    source_page INT,

    correction_context TEXT,
    embedding vector(768),

    -- Root-cause diagnosis (populated by feedback service)
    root_cause VARCHAR(50),  -- EXTRACTION, MERGE, ENRICHMENT, DATA_GAP, ANALYST_JUDGMENT, UNKNOWN
    diagnosis_details JSONB,  -- {stage, raw_extraction_value, merged_value, source_doc, explanation}

    corrected_by VARCHAR(255),
    corrected_at TIMESTAMP DEFAULT NOW(),
    correction_notes TEXT,

    applied_as VARCHAR(50),
    applied_at TIMESTAMP
);

CREATE INDEX idx_corrections_carrier_field ON corrections(carrier, format_variant, field_name);
CREATE INDEX idx_corrections_embedding ON corrections USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);

CREATE TABLE format_flags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID REFERENCES documents(id),

    carrier VARCHAR(100),
    document_type VARCHAR(50),

    closest_known_format VARCHAR(100),
    similarity_score FLOAT,
    missing_signatures JSONB,
    unexpected_patterns JSONB,
    first_page_text TEXT,

    status VARCHAR(50) DEFAULT 'pending',
    assigned_to VARCHAR(255),
    resolution VARCHAR(50),
    new_format_config VARCHAR(255),

    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP
);

CREATE TABLE format_signatures (
    id SERIAL PRIMARY KEY,
    carrier VARCHAR(100),
    document_type VARCHAR(50),
    format_variant VARCHAR(100),

    signature_text TEXT,
    embedding vector(768),
    sample_count INT DEFAULT 0,

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_format_signatures_embedding ON format_signatures USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

-- ============================================
-- EVAL TABLES
-- ============================================

CREATE TABLE golden_data (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    carrier VARCHAR(100),
    account_number VARCHAR(100),
    document_set_description TEXT,
    golden_rows JSONB,

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE eval_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    extraction_run_id UUID REFERENCES extraction_runs(id),
    golden_data_id UUID REFERENCES golden_data(id),

    overall_accuracy FLOAT,
    structured_accuracy FLOAT,
    semi_structured_accuracy FLOAT,
    fuzzy_accuracy FLOAT,
    contract_accuracy FLOAT,
    field_scores JSONB,
    error_analysis JSONB,

    judge_model VARCHAR(100),
    judge_response TEXT,

    created_at TIMESTAMP DEFAULT NOW()
);
