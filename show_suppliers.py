#!/usr/bin/env python3
"""
show_suppliers.py

Print all suppliers currently in invoices.db with:
- id
- name
- type
- invoice count
- total billed (sum of invoice totals, where present)
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Show suppliers in invoices.db with basic stats."
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=Path("invoices.db"),
        help="SQLite DB path (default: invoices.db)",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT
            s.id,
            s.name,
            s.type,
            COUNT(inv.id) AS invoice_count,
            COALESCE(SUM(inv.total), 0.0) AS total_billed
        FROM suppliers s
        LEFT JOIN invoices inv ON inv.supplier_id = s.id
        GROUP BY s.id, s.name, s.type
        ORDER BY s.name COLLATE NOCASE;
        """
    ).fetchall()

    conn.close()

    print(
        f"{'ID':>3}  {'Supplier':<30}  {'Type':<15}  "
        f"{'Invoices':>8}  {'Total billed':>12}"
    )
    print("-" * 80)
    for sid, name, stype, inv_count, total in rows:
        print(
            f"{sid:>3}  {name[:30]:<30}  {str(stype or '')[:15]:<15}  "
            f"{inv_count:>8}  {total:>12.2f}"
        )


if __name__ == "__main__":
    main()

