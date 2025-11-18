#!/usr/bin/env python3
"""
invoice_pipeline.py

Pipeline:

1. (Optional) Run OCR (ocrmypdf) on a bulk-scanned PDF:
     - Image-only invoices from your copier.
2. For each page:
     - Extract invoice number using several supplier-specific patterns.
     - Extract date, supplier, PO, totals, and line items.
3. Group pages into invoices:
     - Pages with the same invoice number are grouped together.
     - Pages with no invoice number but containing money (subtotal/total)
       become their own invoice.
4. Write one PDF per invoice (named with supplier + invoice number where possible).
5. Optionally record everything into a SQLite database (invoices + line items).

Usage examples:

  # If your input is image-only from the scanner:
  # (script will run ocrmypdf)
  python invoice_pipeline.py bulk_scan.pdf --output-dir out_invoices --db invoices.db

  # If you've already run ocrmypdf yourself (e.g. bulk_scan_ocr.pdf):
  python invoice_pipeline.py bulk_scan_ocr.pdf --output-dir out_invoices --db invoices.db --no-ocr
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import PyPDF2
from dateutil import parser as date_parser
from rich.console import Console
from rich.table import Table

console = Console()


# -------------------- Data structures -------------------- #

@dataclass
class LineItem:
    raw_line: str
    part_number: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    line_total: Optional[float] = None


@dataclass
class InvoiceData:
    supplier_name: Optional[str]
    supplier_type: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str]  # ISO yyyy-mm-dd
    po_number: Optional[str]
    subtotal: Optional[float]
    taxes: Dict[str, float]
    total: Optional[float]
    pages: List[int]   # 0-based page indices
    line_items: List[LineItem]
    raw_text: str


# -------------------- Helper functions -------------------- #

def _normalize_year(dt):
    """
    Fix obvious OCR glitches like 2035 -> 2025.

    If the year is more than 1 year in the future but minus 10 years
    is close to the current year, assume it's a 10-year slip.
    """
    today_year = date.today().year
    if dt.year > today_year + 1 and dt.year - 10 >= 2000:
        if abs((dt.year - 10) - today_year) <= 5:
            return dt.replace(year=dt.year - 10)
    return dt


def _clean_invoice_token(val: str) -> str:
    """
    Clean up the raw invoice token.

    - For things like `397-190129` we keep the full branch+number.
    - For junk like `258284PARTSOURCE` we return just `258284`.
    """
    val = val.strip()

    # Trim obvious trailing non-alnum
    val = re.sub(r"[^\w\-]+$", "", val)

    # If it looks like `branch-number`, keep digits and hyphen
    if "-" in val:
        val = re.sub(r"[^0-9\-]", "", val)
        return val

    # Otherwise, grab the first run of 3+ digits (avoid tiny things)
    m = re.search(r"\d{3,}", val)
    if m:
        return m.group(0)

    return val


# -------------------- Field extraction -------------------- #

def extract_invoice_number(text: str) -> Optional[str]:
    """
    Handle the variants seen so far:

    - INVOICE  NUMBER: 258329
    - INVOICE REF: 308973 / INVOICE NUMBER: 258329 / INVOICE DATE: 2025-11-07
    - TERMS  :309068\\n258537\\n2025-11-08 (Ref / Number / Date)
    - PAYMENTREF: 309043\\nNUMBER: 258489\\nDATE: 2025-11-07
    - INVOICE309730\\n258793\\n2025-11-10
    - Invoice Number 397-190129 (NAPA)
    - Invoice : 52813328 (Action Car & Truck)
    - cHiwoice # 8910181680 / Invoice # 8910181680 (Lordco)
    - Fallback NAPA style: 397-190218 (possibly with weird dashes / spaces)
    """

    # PaymentRef style (older PartSource pattern)
    m = re.search(
        r'PAYMENTREF:\s*([0-9]{4,})\s+NUMBER:\s*([0-9]{4,})\s+DATE:\s*(\d{4}-\d{2}-\d{2})',
        text,
        re.IGNORECASE,
    )
    if m:
        return _clean_invoice_token(m.group(2))

    # TERMS :309068\n258537\n2025-11-08
    m = re.search(
        r'TERMS\s*:([0-9]{4,})\s+([0-9]{4,})\s+(\d{4}-\d{2}-\d{2})',
        text,
        re.IGNORECASE,
    )
    if m:
        return _clean_invoice_token(m.group(2))

    # Inline "INVOICE309730\n258793\n2025-11-10" (older PartSource)
    m = re.search(
        r'INVOICE\s*([0-9]{4,})\s+([0-9]{4,})\s+(\d{4}-\d{2}-\d{2})',
        text,
        re.IGNORECASE,
    )
    if m:
        return _clean_invoice_token(m.group(2))

    # "INVOICE NUMBER 397-190129" (NAPA & others)
    m = re.search(
        r'\bINVOICE\s+NUMBER[:\s]+([A-Z0-9\-]+)',
        text,
        re.IGNORECASE,
    )
    if m:
        return _clean_invoice_token(m.group(1))

    # "Invoice : 52813328" (Action Car & Truck)
    m = re.search(
        r'\bInvoice\s*:\s*([0-9]{6,})',
        text,
        re.IGNORECASE,
    )
    if m:
        return _clean_invoice_token(m.group(1))

    # Lordco style: "Invoice # 8910181680" or OCR as "cHiwoice # 8910181680"
    m = re.search(
        r'\b\w*oice\s*#\s*([0-9]{6,})',
        text,
        re.IGNORECASE,
    )
    if m:
        return _clean_invoice_token(m.group(1))

    # NAPA-style / branch-style number on an "Invoice" line
    for line in text.splitlines():
        if "invoice" in line.lower():
            # Accept: 397-190218, 397 - 190218, 397–190218, 397 190218
            m_line = re.search(r'(\d{3})\s*[-–]?\s*(\d{6,})', line)
            if m_line:
                return f"{m_line.group(1)}-{m_line.group(2)}"

    # Last resort: any 3-digit-hyphen-6+digit pattern anywhere
    # (e.g. 397-190129). This still won't match phone numbers like 604-530-6464.
    m = re.search(r'\b(\d{3}-\d{6,})\b', text)
    if m:
        return m.group(1)

    return None


def extract_invoice_date(text: str) -> Optional[str]:
    # Prefer explicit "INVOICE DATE: 2025-11-07"
    m = re.search(
        r'INVOICE\s+DATE[:\s]+([A-Za-z0-9,\/\-\s]+)',
        text,
        re.IGNORECASE,
    )
    if m:
        cand = m.group(1).strip()
        try:
            dt = date_parser.parse(cand, fuzzy=True)
            dt = _normalize_year(dt)
            return dt.date().isoformat()
        except Exception:
            pass

    # TERMS  :309068\n258537\n2025-11-08
    m = re.search(
        r'TERMS\s*:[0-9]{4,}\s+[0-9]{4,}\s+(\d{4}-\d{2}-\d{2})',
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)

    # PAYMENTREF: ... DATE: 2025-11-07
    m = re.search(
        r'PAYMENTREF:\s*[0-9]{4,}\s+NUMBER:\s*[0-9]{4,}\s+DATE:\s*(\d{4}-\d{2}-\d{2})',
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)

    # Inline "INVOICE309730\n258793\n2025-11-10"
    m = re.search(
        r'INVOICE\s*[0-9]{4,}\s+[0-9]{4,}\s+(\d{4}-\d{2}-\d{2})',
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)

    # Slashed dates like "11/06/2025" or "11/05/25"
    m = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', text)
    if m:
        cand = m.group(1)
        try:
            dt = date_parser.parse(cand, dayfirst=False)
            dt = _normalize_year(dt)
            return dt.date().isoformat()
        except Exception:
            pass

    # Fallback: any ISO-looking date, still run through _normalize_year
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        cand = m.group(1)
        try:
            dt = date_parser.parse(cand, fuzzy=True)
            dt = _normalize_year(dt)
            return dt.date().isoformat()
        except Exception:
            return cand

    return None


def extract_po_number(text: str) -> Optional[str]:
    patterns = [
        r'\bPO\s*(?:NO|NUMBER|#)?[:\s]+([A-Z0-9\-]+)',
        r'\bP\.?O\.?\s*#\s*([A-Z0-9\-]+)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def extract_supplier_name(text: str) -> Optional[str]:
    # First, look for explicit patterns (known suppliers)
    supplier_patterns = [
        # NAPA
        (re.compile(r'NAPA\s+PORT\s+KELLS', re.IGNORECASE), "NAPA Port Kells"),
        (re.compile(r'\bNAPA\b', re.IGNORECASE), "NAPA"),

        # Lordco
        (re.compile(r'\bLORDCO\b', re.IGNORECASE), "Lordco Auto Parts"),
        (re.compile(r'\bLORDGO\b', re.IGNORECASE), "Lordco Auto Parts"),
        (re.compile(r'= AUTO PA', re.IGNORECASE), "Lordco Auto Parts"),

        # Action Car & Truck
        (re.compile(r'ACTION\s+CAR\s+AND\s+TRUCK', re.IGNORECASE), "Action Car & Truck"),
        (re.compile(r'CAR AND TRUCK ACCESSORIES', re.IGNORECASE), "Action Car & Truck"),

        # PartSource
        (re.compile(r'\bPARTSOURCE\b', re.IGNORECASE), "PartSource"),
        (re.compile(r'The Parts[., ]+The Pros[., ]+The Price', re.IGNORECASE), "PartSource"),
        (re.compile(r'PARTSOURCE\.CA', re.IGNORECASE), "PartSource"),
        (re.compile(r'PARTS?\s*RCE', re.IGNORECASE), "PartSource"),

        # Mopar / FCA / Stellantis
        (re.compile(r'FCA CANADA', re.IGNORECASE), "FCA Canada / Mopar"),
        (re.compile(r'MOPAR CANADA', re.IGNORECASE), "Mopar Canada"),
        (re.compile(r'STELLANTIS', re.IGNORECASE), "Stellantis / Mopar"),

        # Tire suppliers
        (re.compile(r'KAL[- ]?TIRE', re.IGNORECASE), "Kal Tire"),
        (re.compile(r'OK TIRE', re.IGNORECASE), "OK Tire"),

        # Langley Chrysler – sometimes OCR gives 'BESTCHRYS'
        (re.compile(r'LANGLEY\s+CHRYSLER', re.IGNORECASE), "Langley Chrysler"),
        (re.compile(r'BESTCHRYS', re.IGNORECASE), "Langley Chrysler"),
    ]
    for rgx, name in supplier_patterns:
        if rgx.search(text):
            return name

    # Fallback: guess from header area above BILL / SHIP / CUSTOMER
    lines = text.splitlines()
    header_lines: List[str] = []
    for line in lines[:40]:
        up = line.upper()
        if "SHIP" in up or "BILL TO" in up or "BILL  TO" in up or "CUSTOMER" in up:
            break
        header_lines.append(line.strip())

    header_lines = [ln for ln in header_lines if ln]

    # Filter out obvious junk (returns policy, payment tender, etc.)
    junk_tokens = [
        "MERCHANDISE", "RETURN POLICY", "RETURNS",
        "PAYMENT USING", "TENDER", "INCREMENT",
        "CHECKED AND RECEIVED",
        "TOTAL", "SUB-TOTAL", "GST", "PST", "HST",
    ]

    best = None
    best_score = 0.0
    for ln in header_lines:
        up_ln = ln.upper()
        if any(tok in up_ln for tok in junk_tokens):
            continue
        if len(ln) < 4:
            continue

        # Score by how uppercase it is, plus a small boost for "company-ish" tokens
        up_count = sum(1 for c in ln if c.isupper())
        score = up_count / max(1, len(ln))
        if any(tok in up_ln for tok in ["INC", "LTD", "LIMITED", "CORP", "COMPANY", "TIRE", "CHRYSLER"]):
            score += 0.2

        if score > 0.5 and score > best_score:
            best_score = score
            best = ln.strip()

    return best


def classify_supplier(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    u = name.upper()
    if any(k in u for k in ["FCA CANADA", "MOPAR CANADA", "STELLANTIS"]):
        return "chrysler_corp"
    if "MAPLE RIDGE CHRYSLER" in u or "MR MOTORS" in u or "MRMOTORS" in u:
        return "self"
    if "CHRYSLER" in u:
        return "chrysler_dealer"
    if "TIRE" in u:
        return "tire"
    return "general"


def extract_totals(text: str):
    subtotal: Optional[float] = None
    taxes: Dict[str, float] = {}
    total: Optional[float] = None

    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()

        # SUBTOTAL
        m = re.search(r"\bSUB[- ]?TOTAL\b.*?(\d+\.\d{2})", clean, re.IGNORECASE)
        if m and subtotal is None:
            try:
                subtotal = float(m.group(1))
            except ValueError:
                pass

        # NAPA / Langley-style: "Parts Sale 24.21" / "Total Parts Sales 24.21"
        if subtotal is None and "PARTS" in clean.upper():
            if "PARTS SALE" in clean.upper() or "TOTAL PARTS SALES" in clean.upper():
                m_ps = re.search(r"(\d+\.\d{2})", clean)
                if m_ps:
                    try:
                        subtotal = float(m_ps.group(1))
                    except ValueError:
                        pass

        # GST / PST / HST
        for tax_code in ("GST", "PST", "HST"):
            if tax_code in clean.upper():
                m2 = re.search(rf"{tax_code}[^0-9]*(\d+\.\d{{2}})", clean, re.IGNORECASE)
                if m2:
                    try:
                        taxes[tax_code] = float(m2.group(1))
                    except ValueError:
                        pass

        # Skip lines like "TOTAL WGT" – those are weights, not money
        if "TOTAL WGT" in clean.upper() or "TOTAL WT" in clean.upper():
            continue

        # TOTAL: match "TOTAL $ 14.70", "TOTAL § 300.71", "Total Invoice 25.42", etc.
        # but NOT "SUB-TOTAL 14.39" (that's handled above)
        m3 = re.search(r"(?:^|\s)TOTAL\b[^0-9]*(\d+\.\d{2})", clean, re.IGNORECASE)
        if m3:
            try:
                total = float(m3.group(1))
            except ValueError:
                pass

    return subtotal, taxes, total


def is_line_item_candidate(line: str) -> bool:
    # Must have some letters and at least one money value.
    if not re.search(r"[A-Za-z]", line):
        return False
    money = re.findall(r"\d+\.\d{2}", line)
    if len(money) < 1:
        return False

    blacklist = [
        "SUB-TOTAL", "SUBTOTAL",
        "GST", "PST", "HST", "TOTAL",
        "INVOICE NUMBER", "INVOICE DATE",
        "PAYMENT TERMS", "STORE #",
        "BILL TO", "SHIP TO",
        "WWW.", "PARTSOURCE.CA",
        "MERCHANDISE RETURNS",
        "RECEIVED BY (FULL NAME)",
    ]
    up = line.upper()
    if any(b in up for b in blacklist):
        return False
    return True


def extract_line_items(text: str) -> List[LineItem]:
    """
    Improved heuristics:

    - Try single-line items as before.
    - Additionally, if we see a "description-only" line followed by a
      "money-only" line, merge them into a single candidate line item.
    """
    items: List[LineItem] = []
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            i += 1
            continue

        line_has_letters = bool(re.search(r"[A-Za-z]", line))
        line_has_money = bool(re.search(r"\d+\.\d{2}", line))

        candidate_line = None

        # Case 1: plain old good candidate on a single line
        if is_line_item_candidate(line):
            candidate_line = line

        else:
            # Case 2: description-only line followed by money-only line
            if line_has_letters and not line_has_money and i + 1 < len(lines):
                next_line = lines[i + 1].rstrip()
                next_has_letters = bool(re.search(r"[A-Za-z]", next_line))
                next_has_money = bool(re.search(r"\d+\.\d{2}", next_line))

                # typical pattern: desc on one line, prices on next
                if next_has_money and not next_has_letters:
                    candidate_line = f"{line} {next_line}"
                    i += 1  # consume the next line as part of this item

        if candidate_line is None:
            i += 1
            continue

        raw = candidate_line

        # Money values: last one is line_total, previous maybe unit_price
        money = re.findall(r"(\d+\.\d{2})", raw)
        qty: Optional[float] = None
        unit_price: Optional[float] = None
        line_total: Optional[float] = None

        if money:
            try:
                line_total = float(money[-1])
            except Exception:
                pass
            if len(money) >= 2:
                try:
                    unit_price = float(money[-2])
                except Exception:
                    pass

        tokens = raw.split()
        freq: Dict[str, int] = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1

        # Heuristic to pick a part number-like token
        cand_tokens: List[tuple[str, int]] = []
        for idx, tok in enumerate(tokens):
            t = tok.strip()

            # Drop early pure-qty columns (e.g. "1", "2") regardless of frequency
            if idx <= 2 and re.fullmatch(r"\d+", t) and int(t) <= 10:
                continue

            # Pure decimals are always money, not part numbers
            if re.fullmatch(r"\d+\.\d{2}", t):
                continue

            # Short pure-digit tokens (e.g. "1", "2", "1234") are almost always
            # quantities or line indices, not part numbers. Keep only LONG digit codes.
            if re.fullmatch(r"\d+", t):
                if len(t) < 6:
                    continue  # avoid "1" / "2"

            # Skip tokens with quotes
            if '"' in t:
                continue

            # Require at least one digit
            if not re.search(r"\d", t):
                continue

            # Only allow reasonably clean tokens
            if not re.fullmatch(r"[A-Za-z0-9\-]+", t):
                continue

            score = 1
            if freq.get(t, 0) > 1:
                score += 2
            if re.search(r"[A-Za-z]", t):
                score += 1
            cand_tokens.append((t, score))

        part_number: Optional[str] = None
        if cand_tokens:
            part_number = max(cand_tokens, key=lambda t: t[1])[0]

        description: Optional[str] = None
        if part_number and money:
            try:
                start_idx = raw.index(part_number) + len(part_number)
                first_money = money[0]
                end_idx = raw.rindex(first_money)
                if end_idx > start_idx:
                    description = raw[start_idx:end_idx].strip()
            except ValueError:
                pass

        items.append(
            LineItem(
                raw_line=raw,
                part_number=part_number,
                description=description,
                quantity=qty,
                unit_price=unit_price,
                line_total=line_total,
            )
        )

        i += 1

    return items


def _clean_summary_line_items(
    items: List[LineItem],
    subtotal: Optional[float],
    total: Optional[float],
) -> List[LineItem]:
    """
    Remove obvious summary rows from line_items, e.g.:

      - lines with no part/description and line_total == subtotal or total
      - lines with no part/description and line_total == 0.00

    This cleans up invoices where only subtotal/total/tender lines
    would otherwise appear as fake items.
    """
    cleaned: List[LineItem] = []
    for li in items:
        if (li.part_number is None or not str(li.part_number).strip()) and \
           (li.description is None or not str(li.description).strip()):
            if li.line_total is not None:
                if subtotal is not None and abs(li.line_total - subtotal) < 0.01:
                    continue
                if total is not None and abs(li.line_total - total) < 0.01:
                    continue
                if abs(li.line_total) < 0.001:
                    continue
        cleaned.append(li)
    return cleaned


def _augment_action_descriptions(text: str, items: List[LineItem]) -> List[LineItem]:
    """
    For Action Car & Truck invoices:

      ACTLED-PHISO12-L ... 104.95
      9012 HEAT INJECTED PREMIUM SERIES

    The REAL description is on the line *below* the part+price line.
    For Action, we override whatever junk description we extracted from
    the part+money line with that next text-only line.
    """
    if not items:
        return items

    lines = text.splitlines()

    for li in items:
        if not li.part_number:
            continue

        # Find the first line containing the part number.
        part = li.part_number
        base_idx = None
        for idx, line in enumerate(lines):
            if part in line:
                base_idx = idx
                break

        if base_idx is None:
            continue

        # Look below that line for a description-only line (letters, no price).
        j = base_idx + 1
        while j < len(lines):
            peek = lines[j].strip()
            j += 1

            if not peek:
                continue

            has_letters = bool(re.search(r"[A-Za-z]", peek))
            has_money = bool(re.search(r"\d+\.\d{2}", peek))

            # Stop when we hit totals / new item / junk.
            if not has_letters or has_money:
                break

            up = peek.upper()
            if any(
                tok in up
                for tok in [
                    "INVOICE",
                    "TOTAL",
                    "SUBTOTAL",
                    "SUB-TOTAL",
                    "GST",
                    "PST",
                    "HST",
                    "BILL TO",
                    "SHIP TO",
                ]
            ):
                break

            # This looks like a genuine description line.
            # For Action, we *override* the previous description with this.
            li.description = peek.strip()
            break  # only attach one line for now

    return items


def _normalize_lordco_description(desc: str) -> str:
    """
    Clean up Lordco descriptions like:
        "** Internet  Order  **TIE ROD ENDMethod Date Terms"
    into:
        "TIE ROD END"
    """
    s = desc

    # Drop obvious decoration
    s = re.sub(r"\*+", " ", s)

    # Remove "Internet Order" in any case
    s = re.sub(r"\b[Ii]nternet\b\s+\b[Oo]rder\b", " ", s)

    # Drop "Method Date Terms" and everything after it
    s = re.sub(r"\b[Mm]ethod\b\s+\b[Dd]ate\b\s+\b[Tt]erms\b.*", "", s)

    # Remove leading pure quantity (e.g. "1 ")
    s = re.sub(r"^\s*\d+\s+", " ", s)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fallback_lordco_items(text: str) -> List[LineItem]:
    """
    Fallback for Lordco Auto Parts when generic heuristics find no items.

    Strategy:
      1. First pass: scan for tokens that *could* be part numbers
         (mixed letters+digits, length >= 5), and count how often they appear.
      2. Second pass: only treat tokens that appear at least twice as
         real part numbers (to weed out VINs, order IDs, header codes, etc.).
      3. For each such part line:
           - Try to find a line_total from decimals on the same / next line.
           - Build a description from the same line (minus part + numbers).
           - If that description is tiny, look at the next line:
               * If it has letters, no money, and isn't obviously a header,
                 use that as the description.
           - Finally, normalize the description with _normalize_lordco_description.
    """
    items: List[LineItem] = []
    lines = text.splitlines()

    # ---------- First pass: gather candidate token frequencies ----------
    candidate_counts: Dict[str, int] = {}

    header_tokens = [
        "INVOICE",
        "STATEMENT",
        "SUBTOTAL",
        "SUB-TOTAL",
        "TOTAL",
        "GST",
        "PST",
        "HST",
        "ACCOUNT",
        "CUSTOMER",
        "BILL TO",
        "SHIP TO",
        "MAPLE RIDGE",
        "WEB ORDER",
        "VIN",
        "REGISTRATION",
        "OW ID",
    ]

    for line in lines:
        up = line.upper()
        if any(tok in up for tok in header_tokens):
            continue

        tokens = line.split()
        for tok in tokens:
            clean = re.sub(r"[^A-Za-z0-9]", "", tok)
            if len(clean) < 5:
                continue
            if not re.search(r"[A-Za-z]", clean):
                continue
            if not re.search(r"\d", clean):
                continue
            candidate_counts[clean] = candidate_counts.get(clean, 0) + 1

    if not candidate_counts:
        return items

    # ---------- Second pass: build items from repeated candidates ----------
    seen_no_price_parts: set[str] = set()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        up = line.upper()
        if any(tok in up for tok in header_tokens):
            i += 1
            continue

        tokens = line.split()
        part_candidates: List[str] = []
        for tok in tokens:
            clean = re.sub(r"[^A-Za-z0-9]", "", tok)
            if len(clean) < 5:
                continue
            if not re.search(r"[A-Za-z]", clean):
                continue
            if not re.search(r"\d", clean):
                continue
            # Only keep tokens that appear more than once on the page
            if candidate_counts.get(clean, 0) >= 2:
                part_candidates.append(clean)

        if not part_candidates:
            i += 1
            continue

        item_line = line
        money = re.findall(r"(\d+\.\d{2})", item_line)

        # If no money on this line, see if the next line is a pure-money line
        if not money and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            next_has_letters = bool(re.search(r"[A-Za-z]", next_line))
            next_money = re.findall(r"(\d+\.\d{2})", next_line)
            if next_money and not next_has_letters:
                item_line = item_line + " " + next_line
                money = next_money
                i += 1  # consume the money-only line as part of this item

        line_total: Optional[float] = None
        if money:
            try:
                line_total = float(money[-1])
            except Exception:
                pass

        # Use the first candidate as the part number
        part = part_candidates[0]

        # If there's no per-line price and we've already created an item
        # for this part, don't duplicate it.
        if line_total is None and part in seen_no_price_parts:
            i += 1
            continue

        if line_total is None:
            seen_no_price_parts.add(part)

        # Base description: same line minus part + standalone integers + money
        desc = item_line
        desc = desc.replace(part, "")
        desc = re.sub(r"\b\d+\b", "", desc)
        for mv in money:
            desc = desc.replace(mv, "")
        desc = desc.strip()

        # If description is tiny (e.g. just "1"), try the next line for text-only desc
        if len(desc) < 3 and i + 1 < len(lines):
            peek = lines[i + 1].strip()
            has_letters = bool(re.search(r"[A-Za-z]", peek))
            has_money = bool(re.search(r"\d+\.\d{2}", peek))
            up_peek = peek.upper()

            if (
                has_letters
                and not has_money
                and not any(tok in up_peek for tok in header_tokens)
            ):
                desc = peek.strip()
                i += 1  # consume the description line

        # Lordco-specific cleanup (e.g. strip "Internet Order", "Method Date Terms", etc.)
        desc = _normalize_lordco_description(desc) if desc else None

        items.append(
            LineItem(
                raw_line=item_line,
                part_number=part,
                description=desc or None,
                quantity=None,
                unit_price=None,
                line_total=line_total,
            )
        )

        i += 1

    return items


def build_invoice(text: str, pages: List[int]) -> InvoiceData:
    inv_num = extract_invoice_number(text)
    inv_date = extract_invoice_date(text)
    po = extract_po_number(text)
    supplier = extract_supplier_name(text)
    supplier_type = classify_supplier(supplier)

    subtotal, taxes, total = extract_totals(text)
    line_items = extract_line_items(text)
    line_items = _clean_summary_line_items(line_items, subtotal, total)

    # Supplier-specific tweak: Action Car & Truck descriptions are often on the line below.
    if supplier and "ACTION CAR & TRUCK" in supplier.upper():
        line_items = _augment_action_descriptions(text, line_items)

    # Fallback for Lordco: if we still have no items but money is present,
    # run a more lenient pass looking for mixed alpha-numeric part codes.
    if supplier and "LORDCO" in supplier.upper() and not line_items and (subtotal or total):
        line_items = _fallback_lordco_items(text)

    # Fallback: if subtotal is missing but we have line totals, sum them
    if subtotal is None and line_items:
        line_sum = sum(li.line_total for li in line_items if li.line_total)
        if line_sum > 0:
            subtotal = round(line_sum, 2)

    return InvoiceData(
        supplier_name=supplier,
        supplier_type=supplier_type,
        invoice_number=inv_num,
        invoice_date=inv_date,
        po_number=po,
        subtotal=subtotal,
        taxes=taxes,
        total=total,
        pages=pages,
        line_items=line_items,
        raw_text=text,
    )


def split_into_invoices(page_texts: List[str]) -> List[InvoiceData]:
    """
    Strategy:

    1. For every page, compute invoice_number.
    2. Group pages by invoice_number for inv != None.
    3. For pages with inv == None:
         - If they contain money (subtotal or total), treat each as its own invoice.
         - If they contain no money, ignore (cover pages / junk).
    4. Return all invoices sorted by first page index.
    """
    invoices: List[InvoiceData] = []

    invoice_numbers: List[Optional[str]] = []
    for txt in page_texts:
        invoice_numbers.append(extract_invoice_number(txt))

    # Group pages by invoice number for inv != None
    inv_to_pages: Dict[str, List[int]] = {}
    for idx, inv in enumerate(invoice_numbers):
        if inv is None:
            continue
        inv_to_pages.setdefault(inv, []).append(idx)

    # Build invoices for pages with known invoice numbers
    for inv, pages in sorted(inv_to_pages.items(), key=lambda kv: kv[1][0]):
        joined_text = "\n".join(page_texts[p] for p in pages)
        invoices.append(build_invoice(joined_text, pages))

    # Mark which pages have already been used
    used_pages = {p for pages in inv_to_pages.values() for p in pages}

    # Handle pages with no invoice number
    for idx, txt in enumerate(page_texts):
        if idx in used_pages:
            continue

        subtotal, taxes, total = extract_totals(txt)
        if subtotal is None and total is None:
            # No money at all: treat as header/junk
            continue

        # This page looks like an actual invoice, just with no readable invoice number
        invoices.append(build_invoice(txt, [idx]))

    # Sort invoices by the first page index to preserve document order
    invoices.sort(key=lambda inv: min(inv.pages))

    return invoices


# -------------------- OCR + PDF writing -------------------- #

def run_ocr_if_requested(input_pdf: Path, do_ocr: bool) -> Path:
    """
    Run ocrmypdf if requested; otherwise return the original.
    We use --skip-text so it won't re-OCR pages that already have text.
    """
    if not do_ocr:
        return input_pdf

    tmp_dir = Path(tempfile.mkdtemp(prefix="invoice_ocr_"))
    out_pdf = tmp_dir / (input_pdf.stem + "_ocr.pdf")

    console.print(f"[cyan]Running OCR on[/cyan] {input_pdf} -> {out_pdf}")
    cmd = [
        "ocrmypdf",
        "--skip-text",
        "-l",
        "eng",
        "--deskew",
        str(input_pdf),
        str(out_pdf),
    ]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        console.print("[red]ocrmypdf not found on PATH. Install it or use --no-ocr.[/red]")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]ocrmypdf failed with exit code {e.returncode}[/red]")
        sys.exit(e.returncode)

    return out_pdf


def save_invoices_as_pdfs(
    original_pdf: Path,
    invoices: List[InvoiceData],
    output_dir: Path,
) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reader = PyPDF2.PdfReader(str(original_pdf))
    written_paths: List[Path] = []

    for idx, inv in enumerate(invoices, start=1):
        writer = PyPDF2.PdfWriter()
        for pidx in inv.pages:
            writer.add_page(reader.pages[pidx])

        parts = []
        if inv.supplier_name:
            parts.append(inv.supplier_name)
        if inv.invoice_number:
            parts.append(inv.invoice_number)
        else:
            parts.append(f"invoice_{idx}")

        name = "_".join(parts)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        out_path = output_dir / f"{name}.pdf"

        with open(out_path, "wb") as f:
            writer.write(f)

        written_paths.append(out_path)

    return written_paths


# -------------------- SQLite helpers -------------------- #

def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE,
            type TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY,
            supplier_id INTEGER,
            invoice_number TEXT,
            invoice_date TEXT,
            po_number TEXT,
            subtotal REAL,
            total REAL,
            taxes_json TEXT,
            pdf_path TEXT,
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS line_items (
            id INTEGER PRIMARY KEY,
            invoice_id INTEGER,
            part_number TEXT,
            description TEXT,
            quantity REAL,
            unit_price REAL,
            line_total REAL,
            raw_line TEXT,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        )
        """
    )
    conn.commit()
    return conn


def upsert_supplier(
    conn: sqlite3.Connection,
    name: Optional[str],
    supplier_type: Optional[str],
) -> Optional[int]:
    if not name:
        return None
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO suppliers(name, type) VALUES (?, ?)",
        (name, supplier_type),
    )
    cur.execute("SELECT id FROM suppliers WHERE name = ?", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def insert_invoice_into_db(
    conn: sqlite3.Connection,
    inv: InvoiceData,
    pdf_path: Path,
) -> None:
    cur = conn.cursor()
    supplier_id = upsert_supplier(conn, inv.supplier_name, inv.supplier_type)
    taxes_json = json.dumps(inv.taxes)

    cur.execute(
        """
        INSERT INTO invoices(
            supplier_id, invoice_number, invoice_date, po_number,
            subtotal, total, taxes_json, pdf_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            supplier_id,
            inv.invoice_number,
            inv.invoice_date,
            inv.po_number,
            inv.subtotal,
            inv.total,
            taxes_json,
            str(pdf_path),
        ),
    )
    inv_id = cur.lastrowid

    for li in inv.line_items:
        cur.execute(
            """
            INSERT INTO line_items(
                invoice_id, part_number, description, quantity,
                unit_price, line_total, raw_line
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inv_id,
                li.part_number,
                li.description,
                li.quantity,
                li.unit_price,
                li.line_total,
                li.raw_line,
            )
        )

    conn.commit()


# -------------------- CLI entry -------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split a bulk invoice PDF and extract key fields.",
    )
    parser.add_argument(
        "input_pdf",
        type=Path,
        help="Bulk scanned PDF (image-only or already OCR'd).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out_invoices"),
        help="Directory for individual invoice PDFs (default: out_invoices)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional SQLite DB path for extracted data.",
    )
    parser.add_argument(
        "--no-ocr",
        dest="ocr",
        action="store_false",
        help="Disable OCR and assume input_pdf already has text.",
    )
    parser.set_defaults(ocr=True)

    args = parser.parse_args(argv)

    input_pdf: Path = args.input_pdf
    if not input_pdf.exists():
        console.print(f"[red]Input PDF not found:[/red] {input_pdf}")
        return 1

    ocr_pdf = run_ocr_if_requested(input_pdf, do_ocr=args.ocr)

    console.print(f"[cyan]Reading:[/cyan] {ocr_pdf}")
    reader = PyPDF2.PdfReader(str(ocr_pdf))
    page_texts = [p.extract_text() or "" for p in reader.pages]

    invoices = split_into_invoices(page_texts)
    if not invoices:
        console.print(
            "[red]No invoices detected. Check OCR output and invoice number patterns.[/red]"
        )
        return 1

    written_paths = save_invoices_as_pdfs(ocr_pdf, invoices, args.output_dir)

    # Summary table
    console.print()
    table = Table(title="Extracted invoices")
    table.add_column("#")
    table.add_column("Supplier")
    table.add_column("Type")
    table.add_column("Invoice #")
    table.add_column("Date")
    table.add_column("Pages")
    table.add_column("Subtotal")
    table.add_column("Total")
    table.add_column("PDF path")

    for idx, (inv, path) in enumerate(zip(invoices, written_paths), start=1):
        table.add_row(
            str(idx),
            inv.supplier_name or "?",
            inv.supplier_type or "?",
            inv.invoice_number or "?",
            inv.invoice_date or "?",
            ",".join(str(p + 1) for p in inv.pages),
            f"{inv.subtotal:.2f}" if inv.subtotal is not None else "",
            f"{inv.total:.2f}" if inv.total is not None else "",
            str(path),
        )
    console.print(table)

    if args.db:
        conn = init_db(args.db)
        for inv, path in zip(invoices, written_paths):
            insert_invoice_into_db(conn, inv, path)
        console.print(f"[green]Stored {len(invoices)} invoice(s) in[/green] {args.db}")

    console.print("[green]Done.[/green]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
