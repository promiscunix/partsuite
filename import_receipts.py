#!/usr/bin/env python3
"""
import_receipts.py

Import CDK transaction CSV into invoices.db as "receipts" data.

- Only rows with TRANSCODE. in {"R", "O"} are treated as received.
- R = FCA shipment (Mopar Canada / FCA)
- O = Manual entry, matches external supplier invoices (NAPA, Lordco, etc.)
- DOES NOT modify existing invoices/line_items tables.
- Adds two new tables (if they don't exist yet):
    receipts_batches, receipts_lines
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

RECEIPT_CODES = {"R", "O"}

# Column names as they appear in your CSV
COL_PART = "PARTNUMBER"
COL_CODE = "TRANSCODE."
COL_QTY = "TRANSQTY.."
COL_INV = "INVOICENUMBER.."
COL_DATE = "POSTINGDATE..."


def ensure_receipts_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            imported_at TEXT,
            filename TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER REFERENCES receipts_batches(id),
            supplier_name TEXT,
            invoice_number TEXT,
            part_number TEXT,
            qty_received REAL,
            posting_date TEXT,
            transcode TEXT,
            raw_json TEXT
        )
        """
    )
    conn.commit()


def parse_posting_date(value: str) -> Optional[str]:
    """
    Convert '11/10/2025 12:00:00 AM' -> '2025-11-10' where possible.
    If parsing fails, return the original string.
    """
    value = (value or "").strip()
    if not value:
        return None

    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def supplier_for_transcode(code: str) -> str:
    """
    Map CDK transcode to a logical supplier bucket.

    R = scanned in with FCA shipment (Mopar Canada / FCA)
    O = manually entered by Dale (external supplier invoices)
    """
    code = code.upper()
    if code == "R":
        return "Mopar Canada / FCA"
    if code == "O":
        return "Manual / External Supplier"
    return "Unknown"


def import_receipts(csv_path: Path, db_path: Path, source: str) -> None:
    conn = sqlite3.connect(db_path)
    ensure_receipts_schema(conn)
    cur = conn.cursor()

    imported_at = datetime.utcnow().isoformat(timespec="seconds")

    # Create a batch record for this CSV import
    cur.execute(
        """
        INSERT INTO receipts_batches (source, imported_at, filename)
        VALUES (?, ?, ?)
        """,
        (source, imported_at, str(csv_path)),
    )
    batch_id = cur.lastrowid

    total_rows = 0
    used_rows = 0

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1

            code = (row.get(COL_CODE) or "").strip().upper()
            if code not in RECEIPT_CODES:
                continue

            part = (row.get(COL_PART) or "").strip()
            if not part:
                continue

            qty_str = (row.get(COL_QTY) or "").strip()
            if not qty_str:
                continue

            try:
                qty = float(qty_str)
            except ValueError:
                continue

            if qty == 0:
                continue

            inv_num = (row.get(COL_INV) or "").strip() or None
            posting_raw = (row.get(COL_DATE) or "").strip()
            posting_date = parse_posting_date(posting_raw)

            supplier_name = supplier_for_transcode(code)
            raw_json = json.dumps(row, default=str)

            cur.execute(
                """
                INSERT INTO receipts_lines (
                    batch_id,
                    supplier_name,
                    invoice_number,
                    part_number,
                    qty_received,
                    posting_date,
                    transcode,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    supplier_name,
                    inv_num,
                    part,
                    qty,
                    posting_date,
                    code,
                    raw_json,
                ),
            )
            used_rows += 1

    conn.commit()
    conn.close()

    print(
        f"Imported {used_rows} receipt lines (from {total_rows} CSV rows) "
        f"into batch {batch_id}."
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import CDK transaction CSV (R/O rows) as receipts into invoices.db"
    )
    ap.add_argument(
        "csv",
        type=Path,
        help="CDK transactions CSV (e.g. 6ed29828-....csv)",
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=Path("invoices.db"),
        help="SQLite DB path (default: invoices.db)",
    )
    ap.add_argument(
        "--source",
        default="CDK-transactions",
        help="Source label for this batch (default: CDK-transactions)",
    )

    args = ap.parse_args()
    import_receipts(args.csv, args.db, args.source)


if __name__ == "__main__":
    main()

