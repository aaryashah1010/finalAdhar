"""
src/modules/ocr_engine/engine.py
──────────────────────────────────
EasyOCR wrapper for large-scale document text extraction.

Design goals:
    - Singleton: EasyOCR models loaded exactly once per process.
    - Multi-rotation: tries 0°/90°/180°/270° to handle skewed/sideways scans.
    - Early exit: stops rotation loop as soon as a high-confidence result is found.
    - run_ocr_with_boxes(): returns bounding boxes for the masking pipeline.
    - Privacy: OCR text is NEVER logged or persisted; only metrics are logged.

Why EasyOCR over PaddleOCR:
    - Works reliably on Windows CPU without platform-specific bugs.
    - Supports English + Hindi in one model (needed for "भारत सरकार" keyword).
    - Returns per-word bounding boxes used for automatic Aadhaar masking.
    - Simple pip install — no separate model download step required.

First run:
    EasyOCR will download ~100 MB of model weights to ~/.EasyOCR/model/
    on the very first call to get_instance().  Subsequent runs use the cache.
    Pass model_storage_directory to control the download location.

Privacy contract:
    ✓  confidence scores, rotation metadata, timing — safe to log
    ✗  OCR text content — NEVER logged, never stored, discarded after matching
"""

from __future__ import annotations

import logging
import threading
import time
from typing import ClassVar, List, Optional

import cv2
import numpy as np

from .models import OCRPageResult, RotationCandidate, TextBlock

logger = logging.getLogger(__name__)


# ── Tunable constants ─────────────────────────────────────────────────────────

#: Rotation angles tried in order.  0° is always first (most likely case).
ROTATION_ANGLES: tuple[int, ...] = (0, 90, 180, 270)

#: Scoring weights — must sum to 1.0.
CONFIDENCE_WEIGHT:  float = 0.7
TEXT_LENGTH_WEIGHT: float = 0.3

#: Early exit: stop if confidence exceeds this threshold AND text is non-trivial.
EARLY_EXIT_CONFIDENCE: float = 0.85
EARLY_EXIT_MIN_CHARS:  int   = 20

#: CLAHE parameters for local contrast enhancement.
CLAHE_CLIP_LIMIT: float           = 2.0
CLAHE_TILE_GRID:  tuple[int, int] = (8, 8)

#: Unsharp-mask sharpening kernel — amplifies text edges.
_SHARPEN_KERNEL: np.ndarray = np.array(
    [[-1, -1, -1],
     [-1,  9, -1],
     [-1, -1, -1]],
    dtype=np.float32,
)

#: Gaussian noise-reduction parameters (light — preserves text detail).
GAUSSIAN_KSIZE: tuple[int, int] = (3, 3)
GAUSSIAN_SIGMA: float            = 0.5

#: Maximum image dimension (pixels) sent to EasyOCR.
#: Larger images are downscaled proportionally to fit — saves memory and time.
#: Aadhaar card text is large enough that 1600px is more than sufficient.
MAX_IMAGE_DIM: int = 1600


# ── OCREngine ─────────────────────────────────────────────────────────────────

class OCREngine:
    """
    Singleton EasyOCR wrapper.

    Instantiation is forbidden — use OCREngine.get_instance().

    Example:
        engine = OCREngine.get_instance()
        result = engine.run_multi_rotation(page_image)
        # result: {"text": str, "confidence": float}

        blocks = engine.run_ocr_with_boxes(page_image)
        # blocks: List[TextBlock] — used by the masking pipeline
    """

    _instance:  ClassVar[Optional[OCREngine]] = None
    _init_lock: ClassVar[threading.Lock]      = threading.Lock()

    # ─────────────────────────────────────────────────────────────────────────
    # Singleton lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self) -> None:
        raise RuntimeError(
            "OCREngine cannot be instantiated directly. "
            "Call OCREngine.get_instance() instead."
        )

    @classmethod
    def get_instance(cls, model_dir: str = "./models") -> "OCREngine":
        """
        Return the process-wide OCREngine, creating it on the first call.

        Thread-safe via double-checked locking.

        Args:
            model_dir: Directory for EasyOCR model storage.
                       Defaults to ./models; EasyOCR will use ~/.EasyOCR
                       if this directory is not writable.

        Returns:
            The singleton OCREngine instance.
        """
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = cls.__new__(cls)
                    instance._initialize(model_dir)
                    cls._instance = instance
        return cls._instance

    def _initialize(self, model_dir: str) -> None:
        """
        Load EasyOCR models into memory.

        Downloads ~100 MB of model weights on first run; cached afterwards.
        Languages: English ('en') + Hindi ('hi') — needed for keyword detection.

        Args:
            model_dir: Directory to store/load model files.
        """
        import easyocr  # type: ignore[import]

        logger.info(
            "Initialising EasyOCR (en+hi) — first run downloads ~100 MB of models."
        )
        t0 = time.perf_counter()

        self._reader = easyocr.Reader(
            lang_list                = ["en", "hi"],
            gpu                      = False,
            model_storage_directory  = model_dir,
            download_enabled         = True,
            verbose                  = False,
        )

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("EasyOCR ready (%.0f ms).", elapsed)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def preprocess_image(self, image: np.ndarray) -> np.ndarray:
        """
        Apply a preprocessing pipeline to maximise OCR accuracy on scanned docs.

        Pipeline:
            1. Grayscale     — single luminance channel
            2. CLAHE         — local contrast enhancement for uneven lighting
            3. Sharpening    — 3×3 unsharp-mask kernel to enhance text edges
            4. Gaussian blur — light noise reduction
            5. Resize        — cap longest side at MAX_IMAGE_DIM for speed

        Args:
            image: uint8 numpy array, shape (H, W, 3) BGR or (H, W) grayscale.

        Returns:
            Preprocessed grayscale uint8 array.

        Raises:
            ValueError: If image has unsupported number of dimensions.
        """
        if image.ndim not in (2, 3):
            raise ValueError(
                f"Expected 2-D or 3-D image, got shape {image.shape}."
            )

        # Step 1: Grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()

        # Step 2: CLAHE
        clahe    = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_GRID)
        enhanced = clahe.apply(gray)

        # Step 3: Sharpen
        sharpened = cv2.filter2D(enhanced, ddepth=-1, kernel=_SHARPEN_KERNEL)

        # Step 4: Denoise
        denoised = cv2.GaussianBlur(sharpened, GAUSSIAN_KSIZE, sigmaX=GAUSSIAN_SIGMA)

        # Step 5: Resize — cap longest dimension to MAX_IMAGE_DIM
        h, w = denoised.shape[:2]
        longest = max(h, w)
        if longest > MAX_IMAGE_DIM:
            scale   = MAX_IMAGE_DIM / longest
            new_w   = int(w * scale)
            new_h   = int(h * scale)
            denoised = cv2.resize(denoised, (new_w, new_h), interpolation=cv2.INTER_AREA)

        return denoised

    def run_ocr(self, image: np.ndarray) -> dict:
        """
        Run EasyOCR on a single image and return text + confidence.

        Args:
            image: uint8 numpy array, preprocessed or raw.

        Returns:
            {"text": str, "confidence": float}
            Returns {"text": "", "confidence": 0.0} on empty pages or errors.
        """
        try:
            results = self._reader.readtext(image, detail=1)
        except Exception as exc:
            logger.warning("EasyOCR.readtext raised %s: %s", type(exc).__name__, exc)
            return {"text": "", "confidence": 0.0}

        return self._parse_results(results)

    def run_multi_rotation(self, image: np.ndarray) -> dict:
        """
        Try four rotations (0°, 90°, 180°, 270°) and return the best result.

        Algorithm:
            1. Preprocess image once.
            2. For each rotation angle:
               a. Rotate the preprocessed image.
               b. Run OCR.
               c. Early exit if confidence > EARLY_EXIT_CONFIDENCE and chars >= 20.
            3. Return candidate with highest composite score.

        Args:
            image: uint8 numpy array, shape (H, W) or (H, W, 3).

        Returns:
            {"text": str, "confidence": float}
        """
        t_start = time.perf_counter()

        preprocessed            = self.preprocess_image(image)
        candidates: List[RotationCandidate] = []

        for degrees in ROTATION_ANGLES:
            rotated = self._rotate_image(preprocessed, degrees)
            ocr_out = self.run_ocr(rotated)

            text  = ocr_out["text"]
            conf  = ocr_out["confidence"]
            chars = len(text)

            candidate = RotationCandidate(
                degrees        = degrees,
                text           = text,
                avg_confidence = conf,
                char_count     = chars,
            )
            candidates.append(candidate)

            logger.debug("Rotation %3d°: confidence=%.4f, chars=%d", degrees, conf, chars)

            if conf > EARLY_EXIT_CONFIDENCE and chars >= EARLY_EXIT_MIN_CHARS:
                elapsed_ms = (time.perf_counter() - t_start) * 1000
                logger.info(
                    "Early exit at %d° (conf=%.4f, chars=%d, elapsed=%.1f ms).",
                    degrees, conf, chars, elapsed_ms,
                )
                return {"text": text, "confidence": round(conf, 4)}

            del rotated

        best       = self._select_best(candidates)
        elapsed_ms = (time.perf_counter() - t_start) * 1000

        logger.info(
            "Best rotation: %d° (conf=%.4f, chars=%d, score=%.4f, elapsed=%.1f ms).",
            best.degrees, best.avg_confidence, best.char_count, best.score, elapsed_ms,
        )

        return {
            "text":       best.text,
            "confidence": round(best.avg_confidence, 4),
        }

    def run_ocr_with_boxes(self, image: np.ndarray) -> List[TextBlock]:
        """
        Run EasyOCR and return per-word bounding boxes.

        Used by the auto-masking pipeline to locate and redact Aadhaar numbers
        at their exact pixel positions on the page.

        The image is preprocessed and resized the same way as run_ocr() so
        that pixel coordinates are consistent with the preprocessed dimensions.
        Call this method with the RAW page image — preprocessing is applied
        internally.

        Args:
            image: Raw page image, uint8 numpy array (H, W, 3) BGR.

        Returns:
            List of TextBlock objects with pixel bounding boxes.
            Empty list if OCR finds nothing or an error occurs.
        """
        preprocessed = self.preprocess_image(image)

        try:
            results = self._reader.readtext(preprocessed, detail=1)
        except Exception as exc:
            logger.warning(
                "EasyOCR.readtext (with_boxes) raised %s: %s",
                type(exc).__name__, exc,
            )
            return []

        blocks: List[TextBlock] = []
        for item in results:
            try:
                pts, text, conf = item
                # pts: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]] — four corners
                xs = [int(p[0]) for p in pts]
                ys = [int(p[1]) for p in pts]
                blocks.append(TextBlock(
                    text       = str(text).strip(),
                    x0         = min(xs),
                    y0         = min(ys),
                    x1         = max(xs),
                    y1         = max(ys),
                    confidence = float(conf),
                ))
            except (TypeError, ValueError, IndexError):
                continue

        # Scale coords back to original image size if we resized during preprocess
        orig_h, orig_w = image.shape[:2]
        pre_h, pre_w   = preprocessed.shape[:2]

        if orig_h != pre_h or orig_w != pre_w:
            scale_x = orig_w / pre_w
            scale_y = orig_h / pre_h
            scaled: List[TextBlock] = []
            for b in blocks:
                scaled.append(TextBlock(
                    text       = b.text,
                    x0         = int(b.x0 * scale_x),
                    y0         = int(b.y0 * scale_y),
                    x1         = int(b.x1 * scale_x),
                    y1         = int(b.y1 * scale_y),
                    confidence = b.confidence,
                ))
            return scaled

        return blocks

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: image helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _rotate_image(image: np.ndarray, degrees: int) -> np.ndarray:
        if degrees == 0:
            return image
        _MAP = {
            90:  cv2.ROTATE_90_CLOCKWISE,
            180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE,
        }
        flag = _MAP.get(degrees)
        if flag is None:
            raise ValueError(f"Unsupported rotation angle: {degrees}.")
        return cv2.rotate(image, flag)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: EasyOCR result parsing
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_results(results) -> dict:
        """
        Parse EasyOCR output list into {"text": str, "confidence": float}.

        EasyOCR detail=1 format:
            [ ([[x1,y1],[x2,y1],[x2,y2],[x1,y2]], text, confidence), ... ]
        """
        if not results:
            return {"text": "", "confidence": 0.0}

        lines: list[str]   = []
        confs: list[float] = []

        for item in results:
            try:
                _pts, text, conf = item
                text = str(text).strip()
                if text:
                    lines.append(text)
                    confs.append(float(conf))
            except (TypeError, ValueError):
                continue

        if not lines:
            return {"text": "", "confidence": 0.0}

        avg_conf = sum(confs) / len(confs)
        return {
            "text":       "\n".join(lines),
            "confidence": round(avg_conf, 4),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: rotation scoring
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _select_best(candidates: List[RotationCandidate]) -> RotationCandidate:
        """
        Score all rotation candidates and return the highest-scoring one.

        Formula: score = (avg_confidence × 0.7) + (normalised_text_length × 0.3)
        """
        if not candidates:
            return RotationCandidate(degrees=0, text="", avg_confidence=0.0, char_count=0)

        max_chars = max(c.char_count for c in candidates)
        if max_chars == 0:
            return candidates[0]

        for c in candidates:
            norm_length = c.char_count / max_chars
            c.score = (
                c.avg_confidence * CONFIDENCE_WEIGHT +
                norm_length      * TEXT_LENGTH_WEIGHT
            )

        return max(candidates, key=lambda c: c.score)
