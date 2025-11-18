#!/usr/bin/env python3
"""
report_fca_billed_vs_received.py

Compare FCA/Mopar billed quantities vs CDK 'R' receipts.

- Reads from existing tables: suppliers, invoices, line_items, receipts_lines.
- DOES NOT modify the database.
- Normalizes part numbers (strips leading zeros) so that
  '0VU01321AC' and 'VU01321AC' are treated as the same part.
- Outputs two sections:
    1) Billed more than received  (billed_qty > received_qty)
    2) Received more than billed  (received_qty > billed_qty)
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Dict, Tuple, List


def normalize_part(part: str) -> str:
    """
    Canonicalize a Mopar part number for comparison.

    - Uppercase
    - Strip leading zeros

    Examples:
      '0VU01321AC' -> 'VU01321AC'
      '04892339BE' -> '4892339BE'
      '06512211AA' -> '6512211AA'
    """
    if not part:
        return ""
    s = part.strip().upper()
    i = 0
    while i < len(s) and s[i] == "0":
        i += 1
    return s[i:] or "0"


def load_mopar_billed_quantities(conn: sqlite3.Connection) -> Dict[str, float]:
    """
    Return {canonical_part_number: total_billed_qty} for invoices from Mopar/FCA.
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT li.part_number, COALESCE(SUM(li.quantity), 0.0) AS qty
        FROM line_items li
        JOIN invoices inv ON li.invoice_id = inv.id
        JOIN suppliers s ON inv.supplier_id = s.id
        WHERE s.name LIKE 'Mopar Canada%' OR s.type = 'chrysler_corp'
        GROUP BY li.part_number
        """
    ).fetchall()

    billed: Dict[str, float] = {}
    for part, qty in rows:
        if not part:
            continue
        key = normalize_part(part)
        billed[key] = billed.get(key, 0.0) + (qty or 0.0)
    return billed


def load_fca_received_quantities(conn: sqlite3.Connection) -> Dict[str, float]:
    """
    Return {canonical_part_number: total_received_qty} from receipts_lines where
    transcode = 'R' (FCA shipments).
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT part_number, COALESCE(SUM(qty_received), 0.0) AS qty
        FROM receipts_lines
        WHERE transcode = 'R'
        GROUP BY part_number
        """
    ).fetchall()

    rec: Dict[str, float] = {}
    for part, qty in rows:
        if not part:
            continue
        key = normalize_part(part)
        rec[key] = rec.get(key, 0.0) + (qty or 0.0)
    return rec


def format_section(title: str, rows: List[Tuple[str, float, float, float]], limit: int) -> None:
    print()
    print(title)
    print("-" * len(title))
    print(f"{'Part #':<15} {'Billed':>10} {'Received':>10} {'Diff(b-r)':>10}")
    print("-" * 50)
    for part, billed, rec, diff in rows[:limit]:
        print(f"{part:<15} {billed:>10.2f} {rec:>10.2f} {diff:>10.2f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare FCA/Mopar billed quantities vs CDK 'R' receipts."
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=Path("invoices.db"),
        help="SQLite DB path (default: invoices.db)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max rows to show in each section (default: 100)",
    )

    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    billed = load_mopar_billed_quantities(conn)
    received = load_fca_received_quantities(conn)

    all_parts = sorted(set(billed.keys()) | set(received.keys()))

    billed_more: List[Tuple[str, float, float, float]] = []
    received_more: List[Tuple[str, float, float, float]] = []

    for part in all_parts:
        bq = billed.get(part, 0.0)
        rq = received.get(part, 0.0)
        diff = bq - rq
        if abs(diff) < 1e-6:
            continue
        if diff > 0:
            billed_more.append((part, bq, rq, diff))
        else:
            received_more.append((part, bq, rq, diff))

    # Sort: largest absolute difference first
    billed_more.sort(key=lambda r: r[3], reverse=True)
    received_more.sort(key=lambda r: abs(r[3]), reverse=True)

    format_section("Billed more than received (possible outstanding)", billed_more, args.limit)
    format_section("Received more than billed (possible over-receipt / mismatch)", received_more, args.limit)

    conn.close()


if __name__ == "__main__":
    main()
