#!/usr/bin/env python3
"""
show_parts.py

Inspect what part numbers the invoice pipeline actually captured.

Usage:
  python show_parts.py invoices.db
  python show_parts.py invoices.db --limit 3          # first 3 invoices
  python show_parts.py invoices.db --limit 3 --last   # last 3 invoices
  python show_parts.py invoices.db --invoice 397-190129
"""

import argparse
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description="Show captured part numbers from invoices.db")
    parser.add_argument("db", type=Path, help="Path to invoices.db")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of invoices shown (applied to first or last depending on --last)",
    )
    parser.add_argument(
        "--last",
        action="store_true",
        help="When used with --limit, show the *last* N invoices instead of the first N.",
    )
    parser.add_argument(
        "--invoice",
        type=str,
        default=None,
        help="Filter by a specific invoice number (e.g. 397-190129 or 8910181680)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        console.print(f"[red]Database not found:[/red] {args.db}")
        return 1

    conn = sqlite3.connect(str(args.db))
    cur = conn.cursor()

    # Join invoices + suppliers + line_items
    query = """
        SELECT
            invoices.id,
            COALESCE(invoices.invoice_number, '?') AS inv_no,
            COALESCE(suppliers.name, '?') AS supplier,
            COALESCE(invoices.invoice_date, '?') AS inv_date,
            COALESCE(line_items.part_number, '') AS part_no,
            COALESCE(line_items.description, '') AS desc,
            COALESCE(line_items.line_total, '') AS line_total
        FROM invoices
        LEFT JOIN suppliers ON invoices.supplier_id = suppliers.id
        LEFT JOIN line_items ON line_items.invoice_id = invoices.id
        WHERE 1 = 1
    """

    params = []
    if args.invoice:
        query += " AND invoices.invoice_number = ?"
        params.append(args.invoice)

    query += " ORDER BY invoices.id, line_items.id"
    cur.execute(query, params)
    rows = cur.fetchall()

    if not rows:
        console.print("[yellow]No line items found in database.[/yellow]")
        return 0

    # Group by invoice_id in Python so we can show nice blocks
    by_invoice = {}
    for inv_id, inv_no, supplier, inv_date, part_no, desc, line_total in rows:
        if inv_id not in by_invoice:
            by_invoice[inv_id] = {
                "invoice_number": inv_no,
                "supplier": supplier,
                "date": inv_date,
                "lines": [],
            }
        by_invoice[inv_id]["lines"].append((part_no, desc, line_total))

    # invoices in ascending id (same as document order)
    inv_items = list(by_invoice.items())

    # Apply limit / last
    if args.limit is not None:
        if args.last:
            inv_items = inv_items[-args.limit :]
        else:
            inv_items = inv_items[: args.limit]

    for idx, (inv_id, data) in enumerate(inv_items, start=1):
        title = f"Invoice ID {inv_id} — {data['supplier']} — #{data['invoice_number']} — {data['date']}"
        table = Table(title=title)
        table.add_column("Part #", style="cyan", no_wrap=True)
        table.add_column("Description", style="white")
        table.add_column("Line total", justify="right")

        if not data["lines"]:
            table.add_row("[dim]<no line items>[/dim]", "", "")
        else:
            for part_no, desc, line_total in data["lines"]:
                # line_total can be REAL or empty string
                if isinstance(line_total, float):
                    total_str = f"{line_total:.2f}"
                elif isinstance(line_total, (int,)):
                    total_str = f"{float(line_total):.2f}"
                else:
                    total_str = str(line_total) if line_total not in ("", None) else ""

                table.add_row(
                    part_no or "",
                    (desc or "").strip(),
                    total_str,
                )

        console.print(table)
        if idx != len(inv_items):
            console.print()  # blank line between invoices

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
