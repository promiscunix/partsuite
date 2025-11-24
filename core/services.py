"""
Service layer for invoice processing.

This module wraps the CLI logic from invoice_pipeline.py into reusable
functions that can be called from the FastAPI app.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

import PyPDF2

# Import the core processing functions from invoice_pipeline
import invoice_pipeline as pipeline


def run_ocr_for_api(input_pdf: Path, do_ocr: bool) -> Path:
    """
    Run ocrmypdf if requested (API-friendly version, no console output).

    Raises:
        FileNotFoundError: If ocrmypdf is not found
        subprocess.CalledProcessError: If OCR fails
    """
    if not do_ocr:
        return input_pdf

    tmp_dir = Path(tempfile.mkdtemp(prefix="invoice_ocr_"))
    out_pdf = tmp_dir / (input_pdf.stem + "_ocr.pdf")

    cmd = [
        "ocrmypdf",
        "--skip-text",
        "-l",
        "eng",
        "--deskew",
        str(input_pdf),
        str(out_pdf),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_pdf


def process_bulk_pdf(
    pdf_path: Path,
    output_dir: Path,
    do_ocr: bool = False,
) -> tuple[List[pipeline.InvoiceData], List[Path]]:
    """
    Process a bulk PDF containing one or more invoices.

    Args:
        pdf_path: Path to the input PDF file
        output_dir: Directory where individual invoice PDFs will be saved
        do_ocr: Whether to run OCR on the PDF (if it's image-only)

    Returns:
        Tuple of (list of InvoiceData, list of output PDF paths)

    Raises:
        FileNotFoundError: If ocrmypdf is not found and do_ocr=True
        subprocess.CalledProcessError: If OCR fails

    This function:
    1. Optionally runs OCR if requested
    2. Extracts text from all pages
    3. Splits pages into individual invoices
    4. Saves each invoice as a separate PDF
    5. Returns the extracted invoice data and PDF paths
    """
    # Run OCR if requested (using API-friendly version)
    processed_pdf = run_ocr_for_api(pdf_path, do_ocr=do_ocr)

    # Extract text from all pages
    reader = PyPDF2.PdfReader(str(processed_pdf))
    page_texts = [p.extract_text() or "" for p in reader.pages]

    # Split into invoices
    invoices = pipeline.split_into_invoices(page_texts)

    if not invoices:
        return [], []

    # Save individual invoice PDFs
    # Use the original PDF (not OCR'd version) for page extraction
    # to preserve image quality
    written_paths = pipeline.save_invoices_as_pdfs(
        pdf_path,  # Use original for page extraction
        invoices,
        output_dir,
    )

    return invoices, written_paths


def extract_invoice_data_with_llm(text: str) -> dict:
    """
    Extract invoice metadata using an LLM (Ollama adapter).

    This is a placeholder for future LLM-based extraction.
    For now, it returns an empty dict. The design allows us to:
    1. Send invoice text to a local Ollama instance
    2. Get structured JSON back with supplier, invoice number, date, etc.
    3. Fall back to regex-based extraction if LLM fails

    Args:
        text: Raw text extracted from an invoice page

    Returns:
        Dictionary with extracted fields (currently empty, to be implemented)
    """
    # TODO: Implement Ollama integration
    # Example structure:
    # {
    #     "supplier_name": "...",
    #     "invoice_number": "...",
    #     "invoice_date": "YYYY-MM-DD",
    #     "po_number": "...",
    #     "line_items": [...],
    #     "totals": {...}
    # }
    return {}


def detect_invoice_type(pdf_path: Path) -> str:
    """
    Detect the type of invoice PDF (FCA, bulk supplier, etc.).

    Args:
        pdf_path: Path to the PDF file

    Returns:
        String identifier: "fca", "bulk", or "unknown"
    """
    try:
        reader = PyPDF2.PdfReader(str(pdf_path))
        if not reader.pages:
            return "unknown"

        # Check first page for FCA indicators
        first_page_text = reader.pages[0].extract_text() or ""
        first_page_upper = first_page_text.upper()

        if "FCA CANADA" in first_page_upper or "MOPAR CANADA" in first_page_upper:
            return "fca"

        # Default to bulk (multi-invoice supplier PDF)
        return "bulk"
    except Exception:
        return "unknown"

