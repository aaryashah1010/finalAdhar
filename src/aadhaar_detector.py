"""
src/aadhaar_detector.py
───────────────────────
Scans a single PDF for Aadhaar content.

Reuses existing modules:
  - src.modules.pdf_renderer.renderer.PDFProcessor  — PDF → numpy image
  - src.modules.detector.detector.AadhaarDetector   — Verhoeff + QR + keywords

Detection strategy (per page, in order):
  1. QR code         — fast, works on blurry images, skip OCR if found
  2. OCR pass 1      — English only (fast path)
     → If English keywords found → stop here
  3. OCR pass 2      — eng+hin+guj+mar (multilingual fallback)
     → Only runs if English pass found nothing
  4. Core detector   — Verhoeff checksum + English keyword scoring (strict)
  5. Extended check  — keyword-only fallback for masked-number cards
     → UIDAI / Unique Identification Authority / VID: / native script

Single background thread — no multiprocessing, safe on loaded PCs.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from src.modules.detector.detector import AadhaarDetector as _CoreDetector
from src.modules.pdf_renderer.renderer import PDFProcessor
from src.utils.fitz_compat import fitz

logger = logging.getLogger(__name__)


# ── Tesseract setup ───────────────────────────────────────────────────────────

_TESS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
_TESS_READY = False


def _init_tesseract() -> bool:
    global _TESS_READY
    if _TESS_READY:
        return True
    try:
        import pytesseract
        path_tesseract = shutil.which("tesseract")
        if path_tesseract:
            pytesseract.pytesseract.tesseract_cmd = path_tesseract
            _TESS_READY = True
            return True
        for p in _TESS_CANDIDATES:
            if os.path.exists(p):
                pytesseract.pytesseract.tesseract_cmd = p
                _TESS_READY = True
                return True
        logger.warning("Tesseract binary not found at default paths.")
        return False
    except ImportError:
        logger.error("pytesseract not installed. Run: pip install pytesseract")
        return False


# ── Keyword sets ──────────────────────────────────────────────────────────────
# NOTE: src/modules/detector/detector.py (core) covers:
#   "aadhaar", "aadhar", "uidai", "government of india", "भारत सरकार"
# Everything below EXTENDS that — fills gaps for Gujarati, Marathi,
# masked-number cards, and additional English signals.
# The core module is NOT modified.

# Strong English-only keywords — each alone uniquely identifies Aadhaar
_STRONG_EN = frozenset({
    "uidai",
    "unique identification authority of india",
    "unique identification authority",
    "vid:",
    "vid :",
})

# Weaker English keywords — need 2+ to be confident
_WEAK_EN = frozenset({
    "aadhaar", "aadhar",
    "government of india",
    "enrolment no", "enrollment no",
    "your aadhaar",
    "your aadhar",
})

# Native script keywords — any ONE alone is enough (very Aadhaar-specific)
# HINDI (Devanagari script)
_NATIVE_HI = (
    "आधार",             # Aadhaar
    "भारत सरकार",       # Government of India  ← also in core, kept for completeness
    "भारतीय विशिष्ट",   # Unique Indian (partial of UIDAI full name)
    "विशिष्ट पहचान",    # Unique Identity
    "मेरा आधार",        # My Aadhaar (tagline on Hindi cards)
    "मेरी पहचान",       # My Identity
)

# GUJARATI (Gujarati script — completely absent from core detector)
_NATIVE_GU = (
    "આધાર",             # Aadhaar  ← NOTE: different script from Hindi "आधार"
    "ભારત સરકાર",       # Government of India
    "ભારતીય વિશિષ્ટ",   # Unique Indian (partial)
    "વિશિષ્ટ ઓળખ",      # Unique Identity
    "મારો આધાર",        # My Aadhaar (Gujarati tagline)
    "મારી ઓળખ",         # My Identity
)

# MARATHI (Devanagari — similar to Hindi but different words)
_NATIVE_MR = (
    "माझे आधार",        # My Aadhaar (Marathi tagline)
    "माझी ओळख",         # My Identity
    "आपला आधार",        # Your Aadhaar
    "भारतीय विशिष्ट ओळख",  # Unique Identification (Marathi)
)

# Combined tuple for fast iteration
_NATIVE_ALL = _NATIVE_HI + _NATIVE_GU + _NATIVE_MR


_NEGATIVE_FILENAME_TERMS = frozenset({
    "bank",
    "passbook",
    "cheque",
    "check",
})

_NEGATIVE_FILENAME_PHRASES = (
    "pan card",
    "cancel cheque",
    "cancelled cheque",
    "canceled cheque",
    "cancel check",
    "cancelled check",
    "canceled check",
)

_PAN_FILENAME_MARKERS = (
    "pancard",
    "pancopy",
    "panfront",
    "panback",
    "panid",
    "panno",
    "pannum",
    "pannumber",
)

_PAN_FILENAME_TOKENS = frozenset({
    "pan",
})

_AADHAAR_FILENAME_MARKERS = (
    "aadhaar",
    "aadhar",
    "adhaar",
    "adharcard",
    "adharno",
    "uidai",
)

_AADHAAR_FILENAME_TOKENS = frozenset({
    "adhar",
    "uid",
})


# ── Result model ──────────────────────────────────────────────────────────────

@dataclass
class DetectionResult:
    path:          Path
    is_aadhaar:    bool
    confidence:    float          # 0.0 – 1.0
    reasons:       List[str]      = field(default_factory=list)
    preview_image: Optional[object] = None   # np.ndarray — all pages stitched vertically
    is_uncertain:  bool           = False    # True = too blurry to decide, show to user

    @property
    def confidence_label(self) -> str:
        if self.is_uncertain:
            return "UNCERTAIN"
        if self.confidence >= 0.7:
            return "HIGH"
        if self.confidence >= 0.4:
            return "MEDIUM"
        return "LOW"

    @property
    def reason_str(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "—"


# ── File scanner ──────────────────────────────────────────────────────────────

class AadhaarFileScanner:
    """
    Scan a single PDF file for Aadhaar content.

    Usage:
        scanner = AadhaarFileScanner()
        result  = scanner.scan(Path("document.pdf"))
        if result.is_aadhaar:
            show_viewer(result.preview_image, result.reason_str)
    """

    def __init__(self, render_dpi: int = 150) -> None:
        self.render_dpi = render_dpi
        self._core      = _CoreDetector()
        _init_tesseract()

    # ── Public ────────────────────────────────────────────────────────────────

    def scan(self, pdf_path: Path) -> DetectionResult:
        """Full detection pipeline for one PDF. Never raises."""
        filename_profile = self._classify_filename(pdf_path)
        if filename_profile["pure_numeric"]:
            logger.info(
                "%s — skipped by pure numeric filename form pattern.",
                pdf_path.name,
            )
            return DetectionResult(
                pdf_path,
                False,
                0.0,
                ["Skipped: pure numeric filename form pattern"],
            )

        if filename_profile["pan_named"]:
            logger.info(
                "%s — skipped by PAN filename business rule.",
                pdf_path.name,
            )
            return DetectionResult(
                pdf_path,
                False,
                0.0,
                ["Skipped: PAN filename"],
            )

        renderer = PDFProcessor(dpi=self.render_dpi)
        try:
            load = renderer.load_pdf(str(pdf_path))
            if not load.success:
                return DetectionResult(pdf_path, False, 0.0, ["Cannot open file"])

            pages_text:   List[dict]       = []
            page_images:  List[np.ndarray] = []
            all_pages:    List[np.ndarray] = []   # every rendered page for preview

            if filename_profile["aadhaar_named"]:
                logger.info(
                    "%s — Aadhaar detected by filename rule.",
                    pdf_path.name,
                )
                for page_dict in renderer.extract_pages():
                    all_pages.append(page_dict["image"])
                return DetectionResult(
                    path          = pdf_path,
                    is_aadhaar    = True,
                    confidence    = 0.95,
                    reasons       = ["Filename indicates Aadhaar"],
                    preview_image = self._stitch_pages(all_pages),
                )

            # ── Pre-extract embedded text (ms for digital PDFs) ───────────
            embedded_texts: List[str] = []
            try:
                _fdoc = fitz.open(str(pdf_path))
                for _i in range(_fdoc.page_count):
                    embedded_texts.append(_fdoc[_i].get_text())
                _fdoc.close()
            except Exception:
                embedded_texts = [""] * load.page_count

            for page_dict in renderer.extract_pages():
                img  = page_dict["image"]
                pnum = page_dict["page_number"]
                all_pages.append(img)

                # ── Layer 1: QR (fast, skip OCR if found) ─────────────────
                if self._detect_qr_enhanced(img):
                    logger.info("%s — QR code detected on page %d",
                                pdf_path.name, pnum)
                    # Render remaining pages at low DPI for preview only
                    remaining = self._render_remaining_pages(
                        renderer, pnum, load.page_count
                    )
                    all_pages.extend(remaining)
                    return DetectionResult(
                        path          = pdf_path,
                        is_aadhaar    = True,
                        confidence    = 0.9,
                        reasons       = [f"p{pnum}: QR code"],
                        preview_image = self._stitch_pages(all_pages),
                    )

                # ── Layer 2: Embedded text (instant for digital PDFs) ─────
                page_idx = pnum - 1
                embed    = embedded_texts[page_idx] if page_idx < len(embedded_texts) else ""
                if len(embed.strip()) > 150:
                    logger.debug("[EMBED %s:p%d] chars=%d — skipping OCR",
                                 pdf_path.name, pnum, len(embed))
                    text = embed
                else:
                    # ── Layer 3: OCR (scanned/image PDFs) ────────────────
                    text = self._run_ocr(img, debug_name=f"{pdf_path.name}:p{pnum}")

                pages_text.append({"text": text, "confidence": 0.8})
                page_images.append(img)

            preview = self._stitch_pages(all_pages)

            # ── Layer 4: Core detector (Verhoeff + keyword, strict) ───────
            doc = self._core.process_document(pages_text, page_images)
            if doc.is_aadhaar:
                return DetectionResult(
                    path          = pdf_path,
                    is_aadhaar    = True,
                    confidence    = doc.confidence,
                    reasons       = [f"Pages {doc.pages}: number + keyword"],
                    preview_image = preview,
                )

            # ── Layer 5: Extended keyword-only (catches masked cards) ─────
            for i, pt in enumerate(pages_text):
                raw = pt["text"]
                # Log only the length — never the OCR text, which contains the number.
                logger.debug("[EXTCHECK %s p%d] text_len=%d",
                             pdf_path.name, i+1, len(raw))
                hit, reason = self._extended_keyword_check(raw)
                if hit:
                    has_qr = (self._detect_qr_enhanced(page_images[i])
                              if i < len(page_images) else False)
                    if filename_profile["negative"] and not has_qr:
                        logger.info(
                            "%s — weak Aadhaar keyword ignored because filename "
                            "looks like PAN/cheque/bank evidence (%s).",
                            pdf_path.name, reason,
                        )
                        continue
                    conf   = 0.75 if has_qr else 0.55
                    return DetectionResult(
                        path          = pdf_path,
                        is_aadhaar    = True,
                        confidence    = conf,
                        reasons       = [f"p{i+1}: {reason}"],
                        preview_image = preview,
                    )

            # ── Check if image was too blurry to trust the non-detection ──
            uncertain = self._is_too_blurry(pages_text)
            if uncertain:
                logger.info("%s — too blurry to decide, flagging for manual review",
                            pdf_path.name)
                return DetectionResult(
                    path          = pdf_path,
                    is_aadhaar    = False,
                    confidence    = 0.0,
                    reasons       = ["Image too blurry — manual review needed"],
                    preview_image = preview,
                    is_uncertain  = True,
                )

            return DetectionResult(pdf_path, False, 0.0)

        except Exception as exc:
            logger.warning("Scan error for %s: %s", pdf_path.name, exc)
            return DetectionResult(pdf_path, False, 0.0, [f"Error: {exc}"])
        finally:
            renderer.cleanup()

    # ── Filename signal helpers ──────────────────────────────────────────────

    @staticmethod
    def _classify_filename(pdf_path: Path) -> dict:
        """
        Return lightweight filename hints used to reduce known false positives.

        Pure numeric stems and PAN-named files are business-rule skips from the
        client's file naming pattern. Aadhaar-named files are direct detections.
        """
        stem = pdf_path.stem.strip().lower()
        spaced = re.sub(r"[^a-z0-9]+", " ", stem).strip()
        compact = re.sub(r"[^a-z0-9]+", "", stem)
        tokens = set(spaced.split())

        is_pan = (
            bool(tokens & _PAN_FILENAME_TOKENS)
            or any(marker in compact for marker in _PAN_FILENAME_MARKERS)
            or bool(re.match(r"^pan\d", compact))
        )
        has_negative_term = (
            bool(tokens & _NEGATIVE_FILENAME_TERMS)
            or any(term in compact for term in _NEGATIVE_FILENAME_TERMS)
        )
        has_negative_phrase = any(phrase in spaced for phrase in _NEGATIVE_FILENAME_PHRASES)

        return {
            "pure_numeric": stem.isdigit(),
            "pan_named": is_pan,
            "negative": is_pan or has_negative_term or has_negative_phrase,
            "aadhaar_named": (
                any(marker in compact for marker in _AADHAAR_FILENAME_MARKERS)
                or bool(tokens & _AADHAAR_FILENAME_TOKENS)
            ),
        }

    # ── OCR: single multilingual pass with early exit ────────────────────────

    def _run_ocr(self, image: np.ndarray, debug_name: str = "") -> str:
        """
        Single OCR pass using all languages (eng+hin+guj+mar).
        Tries 0° first — early exits as soon as any Aadhaar signal is found.
        Only continues to 90°/180°/270° if 0° finds nothing.
        """
        try:
            import pytesseract
        except ImportError:
            return ""

        gray  = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray  = clahe.apply(gray)
        cfg   = "--oem 1 --psm 6"
        lang  = "eng+hin+guj+mar"
        best  = ""

        for angle in (0, 90, 180, 270):
            rot  = self._rot(gray, angle)
            text = self._tess(rot, lang, cfg)
            tl   = text.lower()

            # Log only the character count — never the OCR text itself,
            # which would contain the Aadhaar number.
            logger.debug("[OCR %s] %d° → chars=%d", debug_name, angle, len(text))

            # Early exit — any Aadhaar signal found, skip remaining rotations
            if any(kw in tl for kw in _STRONG_EN):
                logger.debug("[OCR %s] Early exit at %d° — strong EN keyword", debug_name, angle)
                return text
            if "aadha" in tl:
                logger.debug("[OCR %s] Early exit at %d° — aadhaar keyword", debug_name, angle)
                return text
            if "government of india" in tl:
                logger.debug("[OCR %s] Early exit at %d° — government of india", debug_name, angle)
                return text
            if any(kw in text for kw in _NATIVE_ALL):
                logger.debug("[OCR %s] Early exit at %d° — native keyword", debug_name, angle)
                return text

            # Valid Verhoeff number found
            nums = self._core.detect_numbers(text)
            if any(self._core.validate_verhoeff(n) for n in nums):
                logger.debug("[OCR %s] Early exit at %d° — Verhoeff number", debug_name, angle)
                return text

            # Skip rotations if 0° produced rich, clean text
            # (>500 chars + >50% ASCII = digital or well-oriented scan)
            if angle == 0:
                ascii_ch = sum(1 for c in text if c.isascii() and c.isprintable() and not c.isspace())
                total_ch = sum(1 for c in text if c.isprintable() and not c.isspace())
                if total_ch > 0 and len(text) > 500 and ascii_ch / total_ch > 0.5:
                    logger.debug("[OCR %s] Skip rotations — rich text at 0° (chars=%d)", debug_name, len(text))
                    return text

            if len(text) > len(best):
                best = text

        return best

    # ── Extended keyword-only check ───────────────────────────────────────────

    @staticmethod
    def _extended_keyword_check(text: str) -> tuple[bool, str]:
        """
        Catch masked-number Aadhaar cards via keywords alone.

        Returns (is_aadhaar, reason_string).
        """
        if not text:
            return False, ""

        tl = re.sub(r'\s+', ' ', text.lower())

        # ── Exact / near-exact keywords ───────────────────────────────────
        if "uidai" in tl:
            return True, "Keyword: UIDAI"
        if "aadha" in tl:
            return True, "Keyword: Aadhaar (partial)"
        if "vid:" in tl or "vid :" in tl:
            return True, "Keyword: VID"
        if "your aadhaar" in tl or "your aadhar" in tl:
            return True, "Keyword: Your Aadhaar"

        # ── Partial keywords — robust to OCR errors on first/last chars ──
        # "Government of India" → OCR often reads as "Government of inca/Indla/lndia"
        if "government of in" in tl:
            return True, "Keyword: Government of India (partial)"

        # "Unique Identification Authority" → OCR garbles "Unique"/"India"
        # Check just the middle part which is more stable
        if "identification authority" in tl:
            return True, "Keyword: Identification Authority (partial)"

        # "Unique Identification" alone (without "Authority")
        if "unique identif" in tl:
            return True, "Keyword: Unique Identification (partial)"

        # "Enrollment No" / "Enrolment No" / "Enroliment No" (OCR variants)
        # Use regex for flexible matching
        if re.search(r'enrol\w{0,6}\s*no', tl):
            return True, "Keyword: Enrollment No (partial)"

        # "authority of india" — catches even without "Unique Identification" prefix
        if "authority of india" in tl or "authority of in" in tl:
            return True, "Keyword: Authority of India (partial)"

        # ── Native script — each is uniquely Aadhaar-specific ─────────────
        for kw in _NATIVE_ALL:
            if kw in text:
                return True, f"Native keyword: {kw}"

        return False, ""

    # ── Blurriness / readability check ───────────────────────────────────────

    @staticmethod
    def _is_too_blurry(pages_text: List[dict]) -> bool:
        """
        Returns True if the OCR text across all pages is too garbled to trust.

        Heuristic: count "real words" (3+ consecutive alphabetic chars).
        If the total real-word count across all pages is < 8, the image is
        too degraded to confidently say it's NOT an Aadhaar card.

        Examples:
          - AdhaarCard_9: garbled symbols → ~2 real words → uncertain ✅
          - Assignment 2: clean English text → 100+ real words → skip ✅
          - AWS cert: clean text → 30+ real words → skip ✅
        """
        if not pages_text:
            return True

        total_real_words = 0
        for pt in pages_text:
            text = pt.get("text", "")
            # Count sequences of 3+ alphabetic chars as "real words"
            words = re.findall(r'[a-zA-Z\u0900-\u097F\u0A80-\u0AFF\u0900-\u097F]{3,}', text)
            total_real_words += len(words)

        return total_real_words < 8

    # ── Page stitching for full-PDF preview ──────────────────────────────────

    @staticmethod
    def _stitch_pages(pages: List[np.ndarray]) -> np.ndarray:
        """
        Stitch multiple page images vertically into one tall image.
        Each page is scaled to a common width (max 1000 px) to keep
        memory reasonable. A thin gray separator is added between pages.
        """
        if not pages:
            return np.zeros((200, 400, 3), dtype=np.uint8)
        if len(pages) == 1:
            return pages[0]

        # Target width: cap at 1000px to keep stitched image manageable
        target_w = min(1000, max(p.shape[1] for p in pages))

        strips: List[np.ndarray] = []
        sep = np.full((8, target_w, 3), 180, dtype=np.uint8)  # gray separator

        for i, page in enumerate(pages):
            h, w = page.shape[:2]
            if w != target_w:
                new_h = max(1, int(h * target_w / w))
                page  = cv2.resize(page, (target_w, new_h), interpolation=cv2.INTER_AREA)
            strips.append(page)
            if i < len(pages) - 1:
                strips.append(sep)

        return np.vstack(strips)

    @staticmethod
    def _render_remaining_pages(
        renderer: PDFProcessor,
        detected_page: int,
        total_pages: int,
    ) -> List[np.ndarray]:
        """
        After early QR exit, render remaining pages at low DPI (72)
        so we can show the full document in the preview viewer.
        Fast — 72 DPI is just for display, not OCR.
        """
        if detected_page >= total_pages:
            return []

        remaining: List[np.ndarray] = []
        low_res = PDFProcessor(dpi=72)

        try:
            # Re-open the same file at low DPI
            if renderer.file_path:
                low_res.load_pdf(renderer.file_path)
                indices = list(range(detected_page, total_pages))
                for page_dict in low_res.extract_pages(page_indices=indices):
                    remaining.append(page_dict["image"])
        except Exception as exc:
            logger.debug("Could not render remaining pages for preview: %s", exc)
        finally:
            low_res.cleanup()

        return remaining

    # ── Enhanced QR detection (5 image variants) ─────────────────────────────

    @staticmethod
    def _detect_qr_enhanced(bgr: np.ndarray) -> bool:
        """
        Try QR detection on 5 preprocessed variants of the image.
        Only returns True if the decoded QR content is an Aadhaar QR.

        Aadhaar QR codes always contain XML with uid= / name= / dob= / gender=
        or a signed byte payload starting with Aadhaar-specific markers.

        Non-Aadhaar QR codes (UPI, GST invoices, vaccination certs, etc.)
        are decoded but rejected because they lack these markers.
        """
        det  = cv2.QRCodeDetector()
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        clahe    = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        _, binarized = cv2.threshold(
            enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        h, w     = gray.shape[:2]
        upscaled = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

        for img in (bgr, gray, enhanced, binarized, upscaled):
            try:
                data, points, _ = det.detectAndDecode(img)
                if points is None:
                    continue
                if data and AadhaarFileScanner._is_aadhaar_qr(data):
                    return True
                # QR found but not decoded (blurry) — treat as potential Aadhaar
                # only if no data could be extracted at all
                if not data:
                    # Try next variant — maybe a sharper one will decode it
                    continue
            except Exception:
                pass

        return False

    @staticmethod
    def _is_aadhaar_qr(data: str) -> bool:
        """
        Check if decoded QR content belongs to an Aadhaar card.

        Aadhaar QR markers:
          - XML attributes: uid=, name=, dob=, gender=, yob=
          - Tag names: PrintLetterBarcodeData, KycRes
          - Numeric-only payload starting with digit (new signed Aadhaar QR)
        """
        if not data:
            return False

        # New Aadhaar secure QR: binary/numeric payload, starts with digit,
        # very long (>100 chars), no URL-like content
        if data[:1].isdigit() and len(data) > 100 and "://" not in data:
            return True

        dl = data.lower()

        # Classic XML-based Aadhaar QR
        if "printletterbarcode" in dl:
            return True
        if "kycres" in dl:
            return True

        # XML attributes always present in Aadhaar QR
        aadhaar_attrs = ("uid=", "name=", "dob=", "gender=", "yob=", "co=", "vtc=")
        matches = sum(1 for attr in aadhaar_attrs if attr in dl)
        if matches >= 3:
            return True

        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _rot(img: np.ndarray, angle: int) -> np.ndarray:
        if angle == 0:
            return img
        m = {90: cv2.ROTATE_90_CLOCKWISE,
             180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}
        return cv2.rotate(img, m[angle])

    @staticmethod
    def _tess(img: np.ndarray, lang: str, cfg: str) -> str:
        try:
            import pytesseract
            return pytesseract.image_to_string(img, lang=lang, config=cfg)
        except Exception as exc:
            logger.debug("Tesseract (%s) error: %s", lang, exc)
            return ""
