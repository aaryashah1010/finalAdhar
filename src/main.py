"""
src/main.py
───────────
Entry point for the Aadhaar card detector.

Run:
    python -m src.main

Two folder pickers appear:
  1. Input folder  — folder containing PDFs to scan
  2. Output folder — where deleted Aadhaar files are moved

Scanning runs in background. Detected Aadhaar files appear one by one
in a viewer — user clicks Keep or Delete for each.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _pick_folder(title: str) -> Path:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title=title)
    if not folder:
        messagebox.showerror("Cancelled", f"No folder selected for: {title}")
        root.destroy()
        sys.exit(1)
    root.destroy()
    return Path(folder)


def main() -> None:
    logging.basicConfig(
        format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt = "%H:%M:%S",
        level   = logging.INFO,
        stream  = sys.stdout,
    )
    for noisy in ("easyocr", "PIL", "matplotlib", "torch", "torchvision"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger(__name__)

    input_env = os.getenv("AADHAAR_INPUT_DIR")
    output_env = os.getenv("AADHAAR_OUTPUT_DIR")

    if input_env and output_env:
        input_folder = Path(input_env)
        output_folder = Path(output_env)
        if not input_folder.exists():
            raise SystemExit(f"AADHAAR_INPUT_DIR does not exist: {input_folder}")
        output_folder.mkdir(parents=True, exist_ok=True)
    else:
        input_folder  = _pick_folder("Select INPUT folder (PDFs to scan)")
        output_folder = _pick_folder("Select OUTPUT folder (deleted files go here)")

    log.info("Input:  %s", input_folder)
    log.info("Output: %s", output_folder)

    from PyQt5.QtWidgets import QApplication
    from src.review_ui import ReviewWindow

    app    = QApplication(sys.argv)
    window = ReviewWindow(input_folder=input_folder, output_folder=output_folder)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
