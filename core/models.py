from datetime import date, datetime
from typing import Optional

from sqlmodel import SQLModel, Field


class Supplier(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    # Your account # with that supplier (e.g. C9033000 for FCA)
    account_number: Optional[str] = None
    # e.g. CDK vendor code (FCA, NAPA, etc.)
    dms_vendor_code: Optional[str] = None


class Invoice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    supplier_id: int = Field(foreign_key="supplier.id")

    invoice_number: str
    invoice_date: date
    po_number: Optional[str] = None

    # Monetary summary fields
    # For FCA:
    #   subtotal       = TOTAL GROSS AMOUNT
    #   freight        = locator + transportation
    #   env_fees       = env.container + env.lubricant
    #   tax_amount     = GST/HST
    #   total_amount   = NET INVOICE AMOUNT
    subtotal: float = 0.0
    freight: float = 0.0
    env_fees: float = 0.0
    tax_amount: float = 0.0
    total_amount: float = 0.0

    # FCA-specific extras used for GL coding
    #   discounts_earned        -> 604900
    #   dealer_generated_return -> affects parts bucket
    #   deposit_values          -> affects parts bucket
    discounts_earned: float = 0.0
    dealer_generated_return: float = 0.0
    deposit_values: float = 0.0

    # draft / reviewed / approved / posted, etc.
    status: str = "draft"

    # Document type: "invoice" or "credit_memo"
    document_type: str = "invoice"

    # D2D (Dealer-to-Dealer) flag for special reporting
    is_d2d: bool = False
    # D2D type: "OBSOLETE", "GUARANTEED_INV", "BACKORDER", or None
    d2d_type: Optional[str] = None

    # Path or filename of the original PDF
    pdf_path: Optional[str] = None


class InvoiceLine(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    invoice_id: int = Field(foreign_key="invoice.id")

    line_number: int = 0

    part_number: str
    description: Optional[str] = None

    qty_billed: float = 0.0
    unit_cost: float = 0.0
    extended_cost: float = 0.0

    is_core: bool = False
    is_env_fee: bool = False
    is_freight: bool = False
    is_discount: bool = False


class ReceivingDocument(SQLModel, table=True):
    """Represents a receiving, packing slip, or check-in document."""

    id: Optional[int] = Field(default=None, primary_key=True)

    supplier_id: int = Field(foreign_key="supplier.id")

    reference: str  # e.g. packing slip #, CDK receiving #, etc.
    received_date: date

    notes: Optional[str] = None


class ReceivingLine(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    receiving_document_id: int = Field(foreign_key="receivingdocument.id")

    part_number: str
    description: Optional[str] = None
    qty_received: float = 0.0


class RadioRequest(SQLModel, table=True):
    """A service department request for a replacement radio."""

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    part_number_request: Optional[str] = None
    part_number: Optional[str] = None
    vin: Optional[str] = None
    customer_number: Optional[str] = None
    warranty_type: Optional[str] = None
