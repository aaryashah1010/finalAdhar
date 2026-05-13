"""
src/modules/pdf_renderer/models.py
───────────────────────────────────
Data models for the PDF rendering pipeline.

These are pure data containers — zero I/O, zero PyMuPDF dependency.
Every field is documented with its unit, coordinate system, and dtype
so downstream modules (OCR, masker, UI) never need to guess.

Privacy contract:
    Models store only structural metadata (dimensions, DPI, page index).
    No text content, no Aadhaar numbers, no OCR results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np


# ── Enumerations ──────────────────────────────────────────────────────────────

class Colorspace(Enum):
    """
    Color channel ordering of the rendered image array.

    BGR is the OpenCV / PaddleOCR convention (most common in this pipeline).
    RGB is standard for PIL / matplotlib display.
    GRAYSCALE collapses channels to a single luminance plane (H × W).
    """
    BGR       = "bgr"        # OpenCV default — used by PaddleOCR internally
    RGB       = "rgb"        # PIL / matplotlib
    GRAYSCALE = "grayscale"  # (H, W) uint8 — faster OCR on some engines


class PDFErrorCode(Enum):
    """
    Structured error codes for PDF-level failures.
    All codes are safe to store and log — they carry no sensitive content.
    """
    NONE             = "none"
    FILE_NOT_FOUND   = "ERR_FILE_NOT_FOUND"
    FILE_UNREADABLE  = "ERR_FILE_UNREADABLE"
    PDF_CORRUPTED    = "ERR_PDF_CORRUPTED"
    PDF_ENCRYPTED    = "ERR_PDF_ENCRYPTED"
    PDF_EMPTY        = "ERR_PDF_EMPTY"
    PAGE_RENDER_FAIL = "ERR_PAGE_RENDER_FAIL"
    OUT_OF_MEMORY    = "ERR_OUT_OF_MEMORY"
    UNKNOWN          = "ERR_UNKNOWN"


# ── Core data containers ──────────────────────────────────────────────────────

@dataclass
class PageDimensions:
    """
    Dual representation of a page's dimensions.

    PDF coordinates (points) are the canonical measurement used for
    masking and region annotation.  Pixel coordinates depend on DPI
    and are used for image processing and display.

    Coordinate system:
        PDF: origin at bottom-left, y increases upward (fitz convention)
        Image: origin at top-left, y increases downward (numpy/OpenCV convention)
    """
    # PDF point space (1 point = 1/72 inch)
    width_pt:  float
    height_pt: float

    # Rendered pixel space (depends on DPI)
    width_px:  int
    height_px: int

    # Rendering parameters
    dpi: int

    @property
    def aspect_ratio(self) -> float:
        """Width / Height ratio of the page."""
        return self.width_pt / self.height_pt if self.height_pt else 1.0

    @property
    def pixel_area(self) -> int:
        """Total number of pixels (width × height)."""
        return self.width_px * self.height_px

    @property
    def estimated_size_bytes(self) -> int:
        """Estimated memory for a 3-channel uint8 image of this page."""
        return self.pixel_area * 3


@dataclass
class PageData:
    """
    Complete output record for a single rendered PDF page.

    This is the unit of data that flows from PDFProcessor → OCREngine.
    The ``image`` field is the only large object; all other fields are
    small scalars suitable for logging and persistence.

    Attributes:
        page_number:  1-based page number (user-visible, matches PDF labels).
        page_index:   0-based index (internal cursor, used for list access).
        image:        Rendered image as a numpy array.
                      Shape: (H, W, 3) for BGR/RGB, (H, W) for GRAYSCALE.
                      Dtype: uint8. Values in [0, 255].
        dimensions:   Dual pixel/point dimensions of this page.
        colorspace:   Channel ordering of ``image``.
        render_ok:    False if this page failed to render (image may be None).
        error_code:   Set when render_ok is False.
    """
    page_number: int          # 1-based
    page_index:  int          # 0-based
    image:       Optional[np.ndarray]
    dimensions:  PageDimensions
    colorspace:  Colorspace
    render_ok:   bool = True
    error_code:  PDFErrorCode = PDFErrorCode.NONE

    def to_dict(self) -> dict:
        """
        Return the required output format:
            {"page_number": int, "image": np.ndarray}

        This is the contract consumed by the OCR engine and masker.
        Only call on successfully rendered pages (render_ok == True).
        """
        return {
            "page_number": self.page_number,
            "image":       self.image,
        }

    @property
    def memory_bytes(self) -> int:
        """Actual memory used by the image array, or 0 if not rendered."""
        if self.image is None:
            return 0
        return self.image.nbytes


@dataclass
class LoadResult:
    """
    Result returned by PDFProcessor.load_pdf().

    Provides all the metadata needed to decide how to process a file
    without yet rendering any pages.

    Attributes:
        file_path:        Absolute path of the opened PDF.
        page_count:       Number of pages. 0 on failure.
        is_encrypted:     True if the PDF requires a password.
        file_size_bytes:  Size of the PDF file on disk.
        success:          False if the PDF could not be opened.
        error_code:       Structured code when success is False.
    """
    file_path:       str
    page_count:      int
    is_encrypted:    bool
    file_size_bytes: int
    success:         bool
    error_code:      PDFErrorCode = PDFErrorCode.NONE

    @property
    def is_processable(self) -> bool:
        """True when the PDF is open, not encrypted, and has pages."""
        return self.success and not self.is_encrypted and self.page_count > 0

    @property
    def file_size_mb(self) -> float:
        return round(self.file_size_bytes / 1_048_576, 2)
