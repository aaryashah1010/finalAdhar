"""Resume state for the one-by-one review UI.

The active Docker flow uses ``src.review_ui`` directly, so this module keeps
file-level progress for that UI without changing the broader pipeline code.
Only relative paths and status metadata are stored; OCR text and Aadhaar
numbers are never persisted.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.utils.file_utils import atomic_json_write, safe_json_read


STATE_FILE_NAME = "_review_resume_state.json"
SCHEMA_VERSION = "1.0"

STATUS_PENDING_SCAN = "pending_scan"
STATUS_SCANNING = "scanning"
STATUS_PENDING_REVIEW = "aadhaar_pending_review"
STATUS_NOT_AADHAAR = "not_aadhaar"
STATUS_KEPT = "kept"
STATUS_DELETED = "deleted"
STATUS_MASKED = "masked"
STATUS_ERROR = "error"
STATUS_MISSING = "missing"

DONE_STATUSES = {
    STATUS_NOT_AADHAAR,
    STATUS_KEPT,
    STATUS_DELETED,
    STATUS_MASKED,
}


class ReviewResumeState:
    """Small JSON-backed resume store for scan/review progress."""

    def __init__(self, input_root: Path, output_root: Path) -> None:
        self.input_root = input_root.resolve()
        self.output_root = output_root.resolve()
        self.path = self.output_root / STATE_FILE_NAME
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._load()

    def reconcile(self, pdf_paths: Iterable[Path]) -> None:
        """Merge current filesystem state into the resume store."""
        with self._lock:
            seen: set[str] = set()
            for path in pdf_paths:
                rel = self.rel_path(path)
                seen.add(rel)
                sig = self._signature(path)
                record = self._records.get(rel)

                if record is None:
                    self._records[rel] = self._new_record(path, sig)
                    continue

                record["absolute_path"] = str(path.resolve())
                record["filename"] = path.name

                status = record.get("status", STATUS_PENDING_SCAN)
                if status == STATUS_SCANNING:
                    record["status"] = STATUS_PENDING_SCAN
                elif status not in DONE_STATUSES and not self._same_signature(record, sig):
                    record["status"] = STATUS_PENDING_SCAN

                record["size"] = sig["size"]
                record["mtime_ns"] = sig["mtime_ns"]

            for rel, record in self._records.items():
                if rel in seen:
                    continue
                if record.get("status") != STATUS_DELETED:
                    record["status"] = STATUS_MISSING
                    record["updated_at"] = self._now()

            self._save_locked()

    def scan_paths(self, pdf_paths: Iterable[Path]) -> list[Path]:
        with self._lock:
            out: list[Path] = []
            for path in pdf_paths:
                status = self._records.get(self.rel_path(path), {}).get("status")
                if status in (STATUS_PENDING_SCAN, STATUS_PENDING_REVIEW, STATUS_ERROR, None):
                    out.append(path)
            return out

    def completed_count(self, pdf_paths: Iterable[Path]) -> int:
        with self._lock:
            count = 0
            for path in pdf_paths:
                status = self._records.get(self.rel_path(path), {}).get("status")
                if status in DONE_STATUSES:
                    count += 1
            return count

    def pending_review_count(self) -> int:
        with self._lock:
            return sum(
                1
                for record in self._records.values()
                if record.get("status") == STATUS_PENDING_REVIEW
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._records.values():
                status = str(record.get("status", STATUS_PENDING_SCAN))
                counts[status] = counts.get(status, 0) + 1
            return counts

    def mark_scanning(self, path: Path) -> None:
        self._mark(path, STATUS_SCANNING)

    def mark_not_aadhaar(self, path: Path) -> None:
        self._mark(path, STATUS_NOT_AADHAAR)

    def mark_pending_review(self, path: Path, confidence: float, reasons: list[str]) -> None:
        self._mark(
            path,
            STATUS_PENDING_REVIEW,
            confidence=confidence,
            reasons=list(reasons),
        )

    def mark_kept(self, path: Path) -> None:
        self._mark(path, STATUS_KEPT)

    def mark_deleted(self, path: Path, output_path: Path) -> None:
        self._mark(path, STATUS_DELETED, output_path=str(output_path.resolve()))

    def mark_masked(self, path: Path) -> None:
        self._mark(path, STATUS_MASKED)

    def mark_error(self, path: Path, reason: str) -> None:
        self._mark(path, STATUS_ERROR, error=reason[:200])

    def rel_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.input_root).as_posix()
        except ValueError:
            return path.name

    def _mark(self, path: Path, status: str, **extra: object) -> None:
        with self._lock:
            rel = self.rel_path(path)
            sig = self._signature(path) if path.exists() else {"size": None, "mtime_ns": None}
            record = self._records.get(rel) or self._new_record(path, sig)
            record["absolute_path"] = str(path.resolve())
            record["filename"] = path.name
            record["status"] = status
            record["size"] = sig["size"]
            record["mtime_ns"] = sig["mtime_ns"]
            record["updated_at"] = self._now()
            record.update(extra)
            self._records[rel] = record
            self._save_locked()

    def _load(self) -> None:
        payload, err = safe_json_read(self.path)
        if err or not isinstance(payload, dict):
            self._records = {}
            return

        if payload.get("schema_version") != SCHEMA_VERSION:
            self._records = {}
            return

        records = payload.get("files", {})
        self._records = records if isinstance(records, dict) else {}

    def _save_locked(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "input_root": str(self.input_root),
            "files": self._records,
            "updated_at": self._now(),
        }
        try:
            atomic_json_write(self.path, payload)
        except PermissionError:
            self.path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _new_record(self, path: Path, sig: dict) -> dict:
        return {
            "relative_path": self.rel_path(path),
            "absolute_path": str(path.resolve()),
            "filename": path.name,
            "status": STATUS_PENDING_SCAN,
            "size": sig["size"],
            "mtime_ns": sig["mtime_ns"],
            "updated_at": self._now(),
        }

    @staticmethod
    def _signature(path: Path) -> dict:
        try:
            stat = path.stat()
        except OSError:
            return {"size": None, "mtime_ns": None}
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    @staticmethod
    def _same_signature(record: dict, sig: dict) -> bool:
        return record.get("size") == sig["size"] and record.get("mtime_ns") == sig["mtime_ns"]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

