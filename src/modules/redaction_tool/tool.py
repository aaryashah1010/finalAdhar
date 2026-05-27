"""
src/modules/redaction_tool/tool.py
----------------------------------
PyQt5 desktop tool for manual PDF masking and permanent redaction.

Scope:
    - Manual rectangle masking only (no OCR/AI auto detection)
    - Page and file navigation
    - Undo / redo of mask edits
    - Permanent redaction save via PyMuPDF
    - Session resume with last processed file index in session.json
"""

from __future__ import annotations

import os
import shutil
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import DefaultDict, Dict, List, Optional

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QFileDialog,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QShortcut,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ...utils.file_utils import atomic_json_write, fingerprint_path_list, safe_json_read
from ...utils.fitz_compat import fitz


SESSION_SCHEMA_VERSION = "1.0"
MIN_DRAW_SIZE_PX = 4.0
DEFAULT_CACHE_SIZE = 3
DEFAULT_RENDER_DPI = 200


@dataclass
class MaskRecord:
    """Stores a mask rectangle in PDF point coordinates for one page."""

    mask_id: int
    rect_pdf: QRectF


@dataclass
class MaskAction:
    """Undo/redo action for add or delete mask operations."""

    op: str
    page_index: int
    mask_id: int
    rect_pdf: QRectF


@dataclass
class PageRenderEntry:
    """Cached page render with pixel and point dimensions."""

    pixmap: QPixmap
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float


class MaskGraphicsItem(QGraphicsRectItem):
    """Graphics item for a single mask rectangle."""

    def __init__(self, mask_id: int, rect: QRectF) -> None:
        super().__init__(rect)
        self.mask_id = mask_id
        self.setBrush(QBrush(QColor(0, 0, 0, 255)))
        self.setPen(QPen(QColor(18, 18, 18), 1))
        self.setZValue(10)
        self.setFlag(QGraphicsRectItem.ItemIsSelectable, True)


class PDFCanvas(QGraphicsView):
    """
    Interactive canvas for PDF preview and mask drawing.

    Modes:
        - draw: click-and-drag to create a mask rectangle
        - delete: click existing mask to select for delete action
    """

    rect_drawn = pyqtSignal(QRectF)

    def __init__(self, scene: QGraphicsScene, parent: Optional[QWidget] = None) -> None:
        super().__init__(scene, parent)
        self._mode = "draw"
        self._drag_start = None
        self._preview_rect_item: Optional[QGraphicsRectItem] = None

        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(32, 32, 32)))

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.setCursor(Qt.CrossCursor if mode == "draw" else Qt.ArrowCursor)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        if self._mode == "draw":
            self._drag_start = self.mapToScene(event.pos())
            self._preview_rect_item = QGraphicsRectItem()
            self._preview_rect_item.setPen(QPen(QColor(0, 170, 255), 1, Qt.DashLine))
            self._preview_rect_item.setBrush(QBrush(QColor(0, 0, 0, 70)))
            self._preview_rect_item.setZValue(20)
            self.scene().addItem(self._preview_rect_item)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._mode != "draw" or self._drag_start is None or self._preview_rect_item is None:
            super().mouseMoveEvent(event)
            return

        current = self.mapToScene(event.pos())
        rect = QRectF(self._drag_start, current).normalized()
        self._preview_rect_item.setRect(rect)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._mode != "draw" or event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return

        if self._drag_start is None or self._preview_rect_item is None:
            super().mouseReleaseEvent(event)
            return

        rect = self._preview_rect_item.rect().normalized()
        self.scene().removeItem(self._preview_rect_item)
        self._preview_rect_item = None
        self._drag_start = None

        if rect.width() >= MIN_DRAW_SIZE_PX and rect.height() >= MIN_DRAW_SIZE_PX:
            self.rect_drawn.emit(rect)

        event.accept()


class PDFRedactionTool(QMainWindow):
    """PyQt5 manual PDF masking tool with permanent redaction save."""

    save_completed = pyqtSignal(str)

    def __init__(
        self,
        pdf_files: Optional[List[str]] = None,
        input_root: Optional[str] = None,
        output_root: Optional[str] = None,
        session_path: str = "session.json",
        render_dpi: int = DEFAULT_RENDER_DPI,
        close_after_save: bool = False,
    ) -> None:
        super().__init__()

        self.setWindowTitle("Manual PDF Redaction Tool")
        self.resize(1400, 900)

        self._render_scale = max(1.0, render_dpi / 72.0)
        self._render_matrix = fitz.Matrix(self._render_scale, self._render_scale)
        self._cache_limit = DEFAULT_CACHE_SIZE

        self._session_path = Path(session_path).resolve()
        self._input_root: Optional[Path] = Path(input_root).resolve() if input_root else None
        self._output_root: Optional[Path] = Path(output_root).resolve() if output_root else None
        self._close_after_save = close_after_save

        self._pdf_files: List[Path] = []
        self._pdf_fingerprint = ""
        self._current_file_index = 0
        self._last_processed_index = -1

        self._doc: Optional[fitz.Document] = None
        self._current_page_index = 0
        self._page_cache: "OrderedDict[int, PageRenderEntry]" = OrderedDict()

        self._masks_by_page: DefaultDict[int, Dict[int, MaskRecord]] = defaultdict(dict)
        self._mask_items_by_id: Dict[int, MaskGraphicsItem] = {}
        self._next_mask_id = 1
        self._selected_mask_id: Optional[int] = None
        self._undo_stack: List[MaskAction] = []
        self._redo_stack: List[MaskAction] = []

        self._zoom_factor = 1.0

        self._setup_ui()
        self._setup_shortcuts()

        if pdf_files:
            self.set_pdf_files(pdf_files, input_root=input_root, output_root=output_root)
        else:
            self.open_pdf_folder()

    # -------------------------------------------------------------------------
    # UI setup
    # -------------------------------------------------------------------------
    def _setup_ui(self) -> None:
        self.scene = QGraphicsScene(self)
        self.canvas = PDFCanvas(self.scene, self)
        self.canvas.rect_drawn.connect(self.draw_mask)

        self.scene.selectionChanged.connect(self._on_scene_selection_changed)

        self.file_label = QLabel("File: -")
        self.page_label = QLabel("Page: -")

        top_row = QHBoxLayout()
        top_row.addWidget(self.file_label, stretch=2)
        top_row.addWidget(self.page_label, stretch=1)

        self.prev_page_btn = QPushButton("Previous Page")
        self.next_page_btn = QPushButton("Next Page")
        self.prev_pdf_btn = QPushButton("Previous PDF")
        self.next_pdf_btn = QPushButton("Next PDF")
        self.skip_pdf_btn = QPushButton("Skip PDF")

        self.prev_page_btn.clicked.connect(self.prev_page)
        self.next_page_btn.clicked.connect(self.next_page)
        self.prev_pdf_btn.clicked.connect(self.prev_pdf)
        self.next_pdf_btn.clicked.connect(self.next_pdf)
        self.skip_pdf_btn.clicked.connect(self.skip_pdf)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.prev_page_btn)
        bottom_row.addWidget(self.next_page_btn)
        bottom_row.addSpacing(12)
        bottom_row.addWidget(self.prev_pdf_btn)
        bottom_row.addWidget(self.next_pdf_btn)
        bottom_row.addWidget(self.skip_pdf_btn)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addLayout(top_row)
        layout.addWidget(self.canvas, stretch=1)
        layout.addLayout(bottom_row)
        self.setCentralWidget(container)

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)

        toolbar = QToolBar("Tools", self)
        self.addToolBar(toolbar)

        self.open_action = QAction("Open Folder", self)
        self.open_action.triggered.connect(self.open_pdf_folder)
        toolbar.addAction(self.open_action)
        toolbar.addSeparator()

        self.draw_mode_action = QAction("Draw Mode", self)
        self.draw_mode_action.setCheckable(True)
        self.draw_mode_action.setChecked(True)
        self.draw_mode_action.triggered.connect(lambda: self.set_mode("draw"))

        self.delete_mode_action = QAction("Delete Mode", self)
        self.delete_mode_action.setCheckable(True)
        self.delete_mode_action.triggered.connect(lambda: self.set_mode("delete"))

        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        mode_group.addAction(self.draw_mode_action)
        mode_group.addAction(self.delete_mode_action)

        toolbar.addAction(self.draw_mode_action)
        toolbar.addAction(self.delete_mode_action)
        toolbar.addSeparator()

        self.undo_action = QAction("Undo", self)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction("Redo", self)
        self.redo_action.triggered.connect(self.redo)
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()

        self.save_action = QAction("Save", self)
        self.save_action.triggered.connect(self.save_pdf)
        toolbar.addAction(self.save_action)

        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_out_action = QAction("Zoom Out", self)
        self.zoom_reset_action = QAction("Reset Zoom", self)
        self.zoom_in_action.triggered.connect(lambda: self.zoom_by(1.15))
        self.zoom_out_action.triggered.connect(lambda: self.zoom_by(1.0 / 1.15))
        self.zoom_reset_action.triggered.connect(self.reset_zoom)
        toolbar.addSeparator()
        toolbar.addAction(self.zoom_in_action)
        toolbar.addAction(self.zoom_out_action)
        toolbar.addAction(self.zoom_reset_action)

        self._refresh_actions()

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence(Qt.Key_Delete), self, activated=self.delete_mask)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self.redo)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self.save_and_next_pdf)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self.save_and_next_pdf)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=self.prev_page)
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=self.next_page)
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.zoom_by(1.15))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.zoom_by(1.0 / 1.15))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.reset_zoom)

    # -------------------------------------------------------------------------
    # Data/session setup
    # -------------------------------------------------------------------------
    def set_pdf_files(
        self,
        pdf_files: List[str],
        input_root: Optional[str] = None,
        output_root: Optional[str] = None,
    ) -> None:
        resolved = sorted((Path(p).resolve() for p in pdf_files), key=lambda p: str(p).lower())
        self._pdf_files = [p for p in resolved if p.suffix.lower() == ".pdf"]
        self._pdf_fingerprint = fingerprint_path_list([str(p) for p in self._pdf_files])

        if input_root:
            self._input_root = Path(input_root).resolve()
        elif self._pdf_files:
            common = os.path.commonpath([str(p.parent) for p in self._pdf_files])
            self._input_root = Path(common).resolve()

        if output_root:
            self._output_root = Path(output_root).resolve()
        elif self._input_root:
            self._output_root = self._input_root / "redacted_output"

        if self._output_root is not None:
            self._output_root.mkdir(parents=True, exist_ok=True)

        resume_index = self._read_resume_index()
        if self._pdf_files:
            self._current_file_index = max(0, min(resume_index, len(self._pdf_files) - 1))
            self.load_pdf(self._current_file_index)
        else:
            self._set_status("No PDF files found.")
            self._refresh_header()
            self._refresh_actions()

    def open_pdf_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Folder with PDFs")
        if not folder:
            return
        root = Path(folder).resolve()
        pdf_files = [str(p) for p in root.rglob("*.pdf")]
        self.set_pdf_files(pdf_files, input_root=str(root))

    def _read_resume_index(self) -> int:
        payload, err = safe_json_read(self._session_path)
        if err or not isinstance(payload, dict):
            return 0
        if payload.get("schema_version") != SESSION_SCHEMA_VERSION:
            return 0
        if payload.get("pdf_fingerprint") != self._pdf_fingerprint:
            return 0
        if payload.get("pdf_count") != len(self._pdf_files):
            return 0
        self._last_processed_index = int(payload.get("last_processed_file_index", -1))
        return int(payload.get("current_file_index", 0))

    def _write_session(self) -> None:
        payload = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "pdf_fingerprint": self._pdf_fingerprint,
            "pdf_count": len(self._pdf_files),
            "current_file_index": self._current_file_index,
            "last_processed_file_index": self._last_processed_index,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self._session_path, payload)

    # -------------------------------------------------------------------------
    # PDF loading and rendering
    # -------------------------------------------------------------------------
    def load_pdf(self, file_index: Optional[int] = None) -> None:
        """Load PDF by index and reset page/mask state for manual redaction."""
        if not self._pdf_files:
            return

        if file_index is not None:
            if not (0 <= file_index < len(self._pdf_files)):
                return
            self._current_file_index = file_index

        self._close_current_doc()
        self._clear_mask_state()

        current_path = self.current_pdf_path
        try:
            self._doc = fitz.open(str(current_path))
            if self._doc.is_encrypted:
                raise RuntimeError("Encrypted PDF is not supported for manual redaction.")
            if len(self._doc) == 0:
                raise RuntimeError("PDF has no pages.")
        except Exception as exc:
            self._doc = None
            QMessageBox.warning(self, "Open Failed", f"Could not open PDF:\n{current_path}\n\n{exc}")
            self._set_status("Failed to open PDF.")
            self._refresh_header()
            self._refresh_actions()
            return

        self._current_page_index = 0
        self._write_session()
        self.render_page(0)

    def render_page(self, page_index: Optional[int] = None) -> None:
        """Render one page lazily with cache and repaint overlays."""
        if self._doc is None:
            self.scene.clear()
            self._refresh_header()
            self._refresh_actions()
            return

        if page_index is not None:
            if not (0 <= page_index < len(self._doc)):
                return
            self._current_page_index = page_index

        entry = self._get_or_render_page(self._current_page_index)
        if entry is None:
            self._set_status("Failed to render page.")
            return

        self.scene.clear()
        self._mask_items_by_id.clear()
        self._selected_mask_id = None

        self.scene.setSceneRect(QRectF(0, 0, entry.width_px, entry.height_px))
        self.scene.addPixmap(entry.pixmap)
        self._render_mask_items_for_page(self._current_page_index, entry)
        self.canvas.setTransform(QTransform().scale(self._zoom_factor, self._zoom_factor))
        self.canvas.centerOn(entry.width_px / 2.0, entry.height_px / 2.0)

        self._refresh_header()
        self._refresh_actions()

    def _get_or_render_page(self, page_index: int) -> Optional[PageRenderEntry]:
        if page_index in self._page_cache:
            entry = self._page_cache.pop(page_index)
            self._page_cache[page_index] = entry
            return entry

        if self._doc is None:
            return None

        try:
            page = self._doc[page_index]
            pix = page.get_pixmap(matrix=self._render_matrix, alpha=False, annots=True)
            fmt = QImage.Format_Grayscale8 if pix.n == 1 else QImage.Format_RGB888
            qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()
            pixmap = QPixmap.fromImage(qimg)
            entry = PageRenderEntry(
                pixmap=pixmap,
                width_px=pix.width,
                height_px=pix.height,
                width_pt=page.rect.width,
                height_pt=page.rect.height,
            )
            self._cache_page(page_index, entry)
            return entry
        except Exception:
            return None

    def _cache_page(self, page_index: int, entry: PageRenderEntry) -> None:
        self._page_cache[page_index] = entry
        while len(self._page_cache) > self._cache_limit:
            self._page_cache.popitem(last=False)

    # -------------------------------------------------------------------------
    # Mask operations
    # -------------------------------------------------------------------------
    def draw_mask(self, scene_rect: QRectF) -> None:
        """Create a new mask from user drag rectangle on current page."""
        if self._doc is None:
            return
        entry = self._get_or_render_page(self._current_page_index)
        if entry is None:
            return

        bounded = scene_rect.normalized().intersected(QRectF(0, 0, entry.width_px, entry.height_px))
        if bounded.width() < MIN_DRAW_SIZE_PX or bounded.height() < MIN_DRAW_SIZE_PX:
            return

        rect_pdf = self._scene_to_pdf_rect(bounded, entry)
        mask = MaskRecord(mask_id=self._next_mask_id, rect_pdf=rect_pdf)
        self._next_mask_id += 1

        self._masks_by_page[self._current_page_index][mask.mask_id] = mask
        self._undo_stack.append(MaskAction("add", self._current_page_index, mask.mask_id, QRectF(mask.rect_pdf)))
        self._redo_stack.clear()

        self.render_page(self._current_page_index)
        self._set_status(f"Mask added on page {self._current_page_index + 1}.")

    def delete_mask(self) -> None:
        """Delete currently selected mask."""
        if self._selected_mask_id is None:
            return
        page_masks = self._masks_by_page.get(self._current_page_index, {})
        mask = page_masks.get(self._selected_mask_id)
        if mask is None:
            return

        del page_masks[self._selected_mask_id]
        self._undo_stack.append(
            MaskAction("delete", self._current_page_index, mask.mask_id, QRectF(mask.rect_pdf))
        )
        self._redo_stack.clear()
        self._selected_mask_id = None

        self.render_page(self._current_page_index)
        self._set_status("Mask deleted.")

    def undo(self) -> None:
        if not self._undo_stack:
            return
        action = self._undo_stack.pop()
        self._apply_action(action, reverse=True)
        self._redo_stack.append(action)
        self.render_page(self._current_page_index)
        self._refresh_actions()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        action = self._redo_stack.pop()
        self._apply_action(action, reverse=False)
        self._undo_stack.append(action)
        self.render_page(self._current_page_index)
        self._refresh_actions()

    def _apply_action(self, action: MaskAction, reverse: bool) -> None:
        page_masks = self._masks_by_page[action.page_index]
        if action.op == "add":
            if reverse:
                page_masks.pop(action.mask_id, None)
            else:
                page_masks[action.mask_id] = MaskRecord(action.mask_id, QRectF(action.rect_pdf))
        elif action.op == "delete":
            if reverse:
                page_masks[action.mask_id] = MaskRecord(action.mask_id, QRectF(action.rect_pdf))
            else:
                page_masks.pop(action.mask_id, None)

        self._next_mask_id = max(self._next_mask_id, action.mask_id + 1)

    # -------------------------------------------------------------------------
    # Save / permanent redaction
    # -------------------------------------------------------------------------
    def apply_redaction(self, output_path: Optional[Path] = None) -> bool:
        """
        Apply all manual masks permanently using PyMuPDF redaction API.

        Returns:
            True on successful write, False on failure.
        """
        if self.current_pdf_path is None:
            return False

        source_path = self.current_pdf_path
        target_path = output_path or self._resolve_output_path(source_path)
        if target_path is None:
            return False

        masks_exist = any(bool(v) for v in self._masks_by_page.values())
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)

            if not masks_exist:
                if source_path.resolve() != target_path.resolve():
                    shutil.copy2(source_path, target_path)
                return True

            doc = fitz.open(str(source_path))
            for page_index, masks in self._masks_by_page.items():
                if not masks:
                    continue
                page = doc[page_index]
                for mask in masks.values():
                    rect = fitz.Rect(
                        mask.rect_pdf.left(),
                        mask.rect_pdf.top(),
                        mask.rect_pdf.right(),
                        mask.rect_pdf.bottom(),
                    )
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                page.apply_redactions()

            doc.save(str(target_path), garbage=4, clean=True, deflate=True)
            doc.close()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save redacted PDF:\n{exc}")
            return False

    def save_pdf(self) -> bool:
        """Save current PDF with permanent redactions."""
        if self._doc is None or self.current_pdf_path is None:
            return False

        target_path = self._resolve_output_path(self.current_pdf_path)
        if target_path is None:
            return False

        ok = self.apply_redaction(target_path)
        if not ok:
            return False

        self._last_processed_index = self._current_file_index
        self._write_session()
        self._set_status(f"Saved: {target_path.name}")
        if self._close_after_save:
            self._close_current_doc()
            self.save_completed.emit(str(target_path))
            self.close()
        else:
            self.save_completed.emit(str(target_path))
        return True

    def save_and_next_pdf(self) -> None:
        if self.save_pdf():
            self.next_pdf(skip_unsaved_prompt=True)

    # -------------------------------------------------------------------------
    # Navigation
    # -------------------------------------------------------------------------
    def prev_page(self) -> None:
        if self._doc is None:
            return
        if self._current_page_index > 0:
            self.render_page(self._current_page_index - 1)

    def next_page(self) -> None:
        if self._doc is None:
            return
        if self._current_page_index < len(self._doc) - 1:
            self.render_page(self._current_page_index + 1)

    def prev_pdf(self) -> None:
        if not self._pdf_files:
            return
        if not self._confirm_discard_if_needed():
            return
        if self._current_file_index > 0:
            self.load_pdf(self._current_file_index - 1)

    def next_pdf(self, skip_unsaved_prompt: bool = False) -> None:
        """Move to next PDF file."""
        if not self._pdf_files:
            return
        if not skip_unsaved_prompt and not self._confirm_discard_if_needed():
            return
        if self._current_file_index < len(self._pdf_files) - 1:
            self.load_pdf(self._current_file_index + 1)
        else:
            QMessageBox.information(self, "Completed", "No more PDFs in the queue.")

    def skip_pdf(self) -> None:
        if not self._pdf_files:
            return
        if not self._confirm_discard_if_needed():
            return
        self._set_status("PDF skipped.")
        self.next_pdf(skip_unsaved_prompt=True)

    # -------------------------------------------------------------------------
    # Mode and zoom
    # -------------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self.canvas.set_mode(mode)
        if mode == "draw":
            self.draw_mode_action.setChecked(True)
        else:
            self.delete_mode_action.setChecked(True)
        self._set_status(f"Mode: {mode.title()}")

    def zoom_by(self, factor: float) -> None:
        self._zoom_factor = max(0.2, min(6.0, self._zoom_factor * factor))
        self.canvas.setTransform(QTransform().scale(self._zoom_factor, self._zoom_factor))
        self._set_status(f"Zoom: {int(self._zoom_factor * 100)}%")

    def reset_zoom(self) -> None:
        self._zoom_factor = 1.0
        self.canvas.setTransform(QTransform())
        self._set_status("Zoom reset to 100%.")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    @property
    def current_pdf_path(self) -> Optional[Path]:
        if not self._pdf_files:
            return None
        if not (0 <= self._current_file_index < len(self._pdf_files)):
            return None
        return self._pdf_files[self._current_file_index]

    def _refresh_header(self) -> None:
        if self.current_pdf_path is None:
            self.file_label.setText("File: -")
            self.page_label.setText("Page: -")
            return

        total_files = len(self._pdf_files)
        file_name = self.current_pdf_path.name
        self.file_label.setText(
            f"File: {file_name} ({self._current_file_index + 1}/{total_files})"
        )

        if self._doc is not None:
            self.page_label.setText(
                f"Page: {self._current_page_index + 1}/{len(self._doc)}"
            )
        else:
            self.page_label.setText("Page: -")

    def _refresh_actions(self) -> None:
        has_doc = self._doc is not None
        has_prev_pdf = self._current_file_index > 0
        has_next_pdf = bool(self._pdf_files) and self._current_file_index < len(self._pdf_files) - 1

        self.prev_page_btn.setEnabled(has_doc and self._current_page_index > 0)
        self.next_page_btn.setEnabled(has_doc and self._doc is not None and self._current_page_index < len(self._doc) - 1)
        self.prev_pdf_btn.setEnabled(has_prev_pdf)
        self.next_pdf_btn.setEnabled(has_next_pdf)
        self.skip_pdf_btn.setEnabled(has_doc)

        self.save_action.setEnabled(has_doc)
        self.undo_action.setEnabled(bool(self._undo_stack))
        self.redo_action.setEnabled(bool(self._redo_stack))

    def _render_mask_items_for_page(self, page_index: int, entry: PageRenderEntry) -> None:
        page_masks = self._masks_by_page.get(page_index, {})
        for mask in page_masks.values():
            scene_rect = self._pdf_to_scene_rect(mask.rect_pdf, entry)
            item = MaskGraphicsItem(mask.mask_id, scene_rect)
            self.scene.addItem(item)
            self._mask_items_by_id[mask.mask_id] = item

    def _scene_to_pdf_rect(self, scene_rect: QRectF, entry: PageRenderEntry) -> QRectF:
        x_scale = entry.width_pt / entry.width_px
        y_scale = entry.height_pt / entry.height_px
        return QRectF(
            scene_rect.left() * x_scale,
            scene_rect.top() * y_scale,
            scene_rect.width() * x_scale,
            scene_rect.height() * y_scale,
        ).normalized()

    def _pdf_to_scene_rect(self, rect_pdf: QRectF, entry: PageRenderEntry) -> QRectF:
        x_scale = entry.width_px / entry.width_pt
        y_scale = entry.height_px / entry.height_pt
        return QRectF(
            rect_pdf.left() * x_scale,
            rect_pdf.top() * y_scale,
            rect_pdf.width() * x_scale,
            rect_pdf.height() * y_scale,
        ).normalized()

    def _confirm_discard_if_needed(self) -> bool:
        if not any(self._masks_by_page.values()):
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved Masks",
            "This PDF has unsaved mask changes. Save before leaving this file?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Yes:
            return self.save_pdf()
        return True

    def _resolve_output_path(self, source_path: Path) -> Optional[Path]:
        if self._output_root is None:
            folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
            if not folder:
                return None
            self._output_root = Path(folder).resolve()
            self._output_root.mkdir(parents=True, exist_ok=True)

        if self._input_root and source_path.is_relative_to(self._input_root):
            rel = source_path.relative_to(self._input_root)
            return (self._output_root / rel).resolve()
        return (self._output_root / source_path.name).resolve()

    def _clear_mask_state(self) -> None:
        self._masks_by_page.clear()
        self._mask_items_by_id.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._selected_mask_id = None
        self._next_mask_id = 1
        self._page_cache.clear()

    def _close_current_doc(self) -> None:
        if self._doc is None:
            return
        try:
            if not self._doc.is_closed:
                self._doc.close()
        except Exception:
            pass
        self._doc = None

    def _on_scene_selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        self._selected_mask_id = None
        if selected:
            item = selected[0]
            if isinstance(item, MaskGraphicsItem):
                self._selected_mask_id = item.mask_id
        self._refresh_actions()

    def _set_status(self, text: str) -> None:
        self.status_bar.showMessage(text, 3500)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._write_session()
        except Exception:
            pass
        self._close_current_doc()
        super().closeEvent(event)


def build_pdf_file_list(input_folder: str) -> List[str]:
    """Recursively list all PDFs under folder in deterministic order."""
    root = Path(input_folder).resolve()
    candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"]
    return [str(p) for p in sorted(candidates, key=lambda p: str(p).lower())]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Manual PDF Redaction Tool")
    parser.add_argument("--input-folder", type=str, default=None, help="Folder containing PDFs")
    parser.add_argument("--output-folder", type=str, default=None, help="Folder for redacted PDFs")
    parser.add_argument("--session", type=str, default="session.json", help="Path to session JSON")
    parser.add_argument("--render-dpi", type=int, default=DEFAULT_RENDER_DPI, help="Preview render DPI")
    args = parser.parse_args()

    app = QApplication([])

    pdf_files = None
    if args.input_folder:
        pdf_files = build_pdf_file_list(args.input_folder)

    window = PDFRedactionTool(
        pdf_files=pdf_files,
        input_root=args.input_folder,
        output_root=args.output_folder,
        session_path=args.session,
        render_dpi=args.render_dpi,
    )
    window.show()
    app.exec_()


if __name__ == "__main__":
    main()
