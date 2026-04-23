"""File deduplication — SHA-256 hash based.

Same hash = skip (identical re-upload).
Same filename + different hash = process as superseding version.
"""

import logging
from dataclasses import dataclass

from backend.services.storage import StorageBackend

logger = logging.getLogger(__name__)


@dataclass
class DedupResult:
    is_duplicate: bool = False
    is_new_version: bool = False
    existing_document_id: str | None = None
    file_hash: str = ""


def check_duplicate(
    file_path: str,
    original_filename: str,
    existing_hashes: dict[str, str] | None = None,
    existing_filenames: dict[str, str] | None = None,
) -> DedupResult:
    """Check if file is duplicate or new version.

    Args:
        file_path: Path to uploaded file
        original_filename: Original filename from upload
        existing_hashes: Map of hash → document_id from DB
        existing_filenames: Map of filename → document_id from DB
    """
    file_hash = StorageBackend.file_hash(file_path)
    result = DedupResult(file_hash=file_hash)

    # Same hash = exact duplicate
    if existing_hashes and file_hash in existing_hashes:
        result.is_duplicate = True
        result.existing_document_id = existing_hashes[file_hash]
        logger.info(f"Duplicate detected: {original_filename} (hash matches {result.existing_document_id})")
        return result

    # Same filename + different hash = new version
    if existing_filenames and original_filename in existing_filenames:
        result.is_new_version = True
        result.existing_document_id = existing_filenames[original_filename]
        logger.info(f"New version detected: {original_filename} supersedes {result.existing_document_id}")
        return result

    return result
