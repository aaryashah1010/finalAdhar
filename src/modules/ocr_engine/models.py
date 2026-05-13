"""
src/modules/ocr_engine/models.py
──────────────────────────────────
Data models for the OCR engine pipeline.

Privacy contract:
    The ``text`` field on these models is TRANSIENT.  It is used only
    for Aadhaar pattern matching and is never persisted, logged, or
    stored beyond the current processing call.  Only confidence scores,
    rotation metadata, and timing values are safe to log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RotationCandidate:
    """
    Internal result for a single rotation attempt during multi-rotation OCR.

    Holds the raw OCR output for one of the four tried angles (0°, 90°,
    180°, 270°) before the best candidate is selected.

    Attributes:
        degrees:        Rotation applied to the image before OCR (0/90/180/270).
        text:           Raw concatenated OCR text.  NEVER log or persist this.
        avg_confidence: Mean confidence across all detected text regions.
        char_count:     Number of characters in ``text`` (used for scoring).
        score:          Composite score assigned by _select_best(); 0.0 until set.
    """

    degrees:        int
    text:           str
    avg_confidence: float
    char_count:     int
    score:          float = 0.0

    @property
    def is_empty(self) -> bool:
        return self.char_count == 0


@dataclass
class OCRPageResult:
    """
    Final OCR result for one page, returned by run_multi_rotation().

    This is the public output type — everything downstream (detector,
    state store) works with this object.

    Attributes:
        text:               Full extracted text.  TRANSIENT — not for storage.
        confidence:         Mean confidence of the winning rotation's detections.
        rotation_deg:       Which rotation produced the best result (0/90/180/270).
        processing_time_ms: Wall-clock time for the entire multi-rotation run.
        early_exit:         True if processing stopped before trying all 4 rotations.
    """

    text:               str
    confidence:         float
    rotation_deg:       int
    processing_time_ms: float
    early_exit:         bool = False

    @property
    def is_empty(self) -> bool:
        return len(self.text.strip()) == 0

    def to_dict(self) -> dict:
        """
        Return the required output format consumed by the detector.

            {"text": str, "confidence": float}

        Only these two fields are returned — rotation metadata is
        retained on the object itself for logging but excluded here.
        """
        return {
            "text":       self.text,
            "confidence": self.confidence,
        }


@dataclass
class TextBlock:
    """
    A single text region detected by OCR, with its bounding box.

    Used by the masking pipeline to locate and redact Aadhaar numbers
    at their precise position on the page image.

    Attributes:
        text:       Detected text for this region. TRANSIENT — not for storage.
        x0:         Left edge of bounding box in pixels.
        y0:         Top edge of bounding box in pixels.
        x1:         Right edge of bounding box in pixels.
        y1:         Bottom edge of bounding box in pixels.
        confidence: Per-region detection confidence (0.0–1.0).
    """

    text:       str
    x0:         int
    y0:         int
    x1:         int
    y1:         int
    confidence: float

    @property
    def cx(self) -> int:
        """Horizontal center of the bounding box."""
        return (self.x0 + self.x1) // 2

    @property
    def cy(self) -> int:
        """Vertical center of the bounding box."""
        return (self.y0 + self.y1) // 2

    @property
    def height(self) -> int:
        return self.y1 - self.y0
