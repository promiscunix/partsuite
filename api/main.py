from __future__ import annotations

from datetime import date
from typing import List, Optional

import os

from fastapi import FastAPI, Depends, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import SQLModel, Session, create_engine, select

from core.models import Supplier, Invoice, InvoiceLine, ReceivingDocument, ReceivingLine
from core.fca_parser import parse_fca_pdf
from PyPDF2 import PdfReader, PdfWriter


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


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------

app = FastAPI(title="Invoice Master API")


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
# Invoice coding endpoint (GL mapping)
# -----------------------------------------------------------------------------

def compute_fca_coding(inv: Invoice) -> List[InvoiceCodingLine]:
    """
    Rule set we agreed on:

      104000 Parts: subtotal + dealer_generated_return + deposit_values + env_fees
      704004 Freight: freight (locator + transportation)
      604900 Discounts: discounts_earned  (credit)
      201105 GST/HST: tax_amount
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

    # For sanity if you ever want to debug:
    # calc_net = parts_amount + freight_amount + gst_amount - discounts_amount

    return [
        InvoiceCodingLine(
            account="104000",
            description="Parts",
            amount=round(parts_amount, 2),
        ),
        InvoiceCodingLine(
            account="704004",
            description="Freight (locator + transportation)",
            amount=round(freight_amount, 2),
        ),
        InvoiceCodingLine(
            account="604900",
            description="Discounts (credit)",
            amount=round(discounts_amount, 2),
        ),
        InvoiceCodingLine(
            account="201105",
            description="GST/HST",
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
