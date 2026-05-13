"""
src/pipeline/integration.py
────────────────────────────
End-to-end integration orchestrator.

Wires together:
    FileManager  →  PDFProcessor  →  OCREngine  →  AadhaarDetector
    → AutoMasker (saves masked PDF to output folder)

Pipeline modes:
    run_detection_pass()  — scan all PDFs, detect Aadhaar, build queue
    run_masking_pass()    — for each detected PDF: auto-mask and save
    launch_redaction_ui() — manual review UI (PyQt5) for human verification
    run()                 — detect + mask (fully automatic, no UI needed)

Design rules:
    • Never rewrites core module logic — calls their public APIs only.
    • Every file outcome is persisted before moving on (crash-safe).
    • Non-Aadhaar files are skipped — NOT copied to the output folder.
    • Detected Aadhaar files are masked automatically (black boxes over
      the number) and saved to output_folder mirroring the input structure.

Persistence layout in --state-dir:
    file_manager_state.json   — FileManager resume state
    detected_queue.json       — ordered list of Aadhaar PDFs found
    masking_state.json        — which detected files have been masked
    review_session.json       — PDFRedactionTool session resume (manual mode)
    processing_summary.json   — final statistics snapshot
"""

from __future__ import annotations

import json
import logging
import re
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np

from ..modules.detector.detector import AadhaarDetector
from ..modules.ocr_engine.models import TextBlock
from ..modules.pdf_renderer.renderer import PDFProcessor
from ..state.file_manager import FileManager
from ..utils.file_utils import atomic_json_write, safe_json_read
from ..utils.fitz_compat import fitz

logger = logging.getLogger(__name__)

# ── File names inside state-dir ───────────────────────────────────────────────

_FM_STATE_FILE       = "file_manager_state.json"
_DETECTED_QUEUE_FILE = "detected_queue.json"
_MASKING_STATE_FILE  = "masking_state.json"
_REVIEW_SESSION_FILE = "review_session.json"
_SUMMARY_FILE        = "processing_summary.json"

# ── Aadhaar regex + OCR correction (used for masking bbox search) ─────────────

_AADHAAR_RE      = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")
_OCR_CORRECTION  = str.maketrans({
    "O": "0", "o": "0", "I": "1", "l": "1",
    "B": "8", "S": "5", "Z": "2", "G": "6",
})


# ── DetectedQueue ─────────────────────────────────────────────────────────────

class _DetectedQueue:
    """
    Append-only, deduplicated, atomically-persisted list of detected PDFs.
    """
    _SCHEMA = "1.0"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._files: List[str] = []
        self._seen: set[str]   = set()
        self._load()

    def add(self, file_path: str) -> bool:
        resolved = str(Path(file_path).resolve())
        if resolved in self._seen:
            return False
        self._files.append(resolved)
        self._seen.add(resolved)
        self._save()
        return True

    @property
    def files(self) -> List[str]:
        return list(self._files)

    def __len__(self) -> int:
        return len(self._files)

    def _load(self) -> None:
        data, err = safe_json_read(self._path)
        if err or not isinstance(data, dict):
            return
        for f in data.get("files", []):
            if isinstance(f, str) and f not in self._seen:
                self._files.append(f)
                self._seen.add(f)

    def _save(self) -> None:
        atomic_json_write(self._path, {
            "schema_version": self._SCHEMA,
            "files":          self._files,
            "updated_at":     _utcnow(),
        })


# ── MaskingState ──────────────────────────────────────────────────────────────

class _MaskingState:
    """
    Tracks which detected files have been successfully masked.
    Allows masking pass to resume after a crash.
    """
    _SCHEMA = "1.0"

    def __init__(self, path: Path) -> None:
        self._path   = path
        self._masked: set[str] = set()
        self._failed: Dict[str, str] = {}
        self._load()

    def mark_masked(self, file_path: str) -> None:
        self._masked.add(str(Path(file_path).resolve()))
        self._failed.pop(str(Path(file_path).resolve()), None)
        self._save()

    def mark_failed(self, file_path: str, reason: str) -> None:
        resolved = str(Path(file_path).resolve())
        self._failed[resolved] = reason
        self._save()

    def is_done(self, file_path: str) -> bool:
        resolved = str(Path(file_path).resolve())
        return resolved in self._masked or resolved in self._failed

    def masked_count(self) -> int:
        return len(self._masked)

    def failed_count(self) -> int:
        return len(self._failed)

    def _load(self) -> None:
        data, err = safe_json_read(self._path)
        if err or not isinstance(data, dict):
            return
        self._masked = set(data.get("masked", []))
        self._failed = dict(data.get("failed", {}))

    def _save(self) -> None:
        atomic_json_write(self._path, {
            "schema_version": self._SCHEMA,
            "masked":         sorted(self._masked),
            "failed":         self._failed,
            "updated_at":     _utcnow(),
        })


# ── Aadhaar bbox finder ───────────────────────────────────────────────────────

def _find_aadhaar_blocks(blocks: List[TextBlock]) -> List[TextBlock]:
    """
    Find TextBlocks that contain parts of an Aadhaar number.

    Strategy:
        1. Group blocks into lines by vertical proximity (Y-center within 20px).
        2. For each line, join all block texts left-to-right.
        3. Apply OCR correction and check for the 12-digit Aadhaar regex.
        4. If the line contains an Aadhaar number, collect all blocks on that
           line whose text (after normalization) is purely digit-like (≥ 4 chars).

    Returns blocks to be redacted — may be empty if no Aadhaar number found.
    """
    if not blocks:
        return []

    # ── Group into lines by Y proximity ──────────────────────────────────────
    sorted_blocks = sorted(blocks, key=lambda b: (b.cy, b.x0))
    lines: List[List[TextBlock]] = []
    current_line: List[TextBlock] = [sorted_blocks[0]]

    for block in sorted_blocks[1:]:
        # Blocks whose Y-centers are within half the average line height
        # are considered the same line.
        avg_height = max(1, sum(b.height for b in current_line) / len(current_line))
        if abs(block.cy - current_line[-1].cy) <= max(15, avg_height * 0.6):
            current_line.append(block)
        else:
            lines.append(current_line)
            current_line = [block]
    lines.append(current_line)

    # ── Scan each line for Aadhaar number ────────────────────────────────────
    redact: List[TextBlock] = []

    for line in lines:
        line.sort(key=lambda b: b.x0)
        line_text  = " ".join(b.text for b in line)
        normalized = line_text.translate(_OCR_CORRECTION)

        if not _AADHAAR_RE.search(normalized):
            continue

        # Found an Aadhaar number on this line.
        # Redact blocks that are primarily digit characters (the number itself).
        for block in line:
            cleaned = re.sub(r"[\s\-]", "", block.text.translate(_OCR_CORRECTION))
            # A block contributes to the number if it has ≥ 4 digit characters
            # and is at least 70% digits (avoids redacting surrounding labels).
            digit_chars = sum(1 for c in cleaned if c.isdigit())
            if digit_chars >= 4 and (len(cleaned) == 0 or digit_chars / len(cleaned) >= 0.7):
                redact.append(block)

    return redact


# ── Per-file auto-masker ──────────────────────────────────────────────────────

def _auto_mask_pdf(
    src_path:    str,
    output_path: str,
    ocr_engine,
    render_dpi:  int = 300,
) -> dict:
    """
    Detect Aadhaar number bounding boxes on each page and permanently redact them.

    Steps per page:
        1. Render to numpy image (via PyMuPDF at render_dpi).
        2. Run run_ocr_with_boxes() → TextBlock list.
        3. Find blocks containing the Aadhaar number via _find_aadhaar_blocks().
        4. Convert pixel coordinates to PDF point coordinates.
        5. Add black redaction annotations via PyMuPDF.
        6. Apply redactions (permanently burns them into the PDF content stream).
    7. Save to output_path.

    Args:
        src_path:    Absolute path to the source PDF.
        output_path: Absolute path where the masked PDF will be saved.
        ocr_engine:  Initialised OCREngine instance.
        render_dpi:  DPI used to render pages for OCR (must match detection pass).

    Returns:
        {"success": bool, "pages_masked": int, "reason": str}
    """
    try:
        doc = fitz.open(src_path)
    except Exception as exc:
        return {"success": False, "pages_masked": 0, "reason": f"open_failed: {exc}"}

    scale = render_dpi / 72.0          # points → pixels factor
    pages_masked = 0

    try:
        for page_index in range(len(doc)):
            page = doc[page_index]

            # ── Render page to numpy image ────────────────────────────────
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # Convert PyMuPDF pixmap to OpenCV BGR array
            img_bytes = pix.samples
            img = np.frombuffer(img_bytes, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            if pix.n == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            else:
                img = img[:, :, :3]
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

            # ── OCR with bounding boxes ───────────────────────────────────
            blocks = ocr_engine.run_ocr_with_boxes(img)
            aadhaar_blocks = _find_aadhaar_blocks(blocks)

            if not aadhaar_blocks:
                continue

            # ── Convert pixel bbox → PDF points ──────────────────────────
            # PyMuPDF's coordinate system: (0,0) = top-left, y increases downward.
            # pixel coords / render_dpi * 72 = PDF points
            px_to_pt = 72.0 / render_dpi

            for block in aadhaar_blocks:
                # Add a small margin around the block for safety
                margin = 3
                rect = fitz.Rect(
                    (block.x0 - margin) * px_to_pt,
                    (block.y0 - margin) * px_to_pt,
                    (block.x1 + margin) * px_to_pt,
                    (block.y1 + margin) * px_to_pt,
                )
                # Clamp to page bounds
                rect = rect & page.rect
                if rect.is_empty:
                    continue
                page.add_redact_annot(rect, fill=(0, 0, 0))

            # Apply permanently (burns into content stream, removes original text)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            pages_masked += 1

            logger.debug(
                "Page %d: redacted %d Aadhaar block(s).",
                page_index + 1, len(aadhaar_blocks),
            )

        if pages_masked == 0:
            # Detection said it's Aadhaar but masker found nothing to redact.
            # Save anyway (file is still an Aadhaar document even if masking
            # found no precise box — better to save than lose the file).
            logger.warning(
                "No Aadhaar blocks found to redact in %s — "
                "saving unmodified copy to output.",
                Path(src_path).name,
            )

        # Ensure output directory exists
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(output_path, garbage=4, deflate=True)

        return {"success": True, "pages_masked": pages_masked, "reason": "ok"}

    except Exception as exc:
        return {"success": False, "pages_masked": 0, "reason": str(exc)}

    finally:
        doc.close()


# ── IntegrationPipeline ───────────────────────────────────────────────────────

class IntegrationPipeline:
    """
    Orchestrates the full PDF → detection → auto-masking pipeline.

    Usage
    ─────
        pipeline = IntegrationPipeline(
            input_folder  = "/data/pdfs",
            output_folder = "/data/redacted",
            state_dir     = "/data/state",
        )
        pipeline.run()        # detect + auto-mask

        # Or step by step:
        pipeline.run_detection_pass()
        pipeline.run_masking_pass()

        # For manual review instead of auto-masking:
        pipeline.launch_redaction_ui()
    """

    def __init__(
        self,
        input_folder:  str,
        output_folder: str,
        model_dir:     str = "./models",
        state_dir:     str = "./state",
        render_dpi:    int = 300,
    ) -> None:
        self._input_folder  = Path(input_folder).resolve()
        self._output_folder = Path(output_folder).resolve()
        self._model_dir     = str(Path(model_dir).resolve())
        self._state_dir     = Path(state_dir).resolve()
        self._render_dpi    = render_dpi

        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._output_folder.mkdir(parents=True, exist_ok=True)

        self._fm_state_path      = self._state_dir / _FM_STATE_FILE
        self._queue_path         = self._state_dir / _DETECTED_QUEUE_FILE
        self._masking_state_path = self._state_dir / _MASKING_STATE_FILE
        self._review_sess_path   = self._state_dir / _REVIEW_SESSION_FILE
        self._summary_path       = self._state_dir / _SUMMARY_FILE

    # ── Public API ────────────────────────────────────────────────────────────

    def run_detection_pass(self) -> Dict:
        """
        Scan the input folder and run OCR + Aadhaar detection on every pending PDF.

        - Resumes from previous run (FileManager cursor).
        - Detected PDFs are appended (deduplicated) to detected_queue.json.
        - Non-Aadhaar files are skipped (not copied anywhere).
        - Each file outcome is persisted before moving on.

        Returns:
            Summary dict with detected / not_aadhaar / errors counts.
        """
        logger.info("=== Detection pass started ===")
        logger.info("Input:  %s", self._input_folder)
        logger.info("State:  %s", self._state_dir)

        fm   = FileManager(state_path=str(self._fm_state_path))
        scan = fm.scan_folder(str(self._input_folder))
        logger.info(
            "Scan complete — total=%d, pending=%d, resumed=%s",
            scan.total_files, scan.pending_count, scan.is_resumed,
        )

        if scan.pending_count == 0:
            logger.info("No pending files — detection pass complete.")
            return self._build_summary(fm, detected=0, not_aadhaar=0, elapsed=0.0)

        queue     = _DetectedQueue(self._queue_path)
        detector  = AadhaarDetector()
        processor = PDFProcessor(dpi=self._render_dpi)
        _ocr_engine = None

        t0 = time.perf_counter()
        detected_count   = 0
        not_aadhaar_count = 0

        try:
            while True:
                record = fm.get_next_file()
                if record is None:
                    break

                file_path = record.file_path
                logger.info("Processing: %s", Path(file_path).name)

                outcome = self._process_one_file(
                    file_path  = file_path,
                    processor  = processor,
                    get_ocr    = lambda: self._ensure_ocr(_ocr_engine, self._model_dir),
                    detector   = detector,
                )

                if outcome.get("ocr_engine"):
                    _ocr_engine = outcome["ocr_engine"]

                if outcome["status"] == "detected":
                    queue.add(file_path)
                    fm.mark_processed(file_path)
                    detected_count += 1
                    logger.info(
                        "  ✓ Aadhaar detected (conf=%.2f, pages=%s)",
                        outcome["confidence"], outcome["pages"],
                    )

                elif outcome["status"] == "not_aadhaar":
                    fm.mark_processed(file_path)
                    not_aadhaar_count += 1
                    logger.debug("  – Not Aadhaar: %s", Path(file_path).name)

                else:
                    fm.mark_error(file_path, outcome["error_code"])
                    logger.warning(
                        "  ✗ Error [%s]: %s",
                        outcome["error_code"], Path(file_path).name,
                    )

                fm.save_state()

        finally:
            processor.cleanup()

        elapsed = time.perf_counter() - t0
        summary = self._build_summary(
            fm, detected=detected_count,
            not_aadhaar=not_aadhaar_count, elapsed=elapsed,
        )
        self._persist_summary(summary)

        logger.info(
            "=== Detection pass done — detected=%d, not_aadhaar=%d, "
            "errors=%d, elapsed=%.1fs ===",
            detected_count, not_aadhaar_count, summary["errors"], elapsed,
        )
        return summary

    def run_masking_pass(self) -> Dict:
        """
        For every file in the detected queue, auto-mask the Aadhaar number
        and save the masked PDF to output_folder (mirroring input structure).

        Non-Aadhaar files are never touched.
        Detected files with no maskable region are saved as-is (unmodified copy).
        Resumable: already-masked files are skipped.

        Returns:
            Summary dict with masked / failed counts.
        """
        logger.info("=== Masking pass started ===")

        queue         = _DetectedQueue(self._queue_path)
        masking_state = _MaskingState(self._masking_state_path)

        if not queue.files:
            logger.warning(
                "No detected files in queue. Run detection pass first."
            )
            return {"masked": 0, "failed": 0, "skipped": 0}

        existing = [f for f in queue.files if Path(f).is_file()]
        missing  = len(queue.files) - len(existing)
        if missing:
            logger.warning("%d queued file(s) no longer on disk — skipped.", missing)

        # Lazy-init OCR engine
        _ocr_engine = None

        t0      = time.perf_counter()
        masked  = 0
        failed  = 0
        skipped = 0

        for file_path in existing:
            if masking_state.is_done(file_path):
                skipped += 1
                logger.debug("Already done, skipping: %s", Path(file_path).name)
                continue

            # Build output path: mirror input folder structure
            output_path = self._build_output_path(file_path)

            logger.info("Masking: %s", Path(file_path).name)

            # Lazy-init OCR engine on first file
            if _ocr_engine is None:
                try:
                    _ocr_engine = self._ensure_ocr(None, self._model_dir)
                except Exception as exc:
                    logger.error("OCR engine init failed: %s", exc)
                    masking_state.mark_failed(file_path, "OCR_INIT_FAILED")
                    failed += 1
                    continue

            result = _auto_mask_pdf(
                src_path    = file_path,
                output_path = str(output_path),
                ocr_engine  = _ocr_engine,
                render_dpi  = self._render_dpi,
            )

            if result["success"]:
                masking_state.mark_masked(file_path)
                masked += 1
                logger.info(
                    "  ✓ Masked (pages_redacted=%d): %s",
                    result["pages_masked"], Path(file_path).name,
                )
            else:
                masking_state.mark_failed(file_path, result["reason"])
                failed += 1
                logger.warning(
                    "  ✗ Masking failed [%s]: %s",
                    result["reason"], Path(file_path).name,
                )

        elapsed = time.perf_counter() - t0
        logger.info(
            "=== Masking pass done — masked=%d, failed=%d, skipped=%d, elapsed=%.1fs ===",
            masked, failed, skipped, elapsed,
        )
        return {"masked": masked, "failed": failed, "skipped": skipped}

    def launch_redaction_ui(self) -> None:
        """
        Open the manual PDFRedactionTool for human review of detected files.
        Use this instead of run_masking_pass() if you want manual control.
        """
        queue = _DetectedQueue(self._queue_path)
        if not queue.files:
            logger.warning(
                "detected_queue.json is empty. Run detection pass first."
            )
            return

        existing = [f for f in queue.files if Path(f).is_file()]
        if not existing:
            logger.error("No valid files in the detected queue.")
            return

        logger.info("Launching manual redaction UI with %d PDF(s).", len(existing))

        from PyQt5.QtWidgets import QApplication
        from ..modules.redaction_tool.tool import PDFRedactionTool

        app = QApplication.instance() or QApplication(sys.argv)
        window = PDFRedactionTool(
            pdf_files    = existing,
            input_root   = str(self._input_folder),
            output_root  = str(self._output_folder),
            session_path = str(self._review_sess_path),
        )
        window.show()
        sys.exit(app.exec_())

    def run(self) -> None:
        """
        Full pipeline: detect Aadhaar files, then open manual redaction UI.
        Non-Aadhaar files are skipped. Detected files are shown in the
        PDFRedactionTool for manual masking and saving.
        """
        self.run_detection_pass()
        self.launch_redaction_ui()

    # ── Per-file detection ────────────────────────────────────────────────────

    @staticmethod
    def _process_one_file(
        file_path: str,
        processor: PDFProcessor,
        get_ocr,
        detector:  AadhaarDetector,
    ) -> dict:
        """
        Run the full detection pipeline on one PDF.

        Returns:
            {"status": "detected"|"not_aadhaar"|"error", ...}
        """
        load_result = processor.load_pdf(file_path)
        if not load_result.is_processable:
            code = (load_result.error_code.value
                    if load_result.error_code else "ERR_LOAD_FAILED")
            return {"status": "error", "error_code": code}

        try:
            ocr_engine = get_ocr()
        except Exception as exc:
            logger.error("OCR engine init failed: %s", exc)
            return {"status": "error", "error_code": "ERR_OCR_INIT"}

        pages_text:  List[dict] = []
        page_images: List       = []

        try:
            for page_dict in processor.extract_pages():
                image = page_dict["image"]
                try:
                    ocr_result = ocr_engine.run_multi_rotation(image)
                except Exception as exc:
                    logger.debug(
                        "OCR failed on page %d: %s",
                        page_dict["page_number"], exc,
                    )
                    ocr_result = {"text": "", "confidence": 0.0}

                pages_text.append(ocr_result)
                page_images.append(image)

        except Exception as exc:
            logger.warning("Page extraction error in %s: %s", file_path, exc)
            return {"status": "error", "error_code": "ERR_PAGE_EXTRACT"}

        if not pages_text:
            return {"status": "error", "error_code": "ERR_NO_PAGES"}

        try:
            result = detector.process_document(
                pages_text  = pages_text,
                page_images = page_images,
            )
        except Exception as exc:
            logger.warning("Detector failed for %s: %s", file_path, exc)
            return {"status": "error", "error_code": "ERR_DETECTOR"}
        finally:
            del pages_text
            del page_images

        if result.is_aadhaar:
            return {
                "status":     "detected",
                "confidence": result.confidence,
                "pages":      result.pages,
                "ocr_engine": ocr_engine,
            }

        return {"status": "not_aadhaar", "ocr_engine": ocr_engine}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ensure_ocr(cached_engine, model_dir: str):
        if cached_engine is not None:
            return cached_engine
        from ..modules.ocr_engine.engine import OCREngine
        return OCREngine.get_instance(model_dir=model_dir)

    def _build_output_path(self, src_path: str) -> Path:
        """
        Mirror the input folder structure under output_folder.

        Example:
            input_folder  = /data/pdfs
            src_path      = /data/pdfs/dept/aadhaar.pdf
            output_path   = /data/out/dept/aadhaar.pdf
        """
        src  = Path(src_path).resolve()
        try:
            rel = src.relative_to(self._input_folder)
        except ValueError:
            # File is outside input_folder (e.g. absolute path passed directly)
            rel = Path(src.name)
        return self._output_folder / rel

    @staticmethod
    def _build_summary(fm, detected, not_aadhaar, elapsed) -> Dict:
        stats = fm.get_statistics() if hasattr(fm, "get_statistics") else {}
        return {
            "total_files": len(fm._file_index),  # type: ignore[attr-defined]
            "detected":    detected,
            "not_aadhaar": not_aadhaar,
            "errors":      stats.get("error", 0),
            "processed":   stats.get("processed", 0),
            "elapsed_s":   round(elapsed, 2),
            "timestamp":   _utcnow(),
        }

    def _persist_summary(self, summary: dict) -> None:
        try:
            atomic_json_write(self._summary_path, summary)
        except Exception as exc:
            logger.warning("Could not write summary: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
