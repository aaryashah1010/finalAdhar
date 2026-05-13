"""
src/modules/detector/detector.py
──────────────────────────────────
Production-grade Aadhaar number detector for large-scale document processing.

Detection pipeline (per page):
    1. Regex extraction   — find all 12-digit candidates (handles spaces/hyphens)
    2. OCR normalisation  — fix common OCR misreadings before digit matching
    3. Verhoeff checksum  — validate candidates; discard false positives
    4. Keyword scoring    — confirm Aadhaar context on the same page
    5. QR detection       — optional confidence boost via OpenCV QR scanner
    6. Page decision      — requires valid number AND keyword (score ≥ 1)
    7. Document decision  — any Aadhaar page → document is Aadhaar

Performance:
    - Stateless: all methods operate on their arguments, no shared mutable state
    - Early exit: stops page iteration when document confidence ≥ 0.90
    - QR detector created once in __init__ (not per-page)
    - Regex pattern pre-compiled at import time

Privacy contract:
    ✓  Only confidence, page numbers, and counts are logged / returned
    ✗  Detected Aadhaar number strings are NEVER logged, stored, or returned
    ✗  OCR text is NEVER persisted through this module
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import cv2
import numpy as np

from .models import DocumentDetectionResult, PageDetectionResult

logger = logging.getLogger(__name__)


# ── Verhoeff tables (class-level constants, read-only tuples) ─────────────────

#: Multiplication (D) table — dihedral group D5 Cayley table.
_D: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)

#: Permutation (P) table — cycling permutations of D5.
_P: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)

#: Inverse (INV) table — used to compute check digits.
_INV: tuple[int, ...] = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


# ── Regex ─────────────────────────────────────────────────────────────────────

#: Matches a 12-digit group optionally separated by spaces or hyphens.
#: Handles both compact "123456789012" and formatted "1234 5678 9012".
#: Word boundaries prevent matching inside longer digit sequences.
_AADHAAR_RE = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")

#: Strip non-digit characters from a matched group to get the raw 12 digits.
_STRIP_NONDIGIT = re.compile(r"[\s\-]")


# ── OCR error correction map ──────────────────────────────────────────────────

#: Single-character substitutions: OCR-misread letters → correct digits.
#: Applied ONLY during number extraction, NOT during keyword matching.
_OCR_CORRECTION_TABLE: dict[int, int | None] = str.maketrans({   # type: ignore[arg-type]
    "O": "0", "o": "0",    # letter-O looks like zero
    "I": "1",              # uppercase-I looks like one
    "l": "1",              # lowercase-L looks like one
    "B": "8",              # B can OCR-misread as 8
    "S": "5",              # S can OCR-misread as 5
    "Z": "2",              # Z can OCR-misread as 2
    "G": "6",              # G can OCR-misread as 6
})


# ── Context keywords ──────────────────────────────────────────────────────────

#: Lowercase keywords that confirm an Aadhaar-related page context.
#: Multi-word entries require whitespace-normalised text for matching.
_CONTEXT_KEYWORDS: frozenset[str] = frozenset({
    "aadhaar",
    "aadhar",              # common alternate romanisation
    "uidai",
    "government of india",
    "भारत सरकार",         # Hindi: Government of India
})


# ── Tunable thresholds ────────────────────────────────────────────────────────

#: Confidence weights (must sum to 1.0).
_W_VALID_NUMBER = 0.5
_W_KEYWORD      = 0.2
_W_QR           = 0.2
_W_MULTIPLE     = 0.1

#: If document confidence reaches or exceeds this, stop processing further pages.
_EARLY_EXIT_THRESHOLD: float = 0.90

#: Aadhaar numbers may not start with 0 or 1 (UIDAI specification).
_INVALID_FIRST_DIGITS: frozenset[str] = frozenset({"0", "1"})


# ── AadhaarDetector ───────────────────────────────────────────────────────────

class AadhaarDetector:
    """
    Stateless Aadhaar document detector.

    All public methods are pure functions of their arguments — no instance
    state is mutated after __init__.  Safe to call from multiple pages
    sequentially; use one instance per process.

    Usage:
        detector = AadhaarDetector()
        result = detector.process_document(
            pages_text  = ocr_results,    # List[{"text": str, "confidence": float}]
            page_images = rendered_pages, # Optional[List[np.ndarray]]
        )
        print(result.to_dict())
        # {"is_aadhaar": True, "pages": [2], "confidence": 0.9}
    """

    def __init__(self) -> None:
        #: QRCodeDetector created once — avoid per-call construction overhead.
        self._qr_detector = cv2.QRCodeDetector()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def normalize_text(self, text: str) -> str:
        """
        Replace common OCR misreadings of digits in *text*.

        This method is ONLY for the number-extraction path.
        Do NOT use for keyword matching — it corrupts words like "UIDAI"
        (e.g., 'I' → '1' would give "U1DA1").

        Corrections applied:
            O/o → 0,  I → 1,  l → 1,  B → 8,  S → 5,  Z → 2,  G → 6

        Args:
            text: Raw OCR output for one page.

        Returns:
            Copy of *text* with OCR-digit corrections applied.
        """
        return text.translate(_OCR_CORRECTION_TABLE)

    def detect_numbers(self, text: str) -> List[str]:
        """
        Extract candidate 12-digit Aadhaar number strings from *text*.

        Steps:
            1. Apply OCR correction (normalize_text).
            2. Apply regex ``\\b\\d{4}[\\s\\-]?\\d{4}[\\s\\-]?\\d{4}\\b``.
            3. Strip separators → pure 12-digit strings.
            4. Reject masked entries (first 4 digits are all-zero = masked).
            5. Deduplicate preserving first-seen order.

        Note:
            Returned strings are UNVALIDATED — callers must pass each to
            validate_verhoeff() before treating them as real Aadhaar numbers.
            Privacy: do not log or persist the returned strings.

        Args:
            text: Page text (raw OCR output, may contain noise).

        Returns:
            Deduplicated list of 12-digit strings, potentially valid Aadhaar.
        """
        if not text:
            return []

        normalised   = self.normalize_text(text)
        raw_matches  = _AADHAAR_RE.findall(normalised)

        seen: set[str]   = set()
        candidates: list[str] = []

        for match in raw_matches:
            digits = _STRIP_NONDIGIT.sub("", match)

            if len(digits) != 12:
                continue

            # Masked Aadhaar (XXXX XXXX 1234) — after OCR correction,
            # X is not a digit, so the regex won't match.  But if OCR
            # misread X as 0, the prefix "000000001234" would also fail
            # our first-digit check in validate_verhoeff.  Extra guard:
            if digits[:4] == "0000":    # still reject overt masks
                continue

            if digits not in seen:
                seen.add(digits)
                candidates.append(digits)

        return candidates

    def validate_verhoeff(self, number: str) -> bool:
        """
        Validate a 12-digit string using the Verhoeff checksum algorithm.

        The Verhoeff algorithm uses three mathematical tables (D, P, INV)
        to compute a checksum.  A valid Aadhaar number's checksum must
        equal 0 when the digits are processed right-to-left.

        Additional UIDAI constraint:
            Aadhaar numbers must not start with 0 or 1.  Any candidate
            with such a prefix is immediately rejected without table lookup.

        Args:
            number: Exactly 12 decimal digit characters (no spaces/hyphens).

        Returns:
            True if the number is structurally valid per Verhoeff algorithm.
            False for wrong length, invalid first digit, or wrong checksum.
        """
        if len(number) != 12 or not number.isdigit():
            return False

        # UIDAI rule: Aadhaar numbers cannot begin with 0 or 1.
        if number[0] in _INVALID_FIRST_DIGITS:
            return False

        # Verhoeff checksum: process digits right-to-left; valid iff c == 0.
        c = 0
        for i, ch in enumerate(reversed(number)):
            c = _D[c][_P[i % 8][int(ch)]]

        return c == 0

    def detect_qr(self, image: np.ndarray) -> bool:
        """
        Detect any QR code present in *image* using OpenCV.

        A QR code on an Aadhaar page is a confidence boost but is NOT
        required for Aadhaar classification.  Detection failure (missing
        OpenCV build, unusual QR variant, very small code) is logged at
        DEBUG level and returns False — never raises.

        Args:
            image: Page image as a uint8 numpy array, shape (H, W) or (H, W, 3).
                   Use the original rendered image, not the preprocessed version.

        Returns:
            True if a QR code was detected (not necessarily decoded).
        """
        if image is None or image.size == 0:
            return False

        try:
            # detectAndDecode returns non-empty string on successful decode.
            data, _, _ = self._qr_detect_and_decode(image)
            if data:
                logger.debug("QR code decoded successfully.")
                return True

            # Fallback: detect-only is faster and works when decode fails
            # (e.g. damaged or partially visible QR codes).
            found, _ = self._qr_detect(image)
            if found:
                logger.debug("QR code detected (decode failed).")
            return bool(found)

        except Exception as exc:
            logger.debug("QR detection raised %s — treated as not found.",
                         type(exc).__name__)
            return False

    def compute_confidence(
        self,
        has_valid_number: bool,
        keyword_score:    int,
        has_qr:           bool,
        match_count:      int,
    ) -> float:
        """
        Compute the Aadhaar detection confidence for one page.

        Scoring (additive, clamped to [0.0, 1.0]):
            +0.5  valid Verhoeff number found
            +0.2  at least one Aadhaar context keyword present
            +0.2  QR code detected on the page image
            +0.1  more than one valid number found (corroborating evidence)

        Args:
            has_valid_number: True if validate_verhoeff passed for any candidate.
            keyword_score:    Number of distinct Aadhaar keywords matched (≥ 0).
            has_qr:           True if detect_qr() returned True.
            match_count:      Count of Verhoeff-valid numbers on this page.

        Returns:
            Confidence float in [0.0, 1.0].
        """
        score = 0.0

        if has_valid_number:
            score += _W_VALID_NUMBER    # +0.5

        if keyword_score >= 1:
            score += _W_KEYWORD         # +0.2

        if has_qr:
            score += _W_QR              # +0.2

        if match_count > 1:
            score += _W_MULTIPLE        # +0.1

        # Round to 10 dp to eliminate IEEE 754 accumulation errors
        # (e.g. 0.5+0.2+0.2 → 0.8999...9 instead of 0.9).
        return round(min(1.0, max(0.0, score)), 10)

    def process_document(
        self,
        pages_text:  List[dict],
        page_images: Optional[List[np.ndarray]] = None,
    ) -> DocumentDetectionResult:
        """
        Run the full Aadhaar detection pipeline over all pages of a document.

        Args:
            pages_text:  Ordered list of OCR results, one dict per page:
                             {"text": str, "confidence": float}
                         Produced by OCREngine.run_multi_rotation().
            page_images: Optional list of page images (same order as pages_text).
                         Required for QR detection.  If None, QR step is skipped.
                         Use the raw rendered images — not preprocessed.

        Returns:
            DocumentDetectionResult with is_aadhaar, pages, and confidence.
            Call .to_dict() for the {"is_aadhaar", "pages", "confidence"} format.

        Performance:
            - Pages are processed in order.
            - Processing halts (early_exit=True) once document confidence
              reaches _EARLY_EXIT_THRESHOLD (0.90).  The returned `pages`
              list may be incomplete in this case.

        Privacy:
            - Detected number strings are NEVER logged or included in the result.
            - Only counts, page numbers, and confidence values are surfaced.
        """
        aadhaar_pages:   List[int] = []
        max_confidence:  float     = 0.0
        early_exit:      bool      = False
        pages_scanned:   int       = 0

        for i, page_ocr in enumerate(pages_text):
            pages_scanned += 1
            page_number    = i + 1   # 1-based

            text = page_ocr.get("text", "") if isinstance(page_ocr, dict) else ""

            if not text or not text.strip():
                logger.debug("Page %d: empty text — skipping.", page_number)
                continue

            # ── Step 1+2: Number extraction with OCR normalisation ────────
            candidates   = self.detect_numbers(text)

            # ── Step 2: Verhoeff validation ───────────────────────────────
            # Count only — number strings are NOT stored beyond this scope.
            valid_count  = sum(1 for n in candidates if self.validate_verhoeff(n))
            has_valid    = valid_count > 0

            # ── Step 3: Context keyword validation ────────────────────────
            keyword_score = self._count_keywords(text)

            # ── Step 4: QR code detection (optional) ─────────────────────
            has_qr = False
            if page_images is not None and i < len(page_images):
                img = page_images[i]
                if img is not None:
                    has_qr = self.detect_qr(img)

            # ── Step 5: Page-level Aadhaar decision ───────────────────────
            # BOTH conditions must hold — either alone is insufficient.
            is_page_aadhaar = has_valid and (keyword_score >= 1)

            confidence = self.compute_confidence(
                has_valid_number = has_valid,
                keyword_score    = keyword_score,
                has_qr           = has_qr,
                match_count      = valid_count,
            )

            logger.debug(
                "Page %d: valid_nums=%d, keywords=%d, qr=%s, conf=%.4f, aadhaar=%s",
                page_number, valid_count, keyword_score, has_qr,
                confidence, is_page_aadhaar,
            )

            if is_page_aadhaar:
                aadhaar_pages.append(page_number)
                max_confidence = max(max_confidence, confidence)

                logger.info(
                    "Aadhaar confirmed on page %d — "
                    "conf=%.4f, keywords=%d, qr=%s, valid_nums=%d",
                    page_number, confidence, keyword_score, has_qr, valid_count,
                )

                # ── Step 7: Early exit ────────────────────────────────────
                if max_confidence >= _EARLY_EXIT_THRESHOLD:
                    early_exit = True
                    logger.info(
                        "Early exit after page %d — "
                        "document confidence %.4f ≥ %.2f threshold.",
                        page_number, max_confidence, _EARLY_EXIT_THRESHOLD,
                    )
                    break

            # Privacy: candidates list (containing number strings) goes
            # out of scope here and is eligible for GC.  No reference escapes.
            del candidates

        # ── Step 6: Document-level decision ──────────────────────────────
        is_aadhaar = len(aadhaar_pages) > 0

        if not is_aadhaar:
            logger.info("Document: no Aadhaar detected across %d page(s).", pages_scanned)
        else:
            logger.info(
                "Document: Aadhaar FOUND on page(s) %s — "
                "confidence=%.4f, early_exit=%s",
                aadhaar_pages, max_confidence, early_exit,
            )

        return DocumentDetectionResult(
            is_aadhaar          = is_aadhaar,
            pages               = aadhaar_pages,
            confidence          = round(max_confidence, 4),
            total_pages_scanned = pages_scanned,
            early_exit          = early_exit,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _count_keywords(text: str) -> int:
        """
        Count distinct Aadhaar context keywords found in *text*.

        Performs case-insensitive, whitespace-normalised substring search.
        Whitespace normalisation converts newlines / tabs to single spaces
        so multi-word keywords like "government of india" are found even
        when OCR splits lines on words.

        Args:
            text: Raw page OCR text.

        Returns:
            Number of distinct Aadhaar keywords found (0 if none).
        """
        if not text:
            return 0
        # Collapse all whitespace to single spaces for multi-word matching.
        normalised = " ".join(text.lower().split())
        return sum(1 for kw in _CONTEXT_KEYWORDS if kw in normalised)

    def _qr_detect_and_decode(self, image: np.ndarray) -> tuple:
        """
        Thin wrapper around cv2.QRCodeDetector.detectAndDecode.

        Exists solely to make the cv2 read-only C-extension method patchable
        in unit tests via patch.object(detector_instance, '_qr_detect_and_decode').
        """
        return self._qr_detector.detectAndDecode(image)

    def _qr_detect(self, image: np.ndarray) -> tuple:
        """
        Thin wrapper around cv2.QRCodeDetector.detect.

        Exists solely to make the cv2 read-only C-extension method patchable
        in unit tests via patch.object(detector_instance, '_qr_detect').
        """
        return self._qr_detector.detect(image)
