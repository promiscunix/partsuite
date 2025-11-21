# core/fca_parser.py

from __future__ import annotations

from typing import List, Dict, Any, Optional
from datetime import datetime, date
import re
import os

from PyPDF2 import PdfReader


def _group_pages_by_invoice(path: str) -> Dict[str, Dict[str, Any]]:
    """
    Read the FCA PDF and group pages by INVOICE NUMBER.
    """
    reader = PdfReader(path)
    groups: Dict[str, Dict[str, Any]] = {}

    for page in reader.pages:
        text = page.extract_text() or ""
        if not text:
            continue

        m_num = re.search(r"INVOICE NUMBER:\s*(.+)", text)
        if not m_num:
            continue

        inv_num = m_num.group(1).strip()

        m_date = re.search(r"INVOICE DATE\s*:?\s*(.+)", text)
        inv_date_str = m_date.group(1).strip() if m_date else None

        group = groups.setdefault(inv_num, {"date_str": None, "pages": []})
        if inv_date_str and not group["date_str"]:
            group["date_str"] = inv_date_str

        group["pages"].append(text)

    return groups


def _parse_invoice_lines(page_texts: List[str]) -> Dict[str, Any]:
    """
    Parse part lines. We mainly use these for part/qty detail;
    totals come from the SUMMARY block.
    """
    combined_lines: List[str] = []
    for t in page_texts:
        combined_lines.extend(t.splitlines())

    parsed_lines: List[Dict[str, Any]] = []
    line_number_counter = 0
    subtotal_lines = 0.0

    for line in combined_lines:
        if not re.match(r"^\s*\d+\s+[A-Z0-9]{5,}\s", line):
            continue

        m = re.match(r"^\s*(\d+)\s+([A-Z0-9]{5,})\s+(.*)$", line)
        if not m:
            continue

        part_number = m.group(2)
        rest = m.group(3)

        qty_match = re.search(r"\s(\d+)\s+([0-9,]+\.[0-9]{2})", rest)
        if not qty_match:
            continue

        qty_billed = int(qty_match.group(1))

        float_strs = re.findall(r"[0-9,]+\.[0-9]{2}", rest)
        floats = [float(s.replace(",", "")) for s in float_strs]

        unit_cost = floats[0] if floats else 0.0
        extended_cost = floats[-1] if floats else 0.0

        description = rest[: qty_match.start()].rstrip()

        line_number_counter += 1
        parsed_lines.append(
            {
                "line_number": line_number_counter,
                "part_number": part_number,
                "description": description,
                "qty_billed": qty_billed,
                "unit_cost": unit_cost,
                "extended_cost": extended_cost,
                "is_core": False,
                "is_env_fee": False,
                "is_freight": False,
                "is_discount": False,
            }
        )

        subtotal_lines += extended_cost

    return {
        "lines": parsed_lines,
        "subtotal_lines": round(subtotal_lines, 2),
    }


def _parse_summary_block(page_texts: List[str]) -> Dict[str, float]:
    """
    Parse the SUMMARY section for a single FCA invoice.
    """
    summary = {
        "gross": 0.0,
        "discounts_earned": 0.0,
        "dealer_generated_return": 0.0,
        "locator_charge": 0.0,
        "deposit_values": 0.0,
        "transportation": 0.0,
        "env_container": 0.0,
        "env_lubricant": 0.0,
        "gst": 0.0,
        "net_invoice": 0.0,
    }

    for text in page_texts:
        if "SUMMARY:" not in text:
            continue

        lines = text.splitlines()
        start_idx: Optional[int] = None
        end_idx: Optional[int] = None

        for i, l in enumerate(lines):
            if "SUMMARY:" in l:
                start_idx = i
            if "NET INVOICE AMOUNT" in l:
                end_idx = i

        if start_idx is None or end_idx is None:
            continue

        block = lines[start_idx : end_idx + 1]

        for l in block:
            nums = re.findall(r"([0-9,]+\.[0-9]{2})", l)
            val = float(nums[-1].replace(",", "")) if nums else None
            if val is None:
                continue

            if "TOTAL GROSS AMOUNT" in l:
                summary["gross"] = val
            elif "DISCOUNTS EARNED" in l:
                summary["discounts_earned"] = val
            elif "ARC01217" in l:
                summary["dealer_generated_return"] = val
            elif "ARC01222" in l:
                summary["locator_charge"] = val
            elif "ARC31101" in l:
                summary["deposit_values"] = val
            elif "ARC45012" in l:
                summary["transportation"] = val
            elif "ENV.CONTAINER" in l:
                summary["env_container"] = val
            elif "ENV.LUBRICANT" in l:
                summary["env_lubricant"] = val
            elif "GST/HST" in l:
                summary["gst"] = val
            elif "NET INVOICE AMOUNT" in l:
                summary["net_invoice"] = val

        break

    return summary


def _normalize_invoice_date(raw: Optional[str]) -> str:
    if not raw:
        return date.today().isoformat()

    raw = raw.strip()
    try:
        dt = datetime.strptime(raw, "%B %d, %Y")
        return dt.date().isoformat()
    except ValueError:
        pass

    try:
        dt = datetime.fromisoformat(raw)
        return dt.date().isoformat()
    except ValueError:
        pass

    return date.today().isoformat()


def parse_fca_pdf(path: str) -> List[Dict[str, Any]]:
    """
    Return FCA invoices in a PDF as:

        {
          "header": {
            "invoice_number": "...",
            "invoice_date": "YYYY-MM-DD",
            "subtotal": 75916.20,      # TOTAL GROSS AMOUNT
            "freight": 108.67,        # locator + transportation
            "env_fees": 71.51,        # env.container + env.lubricant
            "tax_amount": 3837.22,    # GST/HST
            "total_amount": 80581.66,

            # extra summary fields for GL coding:
            "discounts_earned": 161.95,
            "dealer_generated_return": 10.00,
            "deposit_values": 800.01,
          },
          "lines": [...]
        }
    """
    path = os.path.abspath(path)
    groups = _group_pages_by_invoice(path)
    invoices: List[Dict[str, Any]] = []

    for inv_num, data in groups.items():
        parsed_lines = _parse_invoice_lines(data["pages"])
        summary = _parse_summary_block(data["pages"])
        date_iso = _normalize_invoice_date(data.get("date_str"))

        gross = summary["gross"]
        discounts = summary["discounts_earned"]
        dealer_return = summary["dealer_generated_return"]
        deposit = summary["deposit_values"]
        locator = summary["locator_charge"]
        transportation = summary["transportation"]
        env_container = summary["env_container"]
        env_lubricant = summary["env_lubricant"]
        gst = summary["gst"]

        env_total = env_container + env_lubricant
        freight_total = locator + transportation

        # SUBTOTAL in your system = FCA TOTAL GROSS AMOUNT
        subtotal = gross

        # If FCA didn't print a net (they always do, but just in case), compute it.
        computed_total = (
            subtotal
            - discounts
            + dealer_return
            + deposit
            + freight_total
            + env_total
            + gst
        )

        total_amount = summary["net_invoice"] or computed_total

        header = {
            "invoice_number": inv_num,
            "invoice_date": date_iso,
            "po_number": None,

            "subtotal": round(subtotal, 2),
            "freight": round(freight_total, 2),
            "env_fees": round(env_total, 2),
            "tax_amount": round(gst, 2),
            "total_amount": round(total_amount, 2),

            "discounts_earned": round(discounts, 2),
            "dealer_generated_return": round(dealer_return, 2),
            "deposit_values": round(deposit, 2),
        }

        invoices.append(
            {
                "header": header,
                "lines": parsed_lines["lines"],
            }
        )

    return invoices
