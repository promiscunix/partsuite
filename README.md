# Partsuite – Invoice & Receipts Reconciliation

This project is a Python + SQLite toolchain (with a Nix dev shell) to help reconcile:

- What we’ve **been billed for** on supplier invoices (FCA/Mopar + external suppliers)
- What we’ve **received** according to CDK transaction exports

It’s built around:

- **Scanned PDFs** of invoices (FCA, NAPA, Lordco, Action, etc.)
- **CDK CSV exports** where:
  - `Transcode = 'R'` → FCA/Mopar shipment (receipts)
  - `Transcode = 'O'` → manually entered external supplier receipts

Everything ends up in a single `invoices.db` SQLite database.

---

## Dev environment (Nix)

This repo includes a `flake.nix` that sets up the tools you need:

- Python (with PyPDF, PyPDF2, rich, dateutil)
- `ocrmypdf`, `tesseract`, `qpdf`, `poppler_utils` (for OCR and text extraction)

To enter the dev shell:

```bash
cd invoice_master   # or your repo root
nix develop
```

You should see something like:

```text
Invoice dev shell ready.
Example:
  python invoice_pipeline.py bulk_scan.pdf --output-dir out_invoices --db invoices.db
```

All the example commands below assume you’re inside this `nix develop` shell.

---

## Database schema (high level)

The SQLite database `invoices.db` uses four main tables:

- `suppliers`
  - `id`
  - `name` – e.g. `NAPA Port Kells`, `Lordco Auto Parts`, `Mopar Canada Inc. - Parts Invoice`, `Maple Ridge Chrysler`
  - `type` – e.g. `general`, `chrysler_corp`, `self`

- `invoices`
  - `id`
  - `supplier_id` → FK into `suppliers`
  - `invoice_number`
  - `invoice_date` (YYYY-MM-DD)
  - `po_number` (nullable)
  - `subtotal`
  - `total`
  - `pages` / `pdf_path` / other metadata

- `line_items`
  - `id`
  - `invoice_id` → FK into `invoices`
  - `part_number` (canonical; see normalization below)
  - `description`
  - `quantity`
  - `unit_price`
  - `line_total`

- `receipts_lines`
  - `id`
  - `supplier_name` (free text from CDK)
  - `transcode` – `'R'` for FCA shipments, `'O'` for manual external suppliers
  - `part_number` (canonical)
  - `qty_received`
  - `invoice_number` / `posting_date` / other info from the CDK CSV

> **Important:** all part numbers are normalized before being written to the DB (see below).

---

## Part number normalization

To keep things consistent between PDFs, CDK exports, and different supplier formats, we use a single function `normalize_part()` (defined in `invoice_pipeline.py`) everywhere.

Rules:

- **Uppercase**
- **Only letters and digits** (A–Z, 0–9)
- **Remove spaces, dashes, punctuation**
- **Strip leading zeros**

Examples:

- `" 0VU01321-AC "` → `"VU01321AC"`
- `"0651-2211 aa"` → `"6512211AA"`
- `"BAAUA200AB"` → `"BAAUA200AB"` (already fine)

This normalization is applied:

- When creating any `LineItem` (invoices from any supplier)
- When importing `receipts_lines` from the CDK CSV
- When running reconciliation reports

So comparisons are always apples-to-apples.

---

## Main scripts

### 1. `invoice_pipeline.py`

**Purpose:**  
Process a multi-invoice **supplier PDF** (e.g. NAPA, Lordco, Action) into:

- Separate per-invoice PDFs (saved under `--output-dir`)
- Structured header + line items in `invoices.db`

**Typical workflow:**

1. Scan a stack of supplier invoices to a PDF, e.g.:

   ```text
   bulk_scan.pdf
   ```

2. (Optional but recommended) run OCR yourself:

   ```bash
   ocrmypdf -l eng --deskew bulk_scan.pdf bulk_scan_ocr.pdf
   ```

3. Run the pipeline against the OCR’d file:

   ```bash
   python invoice_pipeline.py bulk_scan_ocr.pdf      --output-dir out      --db invoices.db      --no-ocr
   ```

   - `--output-dir out` → directory for split invoice PDFs
   - `--db invoices.db` → target SQLite DB
   - `--no-ocr` → don’t call `ocrmypdf` internally (we already did it)

This will:

- Split `bulk_scan_ocr.pdf` into individual invoice PDFs under `out/`
- Detect supplier, invoice number, date, subtotal, total
- Extract line items (part numbers, descriptions, quantities, line totals)
- Insert `suppliers`, `invoices`, and `line_items` rows into `invoices.db`
- Print a table of detected invoices to the terminal

---

### 2. `parse_fca_invoice.py`

**Purpose:**  
Parse an FCA / Mopar Canada **parts invoice PDF** into:

- Optional CSV for inspection
- Proper `suppliers` / `invoices` / `line_items` rows in `invoices.db`

**Usage:**

```bash
python parse_fca_invoice.py 11_15.pdf   --db invoices.db   --csv 11_15_lines.csv
```

- Treats the whole FCA PDF as **one invoice**:
  - Extracts **invoice number** and **invoice date**
  - Extracts **every line item** (ORD#, part number, quantity, prices)
- Creates a `supplier` with:
  - `name = "Mopar Canada Inc. - Parts Invoice"`
  - `type = 'chrysler_corp'`
- Inserts the invoice and line items into `invoices.db`
- Writes a debug CSV (`11_15_lines.csv`) with raw parsed lines

---

### 3. `import_receipts.py`

**Purpose:**  
Import a full CDK transaction CSV export into `receipts_lines`.

- CDK export includes all transactions.
- We care specifically about:
  - `Transcode = 'R'` → Mopar / FCA shipments
  - `Transcode = 'O'` → manually entered external supplier receipts

**Usage:**

```bash
python import_receipts.py /path/to/cdk_transactions.csv   --db invoices.db
```

This will:

- Read each line of the CSV
- Normalize `part_number` using `normalize_part()`
- Insert rows into `receipts_lines` with:
  - `supplier_name`
  - `transcode` (`'R'` or `'O'`)
  - `part_number`
  - `qty_received`
  - `invoice_number` / `posting_date` / etc.

This does **not** touch `suppliers` / `invoices` / `line_items` — it only populates the receipts side.

---

### 4. `report_fca_billed_vs_received.py`

**Purpose:**  
Reconcile **FCA/Mopar billed quantities** vs **CDK FCA receipts (`R`)**.

- Billed = all line items on invoices where:
  - Supplier name starts with `Mopar Canada%` **or**
  - `suppliers.type = 'chrysler_corp'`
- Received = `receipts_lines` where `transcode = 'R'`

**Usage:**

```bash
python report_fca_billed_vs_received.py --db invoices.db --limit 50
```

**Output:**

Two sections:

1. **Billed more than received (possible outstanding)**

   Parts where:

   ```text
   billed_qty > received_qty
   ```

   Example: Mopar billed you for 14 units, but CDK only shows 11 as received.

2. **Received more than billed (possible over-receipt / mismatch)**

   Parts where:

   ```text
   received_qty > billed_qty
   ```

   Often indicates:

   - Older invoices not yet scanned/parsed into the DB
   - True mismatch / correction

---

### 5. `report_manual_billed_vs_received.py`

**Purpose:**  
Reconcile **non-FCA supplier invoices** vs **manual CDK receipts (`O`)**.

- Billed = invoices where:
  - Supplier is **not** Mopar/FCA (`name NOT LIKE 'Mopar Canada%'` and `type != 'chrysler_corp'`)
  - Supplier is **not** `self` (Maple Ridge Chrysler)
- Received = `receipts_lines` where `transcode = 'O'`

**Usage:**

Global view across all suppliers:

```bash
python report_manual_billed_vs_received.py   --db invoices.db   --limit 50
```

Focus on one supplier (e.g. NAPA):

```bash
python report_manual_billed_vs_received.py   --db invoices.db   --limit 50   --supplier NAPA
```

Focus on Lordco:

```bash
python report_manual_billed_vs_received.py   --db invoices.db   --limit 50   --supplier Lordco
```

**Output:**

Same structure as the FCA report:

1. **Billed more than received (possible outstanding)**
2. **Received more than billed (possible over-receipt / mismatch)**

Now for your **NAPA / Lordco / Action** type suppliers.

---

### 6. `show_parts.py`

**Purpose:**  
Inspect invoice line items by invoice, mainly for debugging OCR/parsing.

Common usage:

```bash
python show_parts.py invoices.db --last --limit 9
```

This prints the last N invoices with:

- Supplier name
- Invoice number, date
- Line items:
  - `part_number`
  - `description`
  - `line_total`

Handy for checking:

- Are we capturing the right part numbers?
- Are descriptions being stitched correctly (e.g. multiline descriptions)?
- Are line totals lining up with the invoice?

Implementation details may vary, but the core idea is: **human-readable invoice contents** pulled from the DB.

---

### 7. `show_suppliers.py`

**Purpose:**  
Simple overview of which suppliers are in the system and how much you’ve billed with each.

**Usage:**

```bash
python show_suppliers.py --db invoices.db
```

Example output:

```text
 ID  Supplier                       Type                Invoices  Total billed
--------------------------------------------------------------------------------
  1  Action Car & Truck             general                   1        139.49
  2  Lordco Auto Parts              general                   3        336.36
  3  Mopar Canada Inc. - Parts...   chrysler_corp             1      12345.67
  4  NAPA Port Kells                general                   4        925.92
  ...
```

---

## Typical end-to-end workflow

1. **Start dev shell:**

   ```bash
   cd invoice_master
   nix develop
   ```

2. **Process a bulk supplier scan:**

   ```bash
   ocrmypdf -l eng --deskew bulk_scan.pdf bulk_scan_ocr.pdf

   python invoice_pipeline.py bulk_scan_ocr.pdf      --output-dir out      --db invoices.db      --no-ocr
   ```

3. **Import FCA weekly invoice:**

   ```bash
   python parse_fca_invoice.py 11_15.pdf      --db invoices.db      --csv 11_15_lines.csv
   ```

4. **Import CDK transactions CSV:**

   ```bash
   python import_receipts.py cdk_transactions.csv      --db invoices.db
   ```

5. **Reconcile FCA (R receipts):**

   ```bash
   python report_fca_billed_vs_received.py --db invoices.db --limit 50
   ```

6. **Reconcile external suppliers (O receipts):**

   ```bash
   python report_manual_billed_vs_received.py --db invoices.db --limit 50
   # Or per supplier:
   python report_manual_billed_vs_received.py --db invoices.db --limit 50 --supplier NAPA
   ```

7. **Spot check data:**

   ```bash
   python show_suppliers.py --db invoices.db
   python show_parts.py invoices.db --last --limit 5
   ```

---

## Resetting the DB (if needed)

Because everything is derived from:

- Scanned supplier PDFs
- FCA weekly invoice PDFs
- CDK transaction CSV

…it’s safe to blow away the DB and rebuild when you change parsing logic:

```bash
rm invoices.db

# Re-run all your imports:
python invoice_pipeline.py ...
python parse_fca_invoice.py ...
python import_receipts.py ...
```

This gives you a clean, normalized snapshot with the latest logic.

---

## Future ideas

- Frontend web UI for:
  - Drag-and-drop PDF upload
  - Viewing reconciliation results
  - Drilling into a specific part/supplier/invoice
- Extra reports:
  - Aging for outstanding items
  - “Billed-not-received” by supplier + date range
- More robust supplier-specific parsers (NAPA, Lordco, Action, tire vendors)
- Export reconciliation summary to CSV for management

For now, this repo provides a solid, script-based backbone to:

> **Track what you’ve been billed for vs what you’ve actually received**  
> across FCA and external suppliers, with everything anchored in a single SQLite DB.
