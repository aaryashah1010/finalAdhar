"""
src/utils/file_utils.py
───────────────────────
Stateless file-system utilities shared across modules.

All functions are pure/side-effect-free except where explicitly noted
(writes, renames). No business logic lives here.

Privacy contract:
    No function here reads or stores file *content* beyond computing a
    structural hash. No OCR data, no Aadhaar numbers.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


# ── JSON persistence ──────────────────────────────────────────────────────────

def atomic_json_write(target: Path, payload: Any, indent: int = 2) -> None:
    """
    Write *payload* as JSON to *target* atomically.

    Strategy:
        1. Serialize to a sibling ``.tmp`` file.
        2. fsync to flush OS write buffers to storage.
        3. os.replace() to atomically swap temp → target.

    This guarantees the target file is either the old complete version or
    the new complete version — never a partial write.

    Args:
        target:  Destination path. Parent directory must exist.
        payload: JSON-serializable object.
        indent:  JSON indentation (default 2 spaces for human readability).

    Raises:
        OSError: If the write or rename fails.
        TypeError: If *payload* is not JSON-serializable.
    """
    tmp = target.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=indent, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())   # guarantee bytes reach storage
        os.replace(tmp, target)     # atomic on POSIX; best-effort on Windows
    except Exception:
        _silent_unlink(tmp)
        raise


def safe_json_read(path: Path) -> tuple[Any | None, str | None]:
    """
    Read and parse a JSON file without raising.

    Returns:
        (parsed_object, None)  on success.
        (None, error_message)  on any failure.

    Note:
        error_message is a sanitized technical string suitable for logging.
        It will never contain file content or sensitive data.
    """
    if not path.exists():
        return None, None   # absence is not an error — caller decides

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data, None
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error at line {exc.lineno}: {exc.msg}"
    except OSError as exc:
        return None, f"OS error reading file: {exc.strerror}"


# ── Path fingerprinting ───────────────────────────────────────────────────────

def fingerprint_path_list(paths: list[str]) -> str:
    """
    Compute a SHA-256 fingerprint of an ordered list of file paths.

    The fingerprint changes if any path is added, removed, or reordered.
    It does NOT change if file *contents* change (intentional — we track
    presence and ordering, not content).

    Each path is separated by a null byte to prevent concatenation collisions
    (e.g. ["ab", "cd"] ≠ ["a", "bcd"]).

    Args:
        paths: Ordered sequence of absolute path strings.

    Returns:
        64-character lowercase hex digest.
    """
    h = hashlib.sha256()
    for path in paths:
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def sha256_of_file(file_path: str, chunk_size: int = 65_536) -> str:
    """
    Compute SHA-256 of a file's contents in streaming chunks.

    Used for detecting duplicate or previously-processed files.
    Reads in 64 KB chunks to keep memory usage flat for large PDFs.

    Args:
        file_path:  Absolute path to the file.
        chunk_size: Read chunk size in bytes (default 64 KB).

    Returns:
        64-character lowercase hex digest.

    Raises:
        OSError: If the file cannot be read.
    """
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


# ── Directory helpers ─────────────────────────────────────────────────────────

def ensure_dir(path: Path) -> None:
    """
    Create *path* and all parents if they do not exist.
    Silently succeeds if the directory already exists.

    Raises:
        OSError: If creation fails for any reason other than already existing.
    """
    path.mkdir(parents=True, exist_ok=True)


def is_readable_dir(path: Path) -> bool:
    """Return True if *path* exists, is a directory, and is readable."""
    return path.exists() and path.is_dir() and os.access(path, os.R_OK)


def is_writable_dir(path: Path) -> bool:
    """Return True if *path* exists, is a directory, and is writable."""
    return path.exists() and path.is_dir() and os.access(path, os.W_OK)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _silent_unlink(path: Path) -> None:
    """Delete *path* without raising if it doesn't exist."""
    try:
        path.unlink()
    except OSError:
        pass
