"""
src/modules/detector/models.py
────────────────────────────────
Data models for Aadhaar detection results.

Privacy contract:
    Models expose ONLY structural detection metadata:
        ✓  is_aadhaar flag
        ✓  page numbers
        ✓  confidence scores
        ✓  match counts (how many, never which ones)
    They NEVER store:
        ✗  Detected Aadhaar number strings
        ✗  OCR text content
        ✗  Any personally identifiable information
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class PageDetectionResult:
    """
    Detection outcome for a single page of a PDF.

    Used internally by AadhaarDetector.process_document() to build
    the final DocumentDetectionResult.  Not exposed in the public API.

    Attributes:
        page_number:       1-based page index.
        is_aadhaar:        True when both a valid number AND a keyword are found.
        has_valid_number:  True if at least one Verhoeff-valid 12-digit number found.
        has_keyword:       True if at least one Aadhaar context keyword found.
        has_qr:            True if a QR code was detected on the page image.
        confidence:        Composite detection confidence in [0.0, 1.0].
        valid_number_count: Count of Verhoeff-valid numbers found.  Never the numbers.
        keyword_score:     Count of distinct Aadhaar keywords matched.
    """

    page_number:        int
    is_aadhaar:         bool
    has_valid_number:   bool
    has_keyword:        bool
    has_qr:             bool
    confidence:         float
    valid_number_count: int
    keyword_score:      int


@dataclass
class DocumentDetectionResult:
    """
    Final Aadhaar detection result for an entire document.

    This is the public output type returned by
    AadhaarDetector.process_document() and consumed by downstream
    modules (state store, UI, masker queue).

    Attributes:
        is_aadhaar:           True if any page in the document is an Aadhaar page.
        pages:                1-based page numbers where Aadhaar was confirmed.
                              May be a subset if early_exit triggered.
        confidence:           Maximum page-level confidence across all Aadhaar pages.
                              0.0 if is_aadhaar is False.
        total_pages_scanned:  Pages actually processed (may be < total if early exit).
        early_exit:           True if processing stopped before the last page.
    """

    is_aadhaar:          bool
    pages:               List[int]
    confidence:          float
    total_pages_scanned: int
    early_exit:          bool = False

    def to_dict(self) -> dict:
        """
        Return the required output format consumed by the pipeline.

            {
                "is_aadhaar": bool,
                "pages":      [int, ...],
                "confidence": float,
            }

        Internal metadata (total_pages_scanned, early_exit) is intentionally
        excluded — they are diagnostic fields, not part of the public contract.
        """
        return {
            "is_aadhaar": self.is_aadhaar,
            "pages":      self.pages,
            "confidence": round(self.confidence, 4),
        }
