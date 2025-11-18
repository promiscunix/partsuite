#!/usr/bin/env python3
"""
parse_fca_invoice.py

Parse an FCA / Mopar Canada "PARTS INVOICE" PDF into structured line items
and insert it into the SAME database schema used by invoice_pipeline.py.

- Treats the whole PDF as ONE invoice (invoice_number, invoice_date).
- Each "ORD#:" block (0310300-XXXX ORD#: ...) is context for following lines.
- Each numbered line (starting with line number and part number) becomes a line item.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from PyPDF2 import PdfReader

# Reuse your existing DB helpers and data models
import invoice_pipeline as base
# base.LineItem, base.InvoiceData, base.init_db, base.insert_invoice_into_db


@dataclass
class FCAHeader:
    supplier: str
    invoice_number: str
    invoice_date: str  # ISO (YYYY-MM-DD) if we can parse, else raw string
    pages: int
    total_invoice: Optional[float] = None


@dataclass
class FCAContext:
    location: Optional[str] = None
    order_number: Optional[str] = None
    order_type: Optional[str] = None
    order_date: Optional[str] = None  # YYYY-MM-DD from the invoice


@dataclass
class FCALineItem:
    invoice_number: str
    invoice_date: str
    page: int

    location: Optional[str]
    order_number: Optional[str]
    order_type: Optional[str]
    order_date: Optional[str]

    line_no: str
    part_no: str
    description: str
    qty: float
    unit_price: float
    gross_amount: float
    net_amount: float
    s_code: str


INVOICE_NO_RE = re.compile(r"INVOICE NUMBER:\s*(.+)")
INVOICE_DATE_RE = re.compile(r"INVOICE DATE\s*:\s*(.+)", re.IGNORECASE)
TOTAL_INVOICE_RE = re.compile(r"TOTAL THIS INVOICE", re.IGNORECASE)


def parse_invoice_date(text: str) -> str:
    """Parse 'NOVEMBER 15, 2025' -> '2025-11-15' if possible."""
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    # Fallback to original
    return text


def parse_order_header(line: str) -> Optional[FCAContext]:
    """
    Example:
      '0310300-3618853 ORD#: T1103F  O/T:E DATE:  2025-11-03'
    """
    if "ORD#:" not in line or "DATE:" not in line:
        return None

    tokens = line.split()
    if not tokens:
        return None

    location = tokens[0]

    order_number = None
    order_type = None
    order_date = None

    # ORD#: <value>
    if "ORD#:" in tokens:
        idx = tokens.index("ORD#:") + 1
        if idx < len(tokens):
            order_number = tokens[idx]

    # O/T:E (single token)
    for tok in tokens:
        if tok.startswith("O/T:"):
            order_type = tok.split(":", 1)[1] or None

    # DATE: YYYY-MM-DD
    if "DATE:" in tokens:
        idx = tokens.index("DATE:") + 1
        if idx < len(tokens):
            order_date = tokens[idx]

    return FCAContext(
        location=location,
        order_number=order_number,
        order_type=order_type,
        order_date=order_date,
    )


def parse_mopar_part_line(raw_line: str) -> Optional[tuple[str, str, str, float, float, float, float, str]]:
    """
    Parse a single Mopar part line.

    Example line tokens:
      1 BAAUA200AB BATTERY     10    193.05   1930.50 22  0       .00   1930.50 B

    We treat trailing 8 tokens as:
      qty, unit_price, gross_amount, dc_percent, dc_misc, dc_amount, net_amount, s_code

    We only care about qty, unit_price, gross_amount, net_amount, s_code.
    Description is everything between part_no and qty.
    """
    tokens = raw_line.split()
    # Must at least have: line_no, part_no, desc, qty, unit, gross, dc%, dc_x, dc_amt, net, code
    if len(tokens) < 11:
        return None

    # First token must be a line number
    if not tokens[0].isdigit():
        return None

    line_no = tokens[0]
    part_no = tokens[1]

    # Last 8 are numeric fields + S code
    qty_tok = tokens[-8]
    unit_tok = tokens[-7]
    gross_tok = tokens[-6]
    net_tok = tokens[-2]
    s_code = tokens[-1]

    # Description is everything between part_no and qty
    desc_tokens = tokens[2 : len(tokens) - 8]
    description = " ".join(desc_tokens).strip()

    def to_float(s: str) -> float:
        s = s.replace(",", "")
        if s == "." or s == "":
            return 0.0
        # Handle '.65' style values
        if s.startswith("."):
            s = "0" + s
        return float(s)

    try:
        qty = to_float(qty_tok)
        unit_price = to_float(unit_tok)
        gross = to_float(gross_tok)
        net = to_float(net_tok)
    except ValueError:
        return None

    return line_no, part_no, description, qty, unit_price, gross, net, s_code


def extract_fca_invoice(pdf_path: Path) -> tuple[FCAHeader, List[FCALineItem]]:
    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)

    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    total_invoice: Optional[float] = None

    ctx = FCAContext()
    items: List[FCALineItem] = []

    for page_index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = raw_line.rstrip()

            # Header: invoice number
            m_no = INVOICE_NO_RE.search(line)
            if m_no and invoice_number is None:
                invoice_number = m_no.group(1).strip()
                continue

            # Header: invoice date
            m_dt = INVOICE_DATE_RE.search(line)
            if m_dt and invoice_date is None:
                invoice_date = parse_invoice_date(m_dt.group(1))
                continue

            # Total this invoice
            if TOTAL_INVOICE_RE.search(line):
                # e.g. "TOTAL THIS INVOICE              1800.23          1800.23"
                toks = line.split()
                nums = [t for t in toks if re.fullmatch(r"[0-9]*\.?[0-9]+", t)]
                if nums:
                    try:
                        total_invoice = float(nums[-1])
                    except ValueError:
                        pass

            # Order header
            ord_ctx = parse_order_header(line)
            if ord_ctx:
                ctx = ord_ctx
                continue

            # Part line: must start with digit then part number
            if re.match(r"^\s*\d+\s+[A-Z0-9]", raw_line):
                parsed = parse_mopar_part_line(raw_line)
                if parsed and invoice_number and invoice_date:
                    (
                        line_no,
                        part_no,
                        description,
                        qty,
                        unit_price,
                        gross_amount,
                        net_amount,
                        s_code,
                    ) = parsed

                    items.append(
                        FCALineItem(
                            invoice_number=invoice_number,
                            invoice_date=invoice_date,
                            page=page_index,
                            location=ctx.location,
                            order_number=ctx.order_number,
                            order_type=ctx.order_type,
                            order_date=ctx.order_date,
                            line_no=line_no,
                            part_no=part_no,
                            description=description,
                            qty=qty,
                            unit_price=unit_price,
                            gross_amount=gross_amount,
                            net_amount=net_amount,
                            s_code=s_code,
                        )
                    )

    if invoice_number is None:
        raise RuntimeError("Could not find INVOICE NUMBER in FCA invoice.")
    if invoice_date is None:
        raise RuntimeError("Could not find INVOICE DATE in FCA invoice.")

    header = FCAHeader(
        supplier="Mopar Canada Inc. - Parts Invoice",
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        pages=pages,
        total_invoice=total_invoice,
    )
    return header, items


# --- DB integration using invoice_pipeline helpers ---------------------------

def insert_into_main_db(
    db_path: Path,
    header: FCAHeader,
    items: List[FCALineItem],
    pdf_path: Path,
) -> None:
    """
    Insert the FCA invoice into the SAME schema used by invoice_pipeline.py:

    - Use base.init_db(db_path) to get a connection and ensure schema exists.
    - Convert FCALineItem -> base.LineItem.
    - Convert FCAHeader + list[LineItem] -> base.InvoiceData.
    - Call base.insert_invoice_into_db(conn, invoice, pdf_path).
    """
    conn = base.init_db(str(db_path))

    subtotal = sum(it.net_amount for it in items)
    total = header.total_invoice if header.total_invoice is not None else subtotal

    base_items: List[base.LineItem] = []
    for it in items:
        # Include order info in description so you can link to CDK data later
        desc_parts = [it.description]
        ctx_bits = []
        if it.order_number:
            ctx_bits.append(f"ORD {it.order_number}")
        if it.order_date:
            ctx_bits.append(f"DATE {it.order_date}")
        if it.location:
            ctx_bits.append(f"LOC {it.location}")
        if ctx_bits:
            desc_parts.append("(" + " ".join(ctx_bits) + ")")
        full_desc = " ".join(desc_parts)

        base_items.append(
            base.LineItem(
                raw_line="",  # raw text line (optional)
                part_number=it.part_no,
                description=full_desc,
                quantity=it.qty,
                unit_price=it.unit_price,
                line_total=it.net_amount,
            )
        )

    inv = base.InvoiceData(
        supplier_name=header.supplier,
        supplier_type="chrysler_corp",
        invoice_number=header.invoice_number,
        invoice_date=header.invoice_date,
        po_number=None,
        subtotal=subtotal,
        taxes={},   # can extend later if you want per-tax breakdown
        total=total,
        pages=list(range(header.pages)),  # 0-based pages
        line_items=base_items,
        raw_text="",  # not needed for now
    )

    base.insert_invoice_into_db(conn, inv, pdf_path)
    conn.close()


# --- CLI --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse an FCA/Mopar Canada parts invoice PDF into structured data."
    )
    ap.add_argument("pdf", type=Path, help="FCA invoice PDF (e.g. 11_15.pdf)")
    ap.add_argument(
        "--csv",
        type=Path,
        help="Optional CSV output path for line items (for inspection).",
    )
    ap.add_argument(
        "--db",
        type=Path,
        help="Optional SQLite database (same one used by invoice_pipeline.py, e.g. invoices.db).",
    )

    args = ap.parse_args()

    header, items = extract_fca_invoice(args.pdf)

    # CSV output (for debugging / manual inspection)
    if args.csv:
        fieldnames = [
            "invoice_number",
            "invoice_date",
            "page",
            "location",
            "order_number",
            "order_type",
            "order_date",
            "line_no",
            "part_no",
            "description",
            "qty",
            "unit_price",
            "gross_amount",
            "net_amount",
            "s_code",
        ]
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for it in items:
                w.writerow(
                    {
                        "invoice_number": it.invoice_number,
                        "invoice_date": it.invoice_date,
                        "page": it.page,
                        "location": it.location,
                        "order_number": it.order_number,
                        "order_type": it.order_type,
                        "order_date": it.order_date,
                        "line_no": it.line_no,
                        "part_no": it.part_no,
                        "description": it.description,
                        "qty": it.qty,
                        "unit_price": it.unit_price,
                        "gross_amount": it.gross_amount,
                        "net_amount": it.net_amount,
                        "s_code": it.s_code,
                    }
                )

    # DB insert
    if args.db:
        insert_into_main_db(args.db, header, items, args.pdf)
        print(
            f"Inserted FCA invoice {header.invoice_number} "
            f"({header.invoice_date}) into {args.db} with {len(items)} line items."
        )
    else:
        # If no DB, at least show a quick summary on stdout
        print(
            f"FCA invoice {header.invoice_number} ({header.invoice_date}), "
            f"{len(items)} line items, total={header.total_invoice}"
        )
        for it in items[:10]:
            print(
                f"{it.order_number or ''} {it.order_date or ''} "
                f"{it.part_no} {it.description} x{int(it.qty)} "
                f"@ {it.unit_price:.2f} = {it.net_amount:.2f}"
            )


if __name__ == "__main__":
    main()
