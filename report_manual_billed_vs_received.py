
#!/usr/bin/env python3
"""
report_manual_billed_vs_received.py

Reconciliation for manual supplier receipts ("O" transcode in CDK).

- Billed side:
    All non-FCA, non-"self" suppliers from invoices/line_items:
      * suppliers where NOT (name LIKE 'Mopar Canada%' OR type = 'chrysler_corp')
      * AND (type IS NULL OR type != 'self')

- Received side:
    receipts_lines with transcode = 'O' (your manual entries).

- Part numbers are normalized via invoice_pipeline.normalize_part so that
  supplier invoices and CDK receipts compare apples-to-apples.

Output:
    1) Billed more than received  (billed_qty > received_qty)
    2) Received more than billed  (received_qty > billed_qty)
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Dict, Tuple, List, Optional

from invoice_pipeline import normalize_part


def load_external_billed_quantities(
    conn: sqlite3.Connection,
    supplier_filter: Optional[str],
) -> Dict[str, float]:
    """
    Return {canonical_part_number: total_billed_qty} for non-FCA, non-self suppliers.

    If supplier_filter is provided, only include suppliers whose name matches
    that pattern (case-insensitive LIKE).
    """
    cur = conn.cursor()

    base_sql = """
        SELECT li.part_number, COALESCE(SUM(li.quantity), 0.0) AS qty
        FROM line_items li
        JOIN invoices inv ON li.invoice_id = inv.id
        JOIN suppliers s ON inv.supplier_id = s.id
        WHERE NOT (s.name LIKE 'Mopar Canada%' OR s.type = 'chrysler_corp')
          AND (s.type IS NULL OR s.type <> 'self')
    """

    params: Tuple = ()
    if supplier_filter:
        base_sql += " AND s.name LIKE ?"
        params = (f"%{supplier_filter}%",)

    base_sql += " GROUP BY li.part_number"

    rows = cur.execute(base_sql, params).fetchall()

    billed: Dict[str, float] = {}
    for part, qty in rows:
        if not part:
            continue
        key = normalize_part(part)
        billed[key] = billed.get(key, 0.0) + (qty or 0.0)
    return billed


def load_manual_received_quantities(
    conn: sqlite3.Connection,
    supplier_filter: Optional[str],
) -> Dict[str, float]:
    """
    Return {canonical_part_number: total_received_qty} from receipts_lines where
    transcode = 'O' (manual/external supplier receipts).

    If supplier_filter is provided, only include rows whose supplier_name
    matches that pattern (case-insensitive LIKE).
    """
    cur = conn.cursor()

    base_sql = """
        SELECT part_number, COALESCE(SUM(qty_received), 0.0) AS qty
        FROM receipts_lines
        WHERE transcode = 'O'
    """

    params: Tuple = ()
    if supplier_filter:
        base_sql += " AND supplier_name LIKE ?"
        params = (f"%{supplier_filter}%",)

    base_sql += " GROUP BY part_number"

    rows = cur.execute(base_sql, params).fetchall()

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
    print(f"{'Part #':<20} {'Billed':>10} {'Received':>10} {'Diff(b-r)':>10}")
    print("-" * 60)
    for part, billed, rec, diff in rows[:limit]:
        print(f"{part:<20} {billed:>10.2f} {rec:>10.2f} {diff:>10.2f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare external supplier billed quantities vs CDK 'O' manual receipts."
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
    ap.add_argument(
        "--supplier",
        type=str,
        default=None,
        help="Optional substring to filter suppliers (e.g. 'NAPA', 'Lordco').",
    )

    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    billed = load_external_billed_quantities(conn, args.supplier)
    received = load_manual_received_quantities(conn, args.supplier)

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

    # Sort by largest absolute difference
    billed_more.sort(key=lambda r: r[3], reverse=True)
    received_more.sort(key=lambda r: abs(r[3]), reverse=True)

    if args.supplier:
        print(f"Supplier filter: {args.supplier}")
    format_section("Billed more than received (possible outstanding)", billed_more, args.limit)
    format_section("Received more than billed (possible over-receipt / mismatch)", received_more, args.limit)

    conn.close()


if __name__ == "__main__":
    main()
