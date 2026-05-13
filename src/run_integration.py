"""
src/run_integration.py
──────────────────────
CLI entry point for the Aadhaar PDF detection + manual redaction pipeline.

If --input-folder and --output-folder are not provided, folder picker
dialogs will appear so you can select them with a click.

Run modes
─────────
    all     — detect then open manual redaction UI (default)
    detect  — detection only, builds the detected queue
    mask    — auto-mask detected files without manual review
    review  — open manual redaction UI from existing queue

Examples
────────
    # Just double-click / run with no args → folder pickers appear:
    python -m src.run_integration

    # Or provide paths manually:
    python -m src.run_integration -i "D:\\pdfs" -o "D:\\output"

    # Detection only (headless):
    python -m src.run_integration -i "D:\\pdfs" -o "D:\\output" --run-mode detect

    # Verbose logging:
    python -m src.run_integration -v
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format  = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt = "%H:%M:%S",
        level   = level,
        stream  = sys.stdout,
    )
    for noisy in ("easyocr", "PIL", "matplotlib", "torch", "torchvision"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _pick_folder(title: str) -> str:
    """Open a native folder-picker dialog and return the selected path."""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()          # hide the empty main window
    root.attributes("-topmost", True)

    folder = filedialog.askdirectory(title=title)

    if not folder:
        messagebox.showerror("Cancelled", f"No folder selected for: {title}")
        root.destroy()
        sys.exit(1)

    root.destroy()
    return folder


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog        = "python -m src.run_integration",
        description = "Aadhaar PDF detection + manual redaction pipeline",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--input-folder", "-i",
        default = None,
        help    = "Root folder containing PDFs to scan. "
                  "If omitted, a folder picker dialog will appear.",
    )
    p.add_argument(
        "--output-folder", "-o",
        default = None,
        help    = "Folder where redacted PDFs will be saved. "
                  "If omitted, a folder picker dialog will appear.",
    )
    p.add_argument(
        "--model-dir", "-m",
        default = "./models",
        help    = "Directory for EasyOCR model storage",
    )
    p.add_argument(
        "--state-dir", "-s",
        default = "./state",
        help    = "Directory for pipeline state and queue JSON files",
    )
    p.add_argument(
        "--run-mode",
        choices = ["detect", "mask", "review", "all"],
        default = "all",
        help    = (
            "all: detect + open manual redaction UI (default); "
            "detect: build detected queue only; "
            "mask: auto-mask without manual review; "
            "review: open manual UI from existing queue"
        ),
    )
    p.add_argument(
        "--render-dpi",
        type    = int,
        default = 300,
        help    = "DPI for PDF page rendering during OCR",
    )
    p.add_argument(
        "--verbose", "-v",
        action  = "store_true",
        help    = "Enable DEBUG logging",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    _configure_logging(args.verbose)

    log = logging.getLogger(__name__)

    # ── Folder selection ──────────────────────────────────────────────────────
    # Show picker dialogs for any path not provided on the command line.

    input_folder = args.input_folder
    if not input_folder:
        log.info("No input folder provided — opening folder picker...")
        input_folder = _pick_folder("Select INPUT folder (contains PDFs to scan)")

    output_folder = args.output_folder
    if not output_folder:
        log.info("No output folder provided — opening folder picker...")
        output_folder = _pick_folder("Select OUTPUT folder (where masked PDFs are saved)")

    log.info("Run mode:      %s", args.run_mode)
    log.info("Input folder:  %s", Path(input_folder).resolve())
    log.info("Output folder: %s", Path(output_folder).resolve())
    log.info("State dir:     %s", Path(args.state_dir).resolve())

    from src.pipeline.integration import IntegrationPipeline

    pipeline = IntegrationPipeline(
        input_folder  = input_folder,
        output_folder = output_folder,
        model_dir     = args.model_dir,
        state_dir     = args.state_dir,
        render_dpi    = args.render_dpi,
    )

    if args.run_mode == "detect":
        summary = pipeline.run_detection_pass()
        log.info("Detection summary: %s", summary)

    elif args.run_mode == "mask":
        summary = pipeline.run_masking_pass()
        log.info("Masking summary: %s", summary)

    elif args.run_mode == "review":
        pipeline.launch_redaction_ui()

    else:  # all — detect then open manual redaction UI
        pipeline.run()


if __name__ == "__main__":
    main()
