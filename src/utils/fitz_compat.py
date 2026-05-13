"""
Compatibility wrapper for PyMuPDF imports.

Some environments expose the runtime API as ``fitz`` while others expose
``pymupdf`` and leave ``fitz`` as an empty namespace package. This module
normalizes the import so application code gets a working PyMuPDF module.
"""

from __future__ import annotations

try:
    import fitz as _fitz  # type: ignore[import]

    if not hasattr(_fitz, "open"):
        raise ImportError("fitz namespace package has no runtime API")

    fitz = _fitz
except Exception:
    import pymupdf as fitz  # type: ignore[import]


__all__ = ["fitz"]
