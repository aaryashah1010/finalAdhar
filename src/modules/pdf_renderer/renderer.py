"""
src/modules/pdf_renderer/renderer.py
──────────────────────────────────────
High-performance PDF → numpy image renderer built on PyMuPDF (fitz).

Design goals:
    - Constant memory: O(1) with respect to page count when used as generator
    - Safety: one document open at a time; always closes on error or cleanup
    - Fault isolation: a single bad page never aborts the entire extraction
    - OCR readiness: 300 DPI, BGR channel order, uint8 dtype

Memory profile at 300 DPI (A4 page = 2480 × 3508 px):
    One page rendered ≈ 26 MB (2480 × 3508 × 3 bytes)
    extract_pages() yields one page then immediately releases the array
    reference, allowing GC to reclaim it before the next page is rendered.
    Peak live memory ≈ 2 × page_size (current + PyMuPDF internal buffer).

Coordinate systems:
    PDF space:   origin bottom-left, unit = point (1 pt = 1/72 inch)
    Image space: origin top-left,    unit = pixel
    Conversion:  PageDimensions holds both; coord_transform.py does math

Usage (context manager — recommended):
    with PDFProcessor(dpi=300) as proc:
        result = proc.load_pdf("/data/scans/doc.pdf")
        if result.is_processable:
            for page_dict in proc.extract_pages():
                run_ocr(page_dict["image"])

Usage (manual cleanup):
    proc = PDFProcessor()
    result = proc.load_pdf("/data/scans/doc.pdf")
    for page_dict in proc.extract_pages():
        run_ocr(page_dict["image"])
    proc.cleanup()

Privacy contract:
    This module handles only image pixel data and structural PDF metadata.
    No text content, OCR results, or Aadhaar numbers ever pass through here.
"""

from __future__ import annotations

import gc
import logging
import os
from pathlib import Path
from typing import Generator, Iterator, List, Optional

import numpy as np

from .models import (
    Colorspace,
    LoadResult,
    PDFErrorCode,
    PageData,
    PageDimensions,
)
from ...utils.fitz_compat import fitz

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

#: PDF points per inch (PDF standard).
_POINTS_PER_INCH: float = 72.0

#: Maximum DPI allowed. Beyond this, memory usage becomes extreme.
#: 600 DPI A4 ≈ 104 MB per page.
_MAX_DPI: int = 600

#: Minimum DPI. Below 72 the output is too coarse for reliable OCR.
_MIN_DPI: int = 72


# ── PDFProcessor ─────────────────────────────────────────────────────────────

class PDFProcessor:
    """
    Renders PDF pages to numpy image arrays using PyMuPDF.

    One document open at a time. Instances are reusable — call load_pdf()
    again to switch to a different file (previous file is closed first).

    Thread safety:
        NOT thread-safe. Each worker thread should own its own PDFProcessor
        instance. fitz.Document objects must not be shared across threads.
    """

    def __init__(
        self,
        dpi: int = 300,
        colorspace: Colorspace = Colorspace.BGR,
    ) -> None:
        """
        Args:
            dpi:        Rendering resolution in dots per inch.
                        Range: [72, 600]. Default 300 for OCR quality.
            colorspace: Channel ordering of output images. Default BGR
                        for OpenCV / PaddleOCR compatibility.
        """
        if not (_MIN_DPI <= dpi <= _MAX_DPI):
            raise ValueError(
                f"DPI must be between {_MIN_DPI} and {_MAX_DPI}, got {dpi}."
            )

        self._dpi        = dpi
        self._colorspace = colorspace
        self._matrix     = fitz.Matrix(dpi / _POINTS_PER_INCH,
                                        dpi / _POINTS_PER_INCH)

        # State — populated by load_pdf()
        self._doc:        Optional[fitz.Document] = None
        self._file_path:  Optional[str]           = None
        self._page_count: int                     = 0
        self._load_result: Optional[LoadResult]   = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def load_pdf(self, file_path: str) -> LoadResult:
        """
        Open a PDF file and prepare it for page extraction.

        Behavior:
            - Any previously open document is closed first (no leaks).
            - Encrypted PDFs are detected and reported without crashing.
            - Corrupted PDFs are caught and reported with ERR_PDF_CORRUPTED.
            - The result is stored as ``self.load_result`` for inspection.

        Args:
            file_path: Absolute or relative path to the PDF file.

        Returns:
            LoadResult with metadata. Check ``result.is_processable`` before
            calling extract_pages().

        Raises:
            Never. All errors are captured in LoadResult.error_code.
        """
        # Always clean up prior document before opening a new one.
        self._close_document()

        path = Path(file_path).resolve()
        file_size = self._safe_file_size(path)

        # ── Validate file existence ───────────────────────────────────────
        if not path.exists():
            logger.warning("PDF not found: %s", path.name)
            return self._fail_result(
                str(path), file_size, PDFErrorCode.FILE_NOT_FOUND
            )

        if not os.access(path, os.R_OK):
            logger.warning("PDF not readable: %s", path.name)
            return self._fail_result(
                str(path), file_size, PDFErrorCode.FILE_UNREADABLE
            )

        # ── Open with PyMuPDF ─────────────────────────────────────────────
        try:
            doc = fitz.open(str(path))
        except fitz.FileDataError:
            logger.warning("PDF corrupted: %s", path.name)
            return self._fail_result(
                str(path), file_size, PDFErrorCode.PDF_CORRUPTED
            )
        except Exception as exc:
            logger.warning(
                "Unexpected error opening PDF %s: %s",
                path.name, type(exc).__name__,
            )
            return self._fail_result(
                str(path), file_size, PDFErrorCode.UNKNOWN
            )

        # ── Check for encryption ──────────────────────────────────────────
        if doc.is_encrypted:
            doc.close()
            logger.warning("PDF is encrypted (password required): %s", path.name)
            result = LoadResult(
                file_path       = str(path),
                page_count      = 0,
                is_encrypted    = True,
                file_size_bytes = file_size,
                success         = False,
                error_code      = PDFErrorCode.PDF_ENCRYPTED,
            )
            self._load_result = result
            return result

        # ── Check for empty document ──────────────────────────────────────
        page_count = len(doc)
        if page_count == 0:
            doc.close()
            logger.warning("PDF has no pages: %s", path.name)
            result = LoadResult(
                file_path       = str(path),
                page_count      = 0,
                is_encrypted    = False,
                file_size_bytes = file_size,
                success         = False,
                error_code      = PDFErrorCode.PDF_EMPTY,
            )
            self._load_result = result
            return result

        # ── Success ───────────────────────────────────────────────────────
        self._doc        = doc
        self._file_path  = str(path)
        self._page_count = page_count

        result = LoadResult(
            file_path       = str(path),
            page_count      = page_count,
            is_encrypted    = False,
            file_size_bytes = file_size,
            success         = True,
            error_code      = PDFErrorCode.NONE,
        )
        self._load_result = result

        logger.info(
            "Loaded PDF: %s (%d pages, %.2f MB, %d DPI)",
            path.name, page_count, result.file_size_mb, self._dpi,
        )
        return result

    def extract_pages(
        self,
        page_indices: Optional[List[int]] = None,
    ) -> Generator[dict, None, None]:
        """
        Yield rendered page dicts one at a time: {"page_number", "image"}.

        This is a generator — it renders and yields each page individually,
        then releases the image array reference before rendering the next.
        This keeps memory usage at O(1) with respect to page count, which
        is critical for PDFs with 100+ pages at 300 DPI.

        Full-list usage (only for small PDFs where memory is not a concern):
            pages = list(processor.extract_pages())

        Args:
            page_indices: Zero-based indices of specific pages to extract.
                          None (default) extracts all pages in order.

        Yields:
            dict with keys:
                "page_number" (int)  — 1-based, matches visible PDF labels.
                "image" (np.ndarray) — uint8, shape (H, W, 3) or (H, W).

        Raises:
            RuntimeError: If load_pdf() has not been called successfully.

        Notes:
            - Pages that fail to render are silently skipped and logged.
              The generator continues to the next page — no exception raised.
            - If the caller accumulates yielded images (e.g. list()), memory
              grows proportionally. Use the generator form for large PDFs.
        """
        self._require_loaded()

        indices = self._resolve_indices(page_indices)
        total   = len(indices)

        logger.info(
            "Extracting %d/%d pages from %s at %d DPI",
            total, self._page_count,
            Path(self._file_path).name,
            self._dpi,
        )

        for position, page_index in enumerate(indices, start=1):
            page_data = self._render_page_safe(page_index)

            if not page_data.render_ok:
                # Log and skip — do NOT raise.  One bad page must not abort
                # the entire batch.  Caller sees a gap in page numbers.
                logger.warning(
                    "Skipping page %d/%d [%s]: render failed (%s)",
                    position, total,
                    Path(self._file_path).name,
                    page_data.error_code.value,
                )
                continue

            yield page_data.to_dict()

            # ── Explicit memory management ────────────────────────────────
            # Drop our reference to the numpy array.  If the caller did not
            # store the yielded dict, the array becomes eligible for GC
            # immediately — before the next page is rendered.
            del page_data

    def extract_page(self, page_index: int) -> Optional[dict]:
        """
        Render and return a single page by zero-based index.

        Convenience wrapper around extract_pages() for cases where only
        one specific page is needed (e.g. UI preview of a detected page).

        Args:
            page_index: Zero-based page index.

        Returns:
            {"page_number": int, "image": np.ndarray} or None on failure.
        """
        self._require_loaded()
        page_data = self._render_page_safe(page_index)
        if not page_data.render_ok:
            return None
        return page_data.to_dict()

    def get_page_dimensions(self, page_index: int) -> Optional[PageDimensions]:
        """
        Return the dimensions of a page WITHOUT rendering the image.

        Useful for pre-computing coordinate transforms before OCR,
        or for checking page orientation cheaply.

        Args:
            page_index: Zero-based page index.

        Returns:
            PageDimensions, or None if the page cannot be accessed.
        """
        self._require_loaded()
        if not (0 <= page_index < self._page_count):
            return None
        try:
            page = self._doc[page_index]
            rect = page.rect
            scale = self._dpi / _POINTS_PER_INCH
            return PageDimensions(
                width_pt  = rect.width,
                height_pt = rect.height,
                width_px  = int(rect.width  * scale),
                height_px = int(rect.height * scale),
                dpi       = self._dpi,
            )
        except Exception:
            return None

    def cleanup(self) -> None:
        """
        Release the open PDF document and free all associated resources.

        Safe to call multiple times — idempotent.
        Always call this when done (or use the context manager instead).
        """
        self._close_document()
        gc.collect()
        logger.debug("PDFProcessor cleanup complete.")

    # ─────────────────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def page_count(self) -> int:
        """Number of pages in the currently loaded PDF. 0 if none loaded."""
        return self._page_count

    @property
    def dpi(self) -> int:
        """Rendering DPI for this processor instance."""
        return self._dpi

    @property
    def file_path(self) -> Optional[str]:
        """Absolute path of the currently loaded PDF, or None."""
        return self._file_path

    @property
    def load_result(self) -> Optional[LoadResult]:
        """LoadResult from the most recent load_pdf() call, or None."""
        return self._load_result

    @property
    def is_loaded(self) -> bool:
        """True if a document is currently open and ready for extraction."""
        return self._doc is not None and not self._doc.is_closed

    # ─────────────────────────────────────────────────────────────────────────
    # Context manager
    # ─────────────────────────────────────────────────────────────────────────

    def __enter__(self) -> PDFProcessor:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def __del__(self) -> None:
        """Safety net: close document if object is garbage collected."""
        # Guard against AttributeError when __init__ raised before _doc was set
        # (e.g. ValueError on invalid DPI — __del__ is still called by Python).
        if hasattr(self, "_doc"):
            self._close_document()

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Rendering
    # ─────────────────────────────────────────────────────────────────────────

    def _render_page_safe(self, page_index: int) -> PageData:
        """
        Render a single page, catching all exceptions.

        Never raises — returns a PageData with render_ok=False on any error.
        This isolation ensures one corrupted/blank page cannot abort a batch.

        Args:
            page_index: Zero-based page index into the open document.

        Returns:
            PageData — check render_ok before using the image.
        """
        # ── Bounds check ─────────────────────────────────────────────────
        if not (0 <= page_index < self._page_count):
            return self._failed_page(
                page_index, PDFErrorCode.PAGE_RENDER_FAIL,
                reason="index out of range",
            )

        try:
            page      = self._doc[page_index]
            rect      = page.rect
            fitz_cs   = self._fitz_colorspace()

            # ── Render to pixmap ──────────────────────────────────────────
            # alpha=False: skip alpha channel — saves 25% memory, not needed
            # for OCR.  clip=None: render full page.
            pixmap = page.get_pixmap(
                matrix     = self._matrix,
                colorspace = fitz_cs,
                alpha      = False,
                annots     = True,   # include annotations (stamps etc.)
            )

            # ── Pixmap → numpy array ──────────────────────────────────────
            image = self._pixmap_to_numpy(pixmap, fitz_cs)

            # ── Build dimensions ──────────────────────────────────────────
            dims = PageDimensions(
                width_pt  = rect.width,
                height_pt = rect.height,
                width_px  = pixmap.width,
                height_px = pixmap.height,
                dpi       = self._dpi,
            )

            # ── Release pixmap immediately ────────────────────────────────
            # fitz.Pixmap holds a C-level buffer.  Explicitly release it
            # before returning so the C memory is freed promptly.
            del pixmap

            return PageData(
                page_number = page_index + 1,
                page_index  = page_index,
                image       = image,
                dimensions  = dims,
                colorspace  = self._colorspace,
                render_ok   = True,
                error_code  = PDFErrorCode.NONE,
            )

        except MemoryError:
            logger.error(
                "Out of memory rendering page %d of %s",
                page_index + 1, Path(self._file_path).name,
            )
            return self._failed_page(page_index, PDFErrorCode.OUT_OF_MEMORY)

        except Exception as exc:
            logger.warning(
                "Failed to render page %d of %s: %s",
                page_index + 1,
                Path(self._file_path).name,
                type(exc).__name__,    # class name only — no content logged
            )
            return self._failed_page(page_index, PDFErrorCode.PAGE_RENDER_FAIL)

    def _pixmap_to_numpy(
        self,
        pixmap: fitz.Pixmap,
        fitz_cs: fitz.Colorspace,
    ) -> np.ndarray:
        """
        Convert a fitz.Pixmap to a numpy uint8 array with correct channels.

        Memory note:
            np.frombuffer() creates a view into the pixmap's buffer.
            The .copy() call materialises it into an independent array,
            allowing us to del pixmap without invalidating the array.

        Args:
            pixmap:  Rendered fitz.Pixmap (alpha=False assumed).
            fitz_cs: The fitz colorspace used during rendering.

        Returns:
            numpy array: shape (H, W, 3) for color, (H, W) for grayscale.
            dtype: uint8. Values: [0, 255].
        """
        samples = np.frombuffer(pixmap.samples, dtype=np.uint8).copy()

        if fitz_cs == fitz.csGRAY:
            # Single-channel: (H, W)
            return samples.reshape(pixmap.height, pixmap.width)

        # Three-channel color: fitz always outputs RGB order.
        image = samples.reshape(pixmap.height, pixmap.width, 3)

        if self._colorspace == Colorspace.BGR:
            # RGB → BGR in-place using a reverse-channel view, then copy.
            # This is the standard OpenCV / PaddleOCR channel order.
            return image[:, :, ::-1].copy()

        # RGB — return as-is (already in fitz output order)
        return image

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _fitz_colorspace(self) -> fitz.Colorspace:
        """Map our Colorspace enum to a fitz colorspace constant."""
        if self._colorspace == Colorspace.GRAYSCALE:
            return fitz.csGRAY
        # Both BGR and RGB are rendered as RGB by fitz;
        # channel swap happens in _pixmap_to_numpy().
        return fitz.csRGB

    def _resolve_indices(
        self, page_indices: Optional[List[int]]
    ) -> List[int]:
        """
        Validate and normalise the requested page index list.

        Args:
            page_indices: Caller-supplied list, or None for all pages.

        Returns:
            Validated, de-duplicated list of 0-based indices in ascending order.
            Out-of-range indices are silently dropped with a warning.
        """
        if page_indices is None:
            return list(range(self._page_count))

        valid   = []
        seen    = set()
        n       = self._page_count

        for idx in page_indices:
            if idx in seen:
                continue
            seen.add(idx)
            if 0 <= idx < n:
                valid.append(idx)
            else:
                logger.warning(
                    "Requested page index %d is out of range [0, %d). Skipping.",
                    idx, n,
                )

        return sorted(valid)

    def _close_document(self) -> None:
        """Close the fitz document if one is open. Idempotent."""
        if self._doc is not None:
            try:
                if not self._doc.is_closed:
                    self._doc.close()
            except Exception:
                pass   # suppress — we're cleaning up regardless
            finally:
                self._doc        = None
                self._file_path  = None
                self._page_count = 0

    def _require_loaded(self) -> None:
        """
        Assert that a document is currently open.

        Raises:
            RuntimeError: If load_pdf() has not been called or has failed.
        """
        if not self.is_loaded:
            raise RuntimeError(
                "No PDF is loaded. Call load_pdf() with a valid file first."
            )

    def _failed_page(
        self,
        page_index: int,
        error_code: PDFErrorCode,
        reason: str = "",
    ) -> PageData:
        """
        Construct a PageData representing a render failure.

        Used internally so callers always receive a PageData, never an
        exception — the render_ok flag signals failure.
        """
        scale = self._dpi / _POINTS_PER_INCH
        # Use A4 as a fallback dimension estimate — dimensions unknown
        dims = PageDimensions(
            width_pt  = 595.0,
            height_pt = 842.0,
            width_px  = int(595.0 * scale),
            height_px = int(842.0 * scale),
            dpi       = self._dpi,
        )
        return PageData(
            page_number = page_index + 1,
            page_index  = page_index,
            image       = None,
            dimensions  = dims,
            colorspace  = self._colorspace,
            render_ok   = False,
            error_code  = error_code,
        )

    @staticmethod
    def _fail_result(
        file_path: str,
        file_size: int,
        error_code: PDFErrorCode,
    ) -> LoadResult:
        """Construct a failed LoadResult."""
        return LoadResult(
            file_path       = file_path,
            page_count      = 0,
            is_encrypted    = False,
            file_size_bytes = file_size,
            success         = False,
            error_code      = error_code,
        )

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        """Return file size in bytes, or 0 if the stat call fails."""
        try:
            return path.stat().st_size
        except OSError:
            return 0
