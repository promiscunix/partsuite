from __future__ import annotations

from datetime import date
from typing import List, Optional

import os
import re
import subprocess

from fastapi import FastAPI, Depends, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import SQLModel, Session, create_engine, select
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from core.models import Supplier, Invoice, InvoiceLine, ReceivingDocument, ReceivingLine
from core.fca_parser import parse_fca_pdf
from core.services import process_bulk_pdf, detect_invoice_type
from PyPDF2 import PdfReader, PdfWriter
from pathlib import Path


# -----------------------------------------------------------------------------
# Database setup
# -----------------------------------------------------------------------------

# SQLite DB in the project root
DATABASE_URL = "sqlite:///invoices.db"

engine = create_engine(DATABASE_URL, echo=False)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    with Session(engine) as session:
        yield session


# -----------------------------------------------------------------------------
# Helper schemas (non-table SQLModel/Pydantic models)
# -----------------------------------------------------------------------------

class SupplierCreate(SQLModel):
    name: str
    account_number: Optional[str] = None
    dms_vendor_code: Optional[str] = None


class InvoiceCreate(SQLModel):
    supplier_id: int
    invoice_number: str
    invoice_date: date
    po_number: Optional[str] = None

    subtotal: float
    freight: float
    env_fees: float
    tax_amount: float
    total_amount: float

    # FCA extras
    discounts_earned: float = 0.0
    dealer_generated_return: float = 0.0
    deposit_values: float = 0.0

    status: str = "draft"
    pdf_path: Optional[str] = None


class InvoiceLineCreate(SQLModel):
    line_number: int
    part_number: str
    description: Optional[str] = None
    qty_billed: float
    unit_cost: float
    extended_cost: float
    is_core: bool = False
    is_env_fee: bool = False
    is_freight: bool = False
    is_discount: bool = False


class ReceivingCreate(SQLModel):
    supplier_id: int
    reference: str
    received_date: date
    notes: Optional[str] = None


class ReceivingLineCreate(SQLModel):
    part_number: str
    description: Optional[str] = None
    qty_received: float


# -----------------------------------------------------------------------------
# Coding response models
# -----------------------------------------------------------------------------

class InvoiceCodingLine(BaseModel):
    account: str
    description: str
    amount: float


class InvoiceCodingResponse(BaseModel):
    invoice_id: int
    supplier_name: str
    invoice_number: str
    invoice_date: date
    total_amount: float
    coding: List[InvoiceCodingLine]


class UploadedInvoiceSummary(BaseModel):
    invoice_id: int
    supplier_name: str
    invoice_number: Optional[str]
    invoice_date: Optional[date]
    total_amount: float
    pdf_path: str


class UploadResponse(BaseModel):
    success: bool
    message: str
    invoice_type: str  # "fca" or "bulk"
    invoices_created: int
    invoices_skipped: int = 0  # Duplicates that were skipped
    invoices: List[UploadedInvoiceSummary]


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------

app = FastAPI(title="Invoice Master API")

# Jinja2 templates and static files (CSS)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()

# -----------------------------------------------------------------------------
# Supplier endpoints
# -----------------------------------------------------------------------------

@app.post("/suppliers", response_model=Supplier)
def create_supplier(
    supplier_in: SupplierCreate,
    session: Session = Depends(get_session),
) -> Supplier:
    supplier = Supplier(
        name=supplier_in.name,
        account_number=supplier_in.account_number,
        dms_vendor_code=supplier_in.dms_vendor_code,
    )
    session.add(supplier)
    session.commit()
    session.refresh(supplier)
    return supplier


@app.get("/suppliers", response_model=List[Supplier])
def list_suppliers(session: Session = Depends(get_session)) -> List[Supplier]:
    return session.exec(select(Supplier)).all()


# -----------------------------------------------------------------------------
# Invoice endpoints (manual create / list / get)
# -----------------------------------------------------------------------------

@app.post("/invoices", response_model=Invoice)
def create_invoice(
    invoice_in: InvoiceCreate,
    session: Session = Depends(get_session),
) -> Invoice:
    inv = Invoice(
        supplier_id=invoice_in.supplier_id,
        invoice_number=invoice_in.invoice_number,
        invoice_date=invoice_in.invoice_date,
        po_number=invoice_in.po_number,
        subtotal=invoice_in.subtotal,
        freight=invoice_in.freight,
        env_fees=invoice_in.env_fees,
        tax_amount=invoice_in.tax_amount,
        total_amount=invoice_in.total_amount,
        discounts_earned=invoice_in.discounts_earned,
        dealer_generated_return=invoice_in.dealer_generated_return,
        deposit_values=invoice_in.deposit_values,
        status=invoice_in.status,
        pdf_path=invoice_in.pdf_path,
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)
    return inv


@app.get("/invoices", response_model=List[Invoice])
def list_invoices(session: Session = Depends(get_session)) -> List[Invoice]:
    query = select(Invoice).order_by(Invoice.invoice_date.desc())
    return session.exec(query).all()


@app.get("/invoices/{invoice_id}", response_model=Invoice)
def get_invoice(
    invoice_id: int,
    session: Session = Depends(get_session),
) -> Invoice:
    inv = session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


# -----------------------------------------------------------------------------
# Invoice line endpoints
# -----------------------------------------------------------------------------

@app.post("/invoices/{invoice_id}/lines", response_model=InvoiceLine)
def add_invoice_line(
    invoice_id: int,
    line_in: InvoiceLineCreate,
    session: Session = Depends(get_session),
) -> InvoiceLine:
    inv = session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    line = InvoiceLine(
        invoice_id=invoice_id,
        line_number=line_in.line_number,
        part_number=line_in.part_number,
        description=line_in.description,
        qty_billed=line_in.qty_billed,
        unit_cost=line_in.unit_cost,
        extended_cost=line_in.extended_cost,
        is_core=line_in.is_core,
        is_env_fee=line_in.is_env_fee,
        is_freight=line_in.is_freight,
        is_discount=line_in.is_discount,
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    return line


@app.get("/invoices/{invoice_id}/lines", response_model=List[InvoiceLine])
def list_invoice_lines(
    invoice_id: int,
    session: Session = Depends(get_session),
) -> List[InvoiceLine]:
    inv = session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    query = (
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice_id)
        .order_by(InvoiceLine.line_number)
    )
    return session.exec(query).all()


# -----------------------------------------------------------------------------
# Receiving endpoints
# -----------------------------------------------------------------------------

@app.post("/receivings", response_model=ReceivingDocument)
def create_receiving_document(
    rec_in: ReceivingCreate,
    session: Session = Depends(get_session),
) -> ReceivingDocument:
    supplier = session.get(Supplier, rec_in.supplier_id)
    if not supplier:
        raise HTTPException(status_code=400, detail="Supplier not found")

    rec = ReceivingDocument(
        supplier_id=rec_in.supplier_id,
        reference=rec_in.reference,
        received_date=rec_in.received_date,
        notes=rec_in.notes,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


@app.get("/receivings", response_model=List[ReceivingDocument])
def list_receivings(session: Session = Depends(get_session)) -> List[ReceivingDocument]:
    return session.exec(select(ReceivingDocument)).all()


@app.post("/receivings/{receiving_id}/lines", response_model=ReceivingLine)
def add_receiving_line(
    receiving_id: int,
    line_in: ReceivingLineCreate,
    session: Session = Depends(get_session),
) -> ReceivingLine:
    rec = session.get(ReceivingDocument, receiving_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Receiving document not found")

    line = ReceivingLine(
        receiving_document_id=receiving_id,
        part_number=line_in.part_number,
        description=line_in.description,
        qty_received=line_in.qty_received,
    )
    session.add(line)
    session.commit()
    session.refresh(line)
    return line


@app.get("/receivings/{receiving_id}/lines", response_model=List[ReceivingLine])
def list_receiving_lines(
    receiving_id: int,
    session: Session = Depends(get_session),
) -> List[ReceivingLine]:
    rec = session.get(ReceivingDocument, receiving_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Receiving document not found")

    query = select(ReceivingLine).where(
        ReceivingLine.receiving_document_id == receiving_id
    )
    return session.exec(query).all()


# -----------------------------------------------------------------------------
# FCA upload endpoint
# -----------------------------------------------------------------------------

DATA_DIR = "data/invoices"
OUTPUT_DIR = "out_invoices"


@app.post("/invoices/upload-fca", response_model=Invoice)
async def upload_fca_invoice(
    supplier_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> Invoice:
    supplier = session.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(
            status_code=400, detail=f"Supplier with id={supplier_id} does not exist"
        )

    os.makedirs(DATA_DIR, exist_ok=True)
    pdf_path = os.path.join(DATA_DIR, file.filename)
    contents = await file.read()
    with open(pdf_path, "wb") as f:
        f.write(contents)

    parsed_invoices = parse_fca_pdf(pdf_path)
    if not parsed_invoices:
        raise HTTPException(status_code=400, detail="No invoices found in FCA PDF.")

    first = parsed_invoices[0]
    header = first["header"]
    lines = first["lines"]

    invoice_date = header.get("invoice_date")
    if isinstance(invoice_date, str):
        invoice_date = date.fromisoformat(invoice_date)

    inv = Invoice(
        supplier_id=supplier_id,
        invoice_number=header["invoice_number"],
        invoice_date=invoice_date,
        po_number=header.get("po_number"),
        subtotal=header.get("subtotal", 0.0),
        freight=header.get("freight", 0.0),
        env_fees=header.get("env_fees", 0.0),
        tax_amount=header.get("tax_amount", 0.0),
        total_amount=header.get("total_amount", 0.0),
        discounts_earned=header.get("discounts_earned", 0.0),
        dealer_generated_return=header.get("dealer_generated_return", 0.0),
        deposit_values=header.get("deposit_values", 0.0),
        status="draft",
        pdf_path=pdf_path,
    )

    session.add(inv)
    session.commit()
    session.refresh(inv)

    for line in lines:
        il = InvoiceLine(
            invoice_id=inv.id,
            line_number=line["line_number"],
            part_number=line["part_number"],
            description=line.get("description"),
            qty_billed=line.get("qty_billed", 0.0),
            unit_cost=line.get("unit_cost", 0.0),
            extended_cost=line.get("extended_cost", 0.0),
            is_core=line.get("is_core", False),
            is_env_fee=line.get("is_env_fee", False),
            is_freight=line.get("is_freight", False),
            is_discount=line.get("is_discount", False),
        )
        session.add(il)

    session.commit()
    session.refresh(inv)
    return inv


# -----------------------------------------------------------------------------
# Helper function to check for duplicate invoices
# -----------------------------------------------------------------------------

def find_existing_invoice(
    session: Session,
    supplier_id: int,
    invoice_number: str,
    invoice_date: date,
) -> Optional[Invoice]:
    """
    Check if an invoice with the same supplier, invoice number, and date already exists.
    
    Returns the existing invoice if found, None otherwise.
    """
    return session.exec(
        select(Invoice).where(
            Invoice.supplier_id == supplier_id,
            Invoice.invoice_number == invoice_number,
            Invoice.invoice_date == invoice_date,
        )
    ).first()


# -----------------------------------------------------------------------------
# Generic bulk PDF upload endpoint
# -----------------------------------------------------------------------------

@app.post("/upload", response_model=UploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    do_ocr: str = Form("false"),
    session: Session = Depends(get_session),
) -> UploadResponse:
    """
    Upload a bulk PDF containing one or more invoices.

    This endpoint:
    - Detects if the PDF is FCA/Mopar or a bulk supplier PDF
    - Processes it accordingly
    - Splits into individual invoices
    - Extracts metadata and line items
    - Saves to database

    Args:
        file: PDF file to upload
        do_ocr: Whether to run OCR (for image-only PDFs)
    """
    # Save uploaded file temporarily
    os.makedirs(DATA_DIR, exist_ok=True)
    pdf_path = Path(DATA_DIR) / file.filename
    contents = await file.read()
    with open(pdf_path, "wb") as f:
        f.write(contents)

    # Detect invoice type
    invoice_type = detect_invoice_type(pdf_path)
    created_invoices: List[UploadedInvoiceSummary] = []
    skipped_count = 0

    if invoice_type == "fca":
        # Use existing FCA parser
        parsed_invoices = parse_fca_pdf(str(pdf_path))
        if not parsed_invoices:
            raise HTTPException(
                status_code=400, detail="No invoices found in FCA PDF."
            )

        # For FCA, we need a supplier. Try to find or create FCA supplier
        fca_supplier = session.exec(
            select(Supplier).where(Supplier.name.like("%FCA%") | Supplier.name.like("%Mopar%"))
        ).first()

        if not fca_supplier:
            # Create FCA supplier if it doesn't exist
            fca_supplier = Supplier(
                name="FCA Canada / Mopar",
                account_number=None,
                dms_vendor_code=None,
            )
            session.add(fca_supplier)
            session.commit()
            session.refresh(fca_supplier)

        # Split FCA PDF into individual invoice PDFs (like we do for bulk invoices)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_dir = Path(OUTPUT_DIR)
        
        # Group pages by invoice number to create individual PDFs
        from PyPDF2 import PdfReader, PdfWriter
        reader = PdfReader(str(pdf_path))
        
        # Build a mapping of invoice numbers to page indices
        # Use the same logic as parse_fca_pdf to ensure consistency
        # Handle both INVOICE NUMBER and CREDIT MEMO NUMBER
        invoice_to_pages: dict[str, list[int]] = {}
        for page_idx, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            # Check for invoice number first
            m_num = re.search(r"INVOICE NUMBER:\s*(.+)", text, re.IGNORECASE)
            # If not found, check for credit memo number
            if not m_num:
                m_num = re.search(r"CREDIT MEMO NUMBER:\s*(.+)", text, re.IGNORECASE)
            if m_num:
                # Normalize the invoice number (strip and handle whitespace)
                inv_num = m_num.group(1).strip()
                # Normalize multiple spaces to single space for consistency
                inv_num = re.sub(r"\s+", " ", inv_num)
                invoice_to_pages.setdefault(inv_num, []).append(page_idx)
        
        # Create individual PDF files for each invoice
        fca_pdf_paths: dict[str, Path] = {}
        for inv_num, page_indices in invoice_to_pages.items():
            writer = PdfWriter()
            for page_idx in page_indices:
                writer.add_page(reader.pages[page_idx])
            
            # Create filename (safe for filesystem)
            safe_inv_num = re.sub(r"[^A-Za-z0-9_.-]+", "_", inv_num)
            out_filename = f"FCA_{safe_inv_num}.pdf"
            out_path = output_dir / out_filename
            
            with open(out_path, "wb") as f:
                writer.write(f)
            
            fca_pdf_paths[inv_num] = out_path

        # Process each FCA invoice
        for parsed in parsed_invoices:
            header = parsed["header"]
            lines = parsed["lines"]

            invoice_date = header.get("invoice_date")
            if isinstance(invoice_date, str):
                invoice_date = date.fromisoformat(invoice_date)

            invoice_number = header["invoice_number"]
            
            # Get the individual PDF path for this invoice
            # Try exact match first, then try normalized versions
            individual_pdf_path = fca_pdf_paths.get(invoice_number)
            if not individual_pdf_path:
                # Try with normalized invoice number (remove extra spaces)
                normalized_inv_num = re.sub(r"\s+", " ", invoice_number).strip()
                individual_pdf_path = fca_pdf_paths.get(normalized_inv_num)
            if not individual_pdf_path:
                # Fallback to original PDF if individual PDF wasn't created
                individual_pdf_path = pdf_path

            # Check for duplicate
            existing_inv = find_existing_invoice(
                session, fca_supplier.id, invoice_number, invoice_date
            )
            if existing_inv:
                # Invoice already exists, skip it
                skipped_count += 1
                created_invoices.append(
                    UploadedInvoiceSummary(
                        invoice_id=existing_inv.id,
                        supplier_name=fca_supplier.name,
                        invoice_number=existing_inv.invoice_number,
                        invoice_date=existing_inv.invoice_date,
                        total_amount=existing_inv.total_amount,
                        pdf_path=existing_inv.pdf_path or str(individual_pdf_path),
                    )
                )
                continue

            inv = Invoice(
                supplier_id=fca_supplier.id,
                invoice_number=header["invoice_number"],
                invoice_date=invoice_date,
                po_number=header.get("po_number"),
                subtotal=header.get("subtotal", 0.0),
                freight=header.get("freight", 0.0),
                env_fees=header.get("env_fees", 0.0),
                tax_amount=header.get("tax_amount", 0.0),
                total_amount=header.get("total_amount", 0.0),
                discounts_earned=header.get("discounts_earned", 0.0),
                dealer_generated_return=header.get("dealer_generated_return", 0.0),
                deposit_values=header.get("deposit_values", 0.0),
                status="draft",
                document_type=header.get("document_type", "invoice"),
                is_d2d=header.get("is_d2d", False),
                d2d_type=header.get("d2d_type"),
                pdf_path=str(individual_pdf_path),
            )

            session.add(inv)
            session.commit()
            session.refresh(inv)

            for line in lines:
                il = InvoiceLine(
                    invoice_id=inv.id,
                    line_number=line["line_number"],
                    part_number=line["part_number"],
                    description=line.get("description"),
                    qty_billed=line.get("qty_billed", 0.0),
                    unit_cost=line.get("unit_cost", 0.0),
                    extended_cost=line.get("extended_cost", 0.0),
                    is_core=line.get("is_core", False),
                    is_env_fee=line.get("is_env_fee", False),
                    is_freight=line.get("is_freight", False),
                    is_discount=line.get("is_discount", False),
                )
                session.add(il)

            session.commit()
            session.refresh(inv)

            created_invoices.append(
                UploadedInvoiceSummary(
                    invoice_id=inv.id,
                    supplier_name=fca_supplier.name,
                    invoice_number=inv.invoice_number,
                    invoice_date=inv.invoice_date,
                    total_amount=inv.total_amount,
                    pdf_path=str(individual_pdf_path),
                )
            )

    else:
        # Process as bulk supplier PDF
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_dir = Path(OUTPUT_DIR)

        # Convert string to bool
        do_ocr_bool = do_ocr.lower() in ("true", "1", "yes", "on")

        try:
            invoices_data, pdf_paths = process_bulk_pdf(
                pdf_path, output_dir, do_ocr=do_ocr_bool
            )
        except FileNotFoundError:
            raise HTTPException(
                status_code=500,
                detail="OCR requested but ocrmypdf not found. Install ocrmypdf or set do_ocr=false.",
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(
                status_code=500,
                detail=f"OCR failed: {e}",
            )

        if not invoices_data:
            raise HTTPException(
                status_code=400, detail="No invoices detected in PDF. Check OCR quality and invoice patterns."
            )

        # Ensure we have matching counts
        if len(invoices_data) != len(pdf_paths):
            raise HTTPException(
                status_code=500,
                detail=f"Mismatch: detected {len(invoices_data)} invoices but {len(pdf_paths)} PDF files generated."
            )

        # Save each invoice to database
        for idx, (inv_data, inv_pdf_path) in enumerate(zip(invoices_data, pdf_paths), 1):
            # Find or create supplier
            supplier = None
            if inv_data.supplier_name:
                supplier = session.exec(
                    select(Supplier).where(Supplier.name == inv_data.supplier_name)
                ).first()

                if not supplier:
                    supplier = Supplier(
                        name=inv_data.supplier_name,
                        account_number=None,
                        dms_vendor_code=None,
                    )
                    session.add(supplier)
                    session.commit()
                    session.refresh(supplier)

            if not supplier:
                # Skip invoices without supplier (but log it)
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Skipping invoice {idx}/{len(invoices_data)}: no supplier name detected")
                continue

            # Parse invoice date
            invoice_date_obj = None
            if inv_data.invoice_date:
                try:
                    invoice_date_obj = date.fromisoformat(inv_data.invoice_date)
                except ValueError:
                    pass

            if not invoice_date_obj:
                invoice_date_obj = date.today()

            # Calculate totals
            subtotal = inv_data.subtotal or 0.0
            tax_amount = sum(inv_data.taxes.values()) if inv_data.taxes else 0.0
            total_amount = inv_data.total or subtotal + tax_amount

            invoice_number = inv_data.invoice_number or f"UNKNOWN-{inv_data.pages[0]}"

            # Check for duplicate
            existing_inv = find_existing_invoice(
                session, supplier.id, invoice_number, invoice_date_obj
            )
            if existing_inv:
                # Invoice already exists, skip it
                skipped_count += 1
                created_invoices.append(
                    UploadedInvoiceSummary(
                        invoice_id=existing_inv.id,
                        supplier_name=supplier.name,
                        invoice_number=existing_inv.invoice_number,
                        invoice_date=existing_inv.invoice_date,
                        total_amount=existing_inv.total_amount,
                        pdf_path=existing_inv.pdf_path or str(inv_pdf_path),
                    )
                )
                continue

            inv = Invoice(
                supplier_id=supplier.id,
                invoice_number=invoice_number,
                invoice_date=invoice_date_obj,
                po_number=inv_data.po_number,
                subtotal=subtotal,
                freight=0.0,  # Not extracted for bulk invoices yet
                env_fees=0.0,
                tax_amount=tax_amount,
                total_amount=total_amount,
                discounts_earned=0.0,
                dealer_generated_return=0.0,
                deposit_values=0.0,
                status="draft",
                pdf_path=str(inv_pdf_path),
            )

            session.add(inv)
            session.commit()
            session.refresh(inv)

            # Add line items
            for line_idx, line_item in enumerate(inv_data.line_items, start=1):
                il = InvoiceLine(
                    invoice_id=inv.id,
                    line_number=line_idx,
                    part_number=line_item.part_number or "",
                    description=line_item.description,
                    qty_billed=line_item.quantity or 0.0,
                    unit_cost=line_item.unit_price or 0.0,
                    extended_cost=line_item.line_total or 0.0,
                    is_core=False,
                    is_env_fee=False,
                    is_freight=False,
                    is_discount=False,
                )
                session.add(il)

            session.commit()
            session.refresh(inv)

            created_invoices.append(
                UploadedInvoiceSummary(
                    invoice_id=inv.id,
                    supplier_name=supplier.name,
                    invoice_number=inv.invoice_number,
                    invoice_date=inv.invoice_date,
                    total_amount=inv.total_amount,
                    pdf_path=str(inv_pdf_path),
                )
            )

    new_count = len(created_invoices) - skipped_count
    total_detected = len(created_invoices)
    
    if skipped_count > 0:
        message = f"Detected {total_detected} invoice(s) in PDF: {new_count} new, {skipped_count} duplicate(s) skipped"
    else:
        message = f"Detected and processed {total_detected} invoice(s) from PDF"

    return UploadResponse(
        success=True,
        message=message,
        invoice_type=invoice_type,
        invoices_created=new_count,
        invoices_skipped=skipped_count,
        invoices=created_invoices,
    )


# -----------------------------------------------------------------------------
# Invoice coding endpoint (GL mapping)
# -----------------------------------------------------------------------------

def compute_fca_coding(inv: Invoice) -> List[InvoiceCodingLine]:
    """
    Rule set we agreed on:

    For Invoices:
      104000 Parts: subtotal + dealer_generated_return + deposit_values + env_fees
      704004 Freight: freight (locator + transportation)
      604900 Discounts: discounts_earned  (credit)
      201105 GST/HST: tax_amount

    For Credit Memos:
      Same accounts, but amounts are already negative (credit memos have negative values)
      So we use the same logic - the negative amounts will flow through correctly
    """
    parts_amount = (
        inv.subtotal
        + inv.dealer_generated_return
        + inv.deposit_values
        + inv.env_fees
    )
    freight_amount = inv.freight
    discounts_amount = inv.discounts_earned
    gst_amount = inv.tax_amount

    # For credit memos, amounts are already negative, so they'll code as credits
    doc_type_label = "Credit Memo" if inv.document_type == "credit_memo" else "Invoice"

    return [
        InvoiceCodingLine(
            account="104000",
            description=f"Parts ({doc_type_label})",
            amount=round(parts_amount, 2),
        ),
        InvoiceCodingLine(
            account="704004",
            description=f"Freight (locator + transportation) ({doc_type_label})",
            amount=round(freight_amount, 2),
        ),
        InvoiceCodingLine(
            account="604900",
            description="Discounts (credit)",
            amount=round(discounts_amount, 2),
        ),
        InvoiceCodingLine(
            account="201105",
            description=f"GST/HST ({doc_type_label})",
            amount=round(gst_amount, 2),
        ),
    ]


@app.get("/invoices/{invoice_id}/coding", response_model=InvoiceCodingResponse)
def get_invoice_coding(
    invoice_id: int,
    session: Session = Depends(get_session),
) -> InvoiceCodingResponse:
    inv = session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    supplier = session.get(Supplier, inv.supplier_id)
    supplier_name = supplier.name if supplier else "Unknown supplier"

    coding_lines = compute_fca_coding(inv)

    return InvoiceCodingResponse(
        invoice_id=inv.id,
        supplier_name=supplier_name,
        invoice_number=inv.invoice_number,
        invoice_date=inv.invoice_date,
        total_amount=inv.total_amount,
        coding=coding_lines,
    )


# -----------------------------------------------------------------------------
# Invoice summary (last page only) PDF endpoint
# -----------------------------------------------------------------------------

SUMMARY_DIR = "data/summary_pages"


@app.get("/invoices/{invoice_id}/summary-pdf")
def get_invoice_summary_pdf(
    invoice_id: int,
    session: Session = Depends(get_session),
):
    inv = session.get(Invoice, invoice_id)
    if not inv or not inv.pdf_path:
        raise HTTPException(status_code=404, detail="Invoice or PDF not found")

    pdf_path = inv.pdf_path
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="Stored PDF file not found")

    reader = PdfReader(pdf_path)
    if len(reader.pages) == 0:
        raise HTTPException(status_code=400, detail="PDF has no pages")

    writer = PdfWriter()
    writer.add_page(reader.pages[-1])  # last page (summary)

    os.makedirs(SUMMARY_DIR, exist_ok=True)
    out_path = os.path.join(SUMMARY_DIR, f"{inv.invoice_number}_summary.pdf")
    with open(out_path, "wb") as f:
        writer.write(f)

    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=os.path.basename(out_path),
    )
@app.get("/", response_class=HTMLResponse)
def ui_home(request: Request) -> HTMLResponse:
    """
    Simple home page that just links to invoice list.
    """
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
        },
    )


@app.get("/ui/invoices", response_class=HTMLResponse)
def ui_list_invoices(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """
    Human-friendly invoice list with supplier, date, total, link to details.
    """
    invoices = session.exec(select(Invoice).order_by(Invoice.invoice_date.desc())).all()
    # Attach supplier names to each invoice
    ui_rows = []
    for inv in invoices:
        supplier = session.get(Supplier, inv.supplier_id)
        ui_rows.append(
            {
                "invoice": inv,
                "supplier_name": supplier.name if supplier else "Unknown supplier",
            }
        )

    return templates.TemplateResponse(
        "invoice_list.html",
        {
            "request": request,
            "rows": ui_rows,
        },
    )


@app.get("/ui/invoices/{invoice_id}", response_class=HTMLResponse)
def ui_invoice_detail(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """
    Detail view: header, totals, GL coding, line items.
    """
    inv = session.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    supplier = session.get(Supplier, inv.supplier_id)
    supplier_name = supplier.name if supplier else "Unknown supplier"

    # Reuse our GL coding logic for a nice table
    coding = compute_fca_coding(inv)

    # Get invoice lines
    lines = session.exec(
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice_id)
        .order_by(InvoiceLine.line_number)
    ).all()

    return templates.TemplateResponse(
        "invoice_detail.html",
        {
            "request": request,
            "invoice": inv,
            "supplier_name": supplier_name,
            "lines": lines,
            "coding": coding,
        },
    )
