"""
src/review_ui.py
────────────────
One-by-one Aadhaar review UI.

Flow:
  1. Background thread scans PDFs continuously.
  2. When Aadhaar is found → added to review queue → UI shows it immediately.
  3. Non-Aadhaar files → silently skipped.
  4. User sees: PDF page preview + Keep / Delete buttons.
  5. Clicking Keep or Delete → next queued file shown instantly.
  6. Scanner keeps running while user reviews (no wait).
  7. Delete = move file to output folder immediately.
"""
from __future__ import annotations

import logging
import os
import json
import shutil
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, List, Optional

import cv2
import numpy as np

from PyQt5.QtCore    import QMutex, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui     import QColor, QFont, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from src.aadhaar_detector import AadhaarFileScanner, DetectionResult
from src.utils.file_utils import atomic_json_write, safe_json_read

logger = logging.getLogger(__name__)

MASKED_KEEP_MANIFEST = "_masked_keep_manifest.json"
MASKED_KEEP_TMP_DIR = ".masked_keep_tmp"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bgr_to_pixmap(bgr: np.ndarray) -> QPixmap:
    """Convert a BGR numpy image to a QPixmap for display."""
    rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    qi   = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
    return QPixmap.fromImage(qi.copy())   # .copy() detaches from numpy buffer


# ── Background scanner thread ─────────────────────────────────────────────────

class ScanWorker(QThread):
    """
    Scans PDFs one by one in a background thread.

    Signals:
        aadhaar_found(DetectionResult) — emitted for every Aadhaar file.
        progress(current, total, filename) — emitted for every file scanned.
        scan_complete(total_scanned, total_aadhaar) — emitted when done.
    """
    aadhaar_found = pyqtSignal(object)        # DetectionResult
    progress      = pyqtSignal(int, int, str) # current, total, name
    scan_complete = pyqtSignal(int, int)      # scanned, aadhaar_count

    def __init__(self, pdf_paths: List[Path], dpi: int = 150) -> None:
        super().__init__()
        self._paths   = pdf_paths
        self._scanner = AadhaarFileScanner(render_dpi=dpi)

    def run(self) -> None:
        total         = len(self._paths)
        aadhaar_count = 0

        for i, path in enumerate(self._paths, start=1):
            self.progress.emit(i, total, path.name)
            result = self._scanner.scan(path)

            if result.is_aadhaar:
                aadhaar_count += 1
                logger.info("Aadhaar: %s (conf=%.2f)", path.name, result.confidence)
                self.aadhaar_found.emit(result)
            elif result.is_uncertain:
                logger.info("Uncertain (blurry): %s", path.name)
                self.aadhaar_found.emit(result)
            else:
                logger.info("Not Aadhaar: %s", path.name)

        self.scan_complete.emit(total, aadhaar_count)


# ── Review window ─────────────────────────────────────────────────────────────

_CONF_COLORS = {
    "HIGH":      "#e67e22",
    "MEDIUM":    "#3498db",
    "LOW":       "#95a5a6",
    "UNCERTAIN": "#f39c12",
}


class ReviewWindow(QMainWindow):
    """
    Main review window.

    Shows one detected Aadhaar file at a time with a PDF page preview.
    Scanning continues in the background while the user reviews.
    """

    def __init__(self, input_folder: Path, output_folder: Path) -> None:
        super().__init__()
        self.input_folder  = input_folder
        self.output_folder = output_folder

        # Review queue: DetectionResult objects waiting to be shown
        self._queue: Deque[DetectionResult] = deque()
        self._current: Optional[DetectionResult] = None
        self._mask_window: Optional[QMainWindow] = None
        self._mask_save_completed = False

        # Counters
        self._total_scanned  = 0
        self._total_aadhaar  = 0
        self._scan_done      = False

        self.setWindowTitle("Aadhaar Card Detector")
        self.setMinimumSize(860, 700)

        self._build_ui()
        self._start_scan()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(8)
        vbox.setContentsMargins(14, 14, 14, 14)

        # ── Top bar ───────────────────────────────────────────────────────
        self._status = QLabel("Initialising scanner…")
        self._status.setFont(QFont("Segoe UI", 10))
        vbox.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setValue(0)
        vbox.addWidget(self._progress)

        # ── File info bar ─────────────────────────────────────────────────
        info_row = QHBoxLayout()
        self._filename_label = QLabel("")
        self._filename_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self._filename_label.setWordWrap(True)
        info_row.addWidget(self._filename_label, 1)

        self._conf_badge = QLabel("")
        self._conf_badge.setFixedWidth(80)
        self._conf_badge.setAlignment(Qt.AlignCenter)
        self._conf_badge.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._conf_badge.setStyleSheet("border-radius: 4px; padding: 3px;")
        info_row.addWidget(self._conf_badge)
        vbox.addLayout(info_row)

        self._reason_label = QLabel("")
        self._reason_label.setStyleSheet("color: #555;")
        self._reason_label.setWordWrap(True)
        vbox.addWidget(self._reason_label)

        # ── PDF page viewer ───────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.StyledPanel)
        self._scroll.setMinimumHeight(350)

        self._img_label = QLabel("No file selected yet.")
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._scroll.setWidget(self._img_label)
        vbox.addWidget(self._scroll, 1)

        # ── Keep / Delete buttons ─────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(20)

        self._keep_btn = QPushButton("✓  Keep")
        self._keep_btn.setFixedHeight(44)
        self._keep_btn.setFont(QFont("Segoe UI", 12))
        self._keep_btn.setStyleSheet(
            "background:#27ae60; color:white; border-radius:6px;"
        )
        self._keep_btn.setEnabled(False)
        self._keep_btn.clicked.connect(self._on_keep)
        btn_row.addWidget(self._keep_btn)

        self._mask_keep_btn = QPushButton("Mask & Keep")
        self._mask_keep_btn.setFixedHeight(44)
        self._mask_keep_btn.setFont(QFont("Segoe UI", 12))
        self._mask_keep_btn.setStyleSheet(
            "background:#2980b9; color:white; border-radius:6px;"
        )
        self._mask_keep_btn.setEnabled(False)
        self._mask_keep_btn.clicked.connect(self._on_mask_keep)
        btn_row.addWidget(self._mask_keep_btn)

        self._delete_btn = QPushButton("🗑  Delete")
        self._delete_btn.setFixedHeight(44)
        self._delete_btn.setFont(QFont("Segoe UI", 12))
        self._delete_btn.setStyleSheet(
            "background:#e74c3c; color:white; border-radius:6px;"
        )
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn)

        vbox.addLayout(btn_row)

        # ── Bottom bar ────────────────────────────────────────────────────
        bottom = QHBoxLayout()
        self._queue_label = QLabel("")
        self._queue_label.setStyleSheet("color:#777; font-size:9pt;")
        bottom.addWidget(self._queue_label, 1)

        out_label = QLabel("Output:")
        bottom.addWidget(out_label)
        self._out_path = QLabel(str(self.output_folder))
        self._out_path.setStyleSheet("color:#2980b9;")
        bottom.addWidget(self._out_path)
        change_btn = QPushButton("Change")
        change_btn.setFixedWidth(70)
        change_btn.clicked.connect(self._change_output)
        bottom.addWidget(change_btn)
        vbox.addLayout(bottom)

    # ── Scanning ──────────────────────────────────────────────────────────────

    def _start_scan(self) -> None:
        pdfs = sorted(self.input_folder.rglob("*.pdf"))
        if not pdfs:
            self._status.setText("No PDF files found in the selected folder.")
            return

        self._progress.setMaximum(len(pdfs))
        self._worker = ScanWorker(pdfs)
        self._worker.progress.connect(self._on_progress)
        self._worker.aadhaar_found.connect(self._on_aadhaar_found)
        self._worker.scan_complete.connect(self._on_scan_complete)
        self._worker.start()

    def _on_progress(self, current: int, total: int, name: str) -> None:
        self._total_scanned = current
        self._progress.setValue(current)
        self._status.setText(
            f"Scanning {current}/{total}: {name}  "
            f"({self._total_aadhaar} Aadhaar found so far)"
        )
        self._update_queue_label()

    def _on_aadhaar_found(self, result: DetectionResult) -> None:
        self._total_aadhaar += 1
        self._queue.append(result)
        if self._current is None:
            self._show_next()

    def _on_scan_complete(self, total: int, aadhaar: int) -> None:
        self._scan_done = True
        self._status.setText(
            f"Scan complete — {total} file(s) scanned, "
            f"{aadhaar} Aadhaar card(s) found."
        )
        self._progress.setValue(self._progress.maximum())
        if self._current is None and not self._queue:
            self._show_idle("All done! No more files to review.")

    # ── Viewer ────────────────────────────────────────────────────────────────

    def _show_next(self) -> None:
        if not self._queue:
            self._current = None
            if self._scan_done:
                self._show_idle("All done! No more files to review.")
            else:
                self._show_idle("Waiting for scanner…")
            return

        result = self._queue.popleft()
        self._current = result
        self._update_queue_label()

        # File name + confidence badge
        self._filename_label.setText(result.path.name)
        label = result.confidence_label
        color = _CONF_COLORS.get(label, "#95a5a6")
        self._conf_badge.setText(label)
        self._conf_badge.setStyleSheet(
            f"background:{color}; color:white; border-radius:4px; padding:3px;"
        )
        if result.is_uncertain:
            self._reason_label.setText(
                "⚠ Image too blurry to detect automatically — is this an Aadhaar card?"
            )
        else:
            self._reason_label.setText(f"Detected by: {result.reason_str}")

        # PDF page image
        if result.preview_image is not None:
            self._display_image(result.preview_image)
        else:
            self._img_label.setText("(No preview available)")

        self._keep_btn.setEnabled(True)
        self._mask_keep_btn.setEnabled(True)
        self._delete_btn.setEnabled(True)

    def _display_image(self, bgr: np.ndarray) -> None:
        pixmap = _bgr_to_pixmap(bgr)
        # Scale to fit the scroll area width while keeping aspect ratio
        avail_w = max(self._scroll.width() - 20, 400)
        scaled  = pixmap.scaledToWidth(avail_w, Qt.SmoothTransformation)
        self._img_label.setPixmap(scaled)
        self._img_label.adjustSize()

    def _show_idle(self, message: str) -> None:
        self._filename_label.setText(message)
        self._conf_badge.setText("")
        self._conf_badge.setStyleSheet("")
        self._reason_label.setText("")
        self._img_label.setText("")
        self._img_label.setPixmap(QPixmap())
        self._keep_btn.setEnabled(False)
        self._mask_keep_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)

    def _update_queue_label(self) -> None:
        q = len(self._queue)
        if q > 0:
            self._queue_label.setText(f"{q} more Aadhaar file(s) waiting for review")
        else:
            self._queue_label.setText("")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_keep(self) -> None:
        if self._current:
            logger.info("Kept: %s", self._current.path.name)
        self._current = None
        self._keep_btn.setEnabled(False)
        self._mask_keep_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._show_next()

    def _on_delete(self) -> None:
        if not self._current:
            return
        result = self._current
        self._current = None
        self._keep_btn.setEnabled(False)
        self._mask_keep_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)

        self._move_file(result.path)
        self._show_next()

    def _on_mask_keep(self) -> None:
        if not self._current:
            return

        result = self._current
        self._keep_btn.setEnabled(False)
        self._mask_keep_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._mask_save_completed = False

        try:
            from src.modules.redaction_tool.tool import PDFRedactionTool
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Masking Unavailable",
                f"Could not open masking tool:\n{exc}",
            )
            self._restore_current_actions(result)
            return

        mask_tmp_root = self.output_folder / MASKED_KEEP_TMP_DIR
        mask_tmp_root.mkdir(parents=True, exist_ok=True)

        window = PDFRedactionTool(
            pdf_files=[str(result.path)],
            input_root=str(self.input_folder),
            output_root=str(mask_tmp_root),
            session_path=str(self.output_folder / "mask_keep_session.json"),
            render_dpi=120,
            close_after_save=True,
        )
        window.save_completed.connect(
            lambda saved_path, item=result: self._on_mask_saved(item, Path(saved_path))
        )
        window.destroyed.connect(lambda _obj=None, item=result: self._on_mask_closed(item))
        self._mask_window = window
        window.show()

    def _on_mask_saved(self, result: DetectionResult, masked_path: Path) -> None:
        self._mask_save_completed = True
        try:
            self._replace_with_masked_pdf(result.path, masked_path)
            rel = self._relative_to_input(result.path)
            self._record_masked_keep(rel)
            logger.info("Masked and kept: %s", result.path.name)
            self._status.setText(f"Masked and kept: {result.path.name}")
            self._current = None
            self._show_next()
        except Exception as exc:
            logger.error("Mask & Keep failed for %s: %s", result.path.name, exc)
            QMessageBox.warning(
                self,
                "Mask Failed",
                f"Could not save masked PDF:\n{exc}",
            )
            self._mask_save_completed = False
            self._restore_current_actions(result)

    def _on_mask_closed(self, result: DetectionResult) -> None:
        self._mask_window = None
        if not self._mask_save_completed and self._current is result:
            self._restore_current_actions(result)

    def _restore_current_actions(self, result: DetectionResult) -> None:
        if self._current is result:
            self._keep_btn.setEnabled(True)
            self._mask_keep_btn.setEnabled(True)
            self._delete_btn.setEnabled(True)

    def _move_file(self, src: Path) -> None:
        self.output_folder.mkdir(parents=True, exist_ok=True)
        try:
            rel = src.resolve().relative_to(self.input_folder.resolve())
        except ValueError:
            rel = Path(src.name)

        dst = self.output_folder / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        if dst.exists():
            base = dst.with_suffix("")
            suffix = dst.suffix
            i = 1
            while dst.exists():
                dst = base.with_name(f"{base.name}_{i}").with_suffix(suffix)
                i += 1

        try:
            shutil.move(str(src), str(dst))
            logger.info("Moved: %s → %s", src.name, dst)
            self._status.setText(f"Deleted: {src.name} → {dst.parent.name}/")
        except Exception as exc:
            logger.error("Move failed for %s: %s", src.name, exc)
            QMessageBox.warning(self, "Move Failed",
                                f"Could not move {src.name}:\n{exc}")

    def _relative_to_input(self, src: Path) -> Path:
        try:
            return src.resolve().relative_to(self.input_folder.resolve())
        except ValueError:
            return Path(src.name)

    def _replace_with_masked_pdf(self, original_path: Path, masked_path: Path) -> None:
        if not masked_path.is_file():
            raise FileNotFoundError(f"Masked PDF was not saved: {masked_path}")
        if not original_path.is_file():
            raise FileNotFoundError(f"Original PDF no longer exists: {original_path}")

        temp_path = original_path.with_name(
            f".{original_path.stem}.masked_tmp{original_path.suffix}"
        )
        shutil.copy2(str(masked_path), str(temp_path))
        os.replace(str(temp_path), str(original_path))

        try:
            masked_path.unlink()
        except OSError:
            logger.debug("Could not remove temporary masked copy: %s", masked_path)

    def _record_masked_keep(self, rel_path: Path) -> None:
        manifest_path = self.output_folder / MASKED_KEEP_MANIFEST
        payload, err = safe_json_read(manifest_path)
        if err or not isinstance(payload, dict):
            payload = {"schema_version": "1.0", "files": []}

        rel = rel_path.as_posix()
        files = [f for f in payload.get("files", []) if isinstance(f, str)]
        if rel not in files:
            files.append(rel)

        payload["schema_version"] = "1.0"
        payload["files"] = sorted(files)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.output_folder.mkdir(parents=True, exist_ok=True)
        try:
            atomic_json_write(manifest_path, payload)
        except PermissionError:
            manifest_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # ── Output folder ─────────────────────────────────────────────────────────

    def _change_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select output folder", str(self.output_folder)
        )
        if folder:
            self.output_folder = Path(folder)
            self._out_path.setText(str(self.output_folder))

    # ── Resize: re-scale image when window is resized ─────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._current and self._current.preview_image is not None:
            self._display_image(self._current.preview_image)
