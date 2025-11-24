#!/usr/bin/env python3
"""
Retroactively update D2D flags for existing invoices/credit memos
by checking their PDF files.
"""

import sys
sys.path.insert(0, '.')

from sqlmodel import Session, create_engine, select
from core.models import Invoice
from PyPDF2 import PdfReader
import re
from pathlib import Path

DATABASE_URL = "sqlite:///invoices.db"
engine = create_engine(DATABASE_URL, echo=False)

def detect_d2d_from_pdf(pdf_path: str) -> tuple[bool, str | None]:
    """Check PDF for D2D indicators and return (is_d2d, d2d_type)."""
    if not pdf_path or not Path(pdf_path).exists():
        return False, None
    
    try:
        reader = PdfReader(pdf_path)
        combined_text = " ".join([p.extract_text() or "" for p in reader.pages]).upper()
        
        is_d2d = "D2D" in combined_text or "D 2 D" in combined_text or "D-2-D" in combined_text
        d2d_type = None
        
        if is_d2d:
            if "D2D OBSOLETE" in combined_text or "D 2 D OBSOLETE" in combined_text:
                d2d_type = "OBSOLETE"
            elif "D2D GUARANTEED" in combined_text or "D 2 D GUARANTEED" in combined_text:
                d2d_type = "GUARANTEED_INV"
            elif "D2D BACKORDER" in combined_text or "D 2 D BACKORDER" in combined_text:
                d2d_type = "BACKORDER"
        
        return is_d2d, d2d_type
    except Exception as e:
        print(f"Error reading {pdf_path}: {e}")
        return False, None

def main():
    with Session(engine) as session:
        # Get all invoices
        invoices = session.exec(select(Invoice)).all()
        
        print(f"Checking {len(invoices)} invoice(s) for D2D indicators...")
        print("=" * 60)
        
        updated_count = 0
        for inv in invoices:
            if not inv.pdf_path:
                continue
            
            # Try to find the PDF - check multiple locations
            pdf_paths_to_try = [
                Path(inv.pdf_path),
                Path(".") / inv.pdf_path,
            ]
            
            # For FCA invoices, try to find individual split PDFs
            # Normalize invoice number for filename matching
            safe_inv_num = inv.invoice_number.replace(" ", "_").replace("/", "_")
            out_dir = Path("out_invoices")
            if out_dir.exists():
                # Try exact match first
                pdf_paths_to_try.extend([
                    out_dir / f"FCA_{safe_inv_num}.pdf",
                    out_dir / f"FCA_{safe_inv_num}_MAPLE_RIDGE.pdf",
                ])
                
                # Try pattern matching - look for PDFs containing the invoice number
                # Remove spaces and try matching
                inv_num_clean = inv.invoice_number.replace(" ", "").replace("/", "")
                for pdf_file in out_dir.glob("FCA_*.pdf"):
                    pdf_name_upper = pdf_file.name.upper()
                    inv_num_upper = inv_num_clean.upper()
                    if inv_num_upper in pdf_name_upper:
                        pdf_paths_to_try.append(pdf_file)
                        break
            
            pdf_path = None
            for path in pdf_paths_to_try:
                if path.exists():
                    pdf_path = path
                    break
            
            if not pdf_path:
                continue
            
            is_d2d, d2d_type = detect_d2d_from_pdf(str(pdf_path))
            
            # Update if needed
            needs_update = False
            if is_d2d:
                if not inv.is_d2d:
                    needs_update = True
                    print(f"Updating Invoice {inv.id} ({inv.invoice_number}): Setting is_d2d=True, d2d_type={d2d_type}")
                elif inv.d2d_type != d2d_type:
                    needs_update = True
                    print(f"Updating Invoice {inv.id} ({inv.invoice_number}): Changing d2d_type from {inv.d2d_type} to {d2d_type}")
            
            if needs_update:
                inv.is_d2d = True
                inv.d2d_type = d2d_type
                session.add(inv)
                updated_count += 1
            elif not is_d2d and inv.is_d2d:
                # Clear D2D flag if it was incorrectly set
                print(f"Clearing D2D flag for Invoice {inv.id} ({inv.invoice_number})")
                inv.is_d2d = False
                inv.d2d_type = None
                session.add(inv)
                updated_count += 1
        
        session.commit()
        print(f"\n✓ Updated {updated_count} invoice(s)")

if __name__ == "__main__":
    main()

