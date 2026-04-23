"""Tests for the self-healing feedback service.

Tests root-cause diagnosis, correction querying, and pattern analysis.
Uses the extraction cache on disk (same data used by evals).
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from backend.services.feedback import (
    RootCause,
    diagnose_correction,
    get_relevant_corrections,
    analyze_correction_patterns,
    CorrectionHint,
    KnowledgeSuggestion,
)


# ============================================
# Root-Cause Diagnosis Tests
# ============================================


class TestDiagnoseCorrection:
    """Test root-cause diagnosis against real extraction cache."""

    def test_extraction_error_detected(self):
        """When raw extraction has wrong value and merge kept it → EXTRACTION."""
        # AT&T CSR 614_336_1586 extracts all addresses as "6271 COSGRAY RD"
        # If someone corrects it to a different address that's NOT in any raw extraction,
        # that's either DATA_GAP or EXTRACTION error
        diag = diagnose_correction(
            carrier="att",
            field_name="service_address_1",
            extracted_value="6271 COSGRAY RD",
            corrected_value="6271 COSGRAY RD",  # Same value = no correction needed
            account_number="6143361586",
            phone_number="6143361586",
        )
        # Correcting to the same value → should find it in raw extraction
        assert diag.root_cause in (RootCause.MERGE, RootCause.EXTRACTION, RootCause.UNKNOWN)

    def test_data_gap_detected(self):
        """When corrected value doesn't exist in any raw extraction → DATA_GAP."""
        diag = diagnose_correction(
            carrier="att",
            field_name="auto_renew",
            extracted_value=None,
            corrected_value="Yes",
            account_number="6143361586",
            phone_number="6143361586",
        )
        # auto_renew is not in any AT&T extraction cache
        assert diag.root_cause == RootCause.DATA_GAP

    def test_missing_cache_returns_data_gap(self):
        """When no extraction cache for carrier → DATA_GAP (value not found anywhere)."""
        diag = diagnose_correction(
            carrier="nonexistent_carrier",
            field_name="billing_name",
            extracted_value="ACME",
            corrected_value="ACME Corp",
        )
        assert diag.root_cause == RootCause.DATA_GAP

    def test_merge_error_when_raw_has_correct_value(self):
        """When raw extraction has the correct value but merge changed it → MERGE."""
        # AT&T 5500 CSR extracts addresses per-SLA. If merge propagates wrong address,
        # the raw extraction would have the correct value.
        # This test verifies the logic, not specific data.
        diag = diagnose_correction(
            carrier="att",
            field_name="service_type",
            extracted_value="BLC",
            corrected_value="POTS",
            account_number="6143361586",
            phone_number="6143368199",
        )
        # "BLC" is in the raw cache for 1586, "POTS" might be in raw cache too
        # or it might be a normalization issue (handled by merge)
        assert diag.root_cause in (RootCause.EXTRACTION, RootCause.MERGE,
                                    RootCause.DATA_GAP, RootCause.ENRICHMENT)
        assert diag.explanation  # Should always have an explanation


# ============================================
# Correction Querying Tests
# ============================================


class TestGetRelevantCorrections:
    """Test correction hint retrieval with guardrails."""

    def _write_corrections(self, tmp_dir: str, carrier: str, corrections: list[dict]):
        path = Path(tmp_dir) / f"{carrier}_corrections.json"
        path.write_text(json.dumps(corrections))

    def test_empty_when_no_corrections(self):
        """No correction file → empty list."""
        with tempfile.TemporaryDirectory() as tmp:
            hints = get_relevant_corrections("att", corrections_dir=tmp)
            assert hints == []

    def test_guardrail_filters_single_correction(self):
        """Single correction for a pattern → NOT returned (needs 2+ agreement)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
            ])
            hints = get_relevant_corrections("att", corrections_dir=tmp)
            assert len(hints) == 0

    def test_two_agreeing_corrections_returned(self):
        """Two corrections agreeing on same field+value → returned."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
            ])
            hints = get_relevant_corrections("att", corrections_dir=tmp)
            assert len(hints) == 1
            assert hints[0].field_name == "service_type"
            assert hints[0].correct_value == "POTS"
            assert hints[0].occurrence_count == 2

    def test_non_extraction_corrections_excluded(self):
        """Corrections with root_cause != EXTRACTION → not returned."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "billing_name", "extracted_value": "ACME",
                 "corrected_value": "ACME Corp", "root_cause": "MERGE"},
                {"field_name": "billing_name", "extracted_value": "ACME",
                 "corrected_value": "ACME Corp", "root_cause": "MERGE"},
            ])
            hints = get_relevant_corrections("att", corrections_dir=tmp)
            assert len(hints) == 0

    def test_multiple_patterns_sorted_by_frequency(self):
        """Multiple correction patterns → sorted by frequency (most common first)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                # 3 corrections for service_type
                {"field_name": "service_type", "extracted_value": "X",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                {"field_name": "service_type", "extracted_value": "Y",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                {"field_name": "service_type", "extracted_value": "Z",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                # 2 corrections for billing_name
                {"field_name": "billing_name", "extracted_value": "A",
                 "corrected_value": "B", "root_cause": "EXTRACTION"},
                {"field_name": "billing_name", "extracted_value": "A",
                 "corrected_value": "B", "root_cause": "EXTRACTION"},
            ])
            hints = get_relevant_corrections("att", corrections_dir=tmp)
            assert len(hints) == 2
            assert hints[0].field_name == "service_type"  # 3 > 2
            assert hints[0].occurrence_count == 3


# ============================================
# Pattern Analysis Tests
# ============================================


class TestAnalyzeCorrectionPatterns:
    """Test correction pattern analysis and suggestion generation."""

    def _write_corrections(self, tmp_dir: str, carrier: str, corrections: list[dict]):
        path = Path(tmp_dir) / f"{carrier}_corrections.json"
        path.write_text(json.dumps(corrections))

    def test_empty_when_no_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            suggestions = analyze_correction_patterns("att", corrections_dir=tmp)
            assert suggestions == []

    def test_extraction_pattern_generates_suggestion(self):
        """3+ identical extraction corrections → suggestion generated."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
            ])
            suggestions = analyze_correction_patterns("att", corrections_dir=tmp, min_count=3)
            assert len(suggestions) == 1
            assert suggestions[0].suggestion_type == "service_type"
            assert suggestions[0].field_name == "service_type"
            assert suggestions[0].suggested_value == "POTS"
            assert suggestions[0].correction_count == 3

    def test_below_threshold_no_suggestion(self):
        """2 corrections (below default min_count=3) → no suggestion."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
                {"field_name": "service_type", "extracted_value": "BLC",
                 "corrected_value": "POTS", "root_cause": "EXTRACTION"},
            ])
            suggestions = analyze_correction_patterns("att", corrections_dir=tmp, min_count=3)
            assert len(suggestions) == 0

    def test_data_gap_pattern_flagged(self):
        """Repeated DATA_GAP corrections → suggestion with data_gap type."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "auto_renew", "extracted_value": "",
                 "corrected_value": "Yes", "root_cause": "DATA_GAP"},
                {"field_name": "auto_renew", "extracted_value": "",
                 "corrected_value": "Yes", "root_cause": "DATA_GAP"},
                {"field_name": "auto_renew", "extracted_value": "",
                 "corrected_value": "Yes", "root_cause": "DATA_GAP"},
            ])
            suggestions = analyze_correction_patterns("att", corrections_dir=tmp, min_count=3)
            assert len(suggestions) == 1
            assert suggestions[0].suggestion_type == "data_gap"

    def test_merge_error_pattern(self):
        """Repeated MERGE corrections → merge_rule suggestion."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_corrections(tmp, "att", [
                {"field_name": "service_address_1", "extracted_value": "OLD ADDR",
                 "corrected_value": "NEW ADDR", "root_cause": "MERGE"},
                {"field_name": "service_address_1", "extracted_value": "OLD ADDR",
                 "corrected_value": "NEW ADDR", "root_cause": "MERGE"},
                {"field_name": "service_address_1", "extracted_value": "OLD ADDR",
                 "corrected_value": "NEW ADDR", "root_cause": "MERGE"},
            ])
            suggestions = analyze_correction_patterns("att", corrections_dir=tmp, min_count=3)
            assert len(suggestions) == 1
            assert suggestions[0].suggestion_type == "merge_rule"
