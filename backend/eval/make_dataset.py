"""Generate a labelled synthetic invoice dataset.

Why synthetic: a real labelled invoice corpus cannot be shipped in a public
repo (the public ones are licence-encumbered and contain real company data).
Generating them means the ground truth is exact, so the accuracy numbers in
the evaluation report are measured rather than estimated.

What it produces, in `eval/dataset/`:
  files/      digital PDFs, scanned-style PDFs, JPG/PNG/TIFF images, XLSX, CSV
  ground_truth.json   one record per file with the true field values
  corrupt/    deliberately broken files for the failure-path tests

The scanned variants are rendered then re-rasterised with noise, blur and a
slight rotation, so the OCR path is exercised against genuinely degraded input
rather than a clean render.
"""
from __future__ import annotations

import json
import os
import random
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pymupdf
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(HERE, "dataset")
FILES = os.path.join(DATASET, "files")
CORRUPT = os.path.join(DATASET, "corrupt")

SUPPLIERS = [
    "Northwind Traders Ltd", "Acme Industrial Supply", "Belmont Logistics GmbH",
    "Riverside Medical Group", "Kestrel Office Solutions", "Pioneer Freight Co",
    "Delta Components Inc", "Harbourline Services", "Summit Paper Works",
    "Vantage Facilities Ltd",
]
CUSTOMERS = [
    "Cottage Home Care Services", "Riverside Select Services LLC",
    "Meridian Health Partners", "Oakfield Residential Trust",
    "Brightway Community Care", "Lakeshore Support Network",
]
ITEMS = [
    ("Nitrile examination gloves, box of 100", 8.50),
    ("Disposable face masks, box of 50", 12.00),
    ("Hand sanitiser 500ml", 4.25),
    ("Incontinence pads, carton", 32.90),
    ("Blood pressure monitor", 78.00),
    ("Wheelchair cushion, gel", 56.40),
    ("Clinical waste bags, roll of 50", 19.75),
    ("Printer paper A4, ream", 5.60),
    ("Toner cartridge, black", 64.30),
    ("Monthly scheduling software licence", 240.00),
    ("Caregiver training module", 150.00),
    ("Vehicle fuel allowance", 95.20),
]
CURRENCIES = ["USD", "EUR", "GBP"]
SYMBOL = {"USD": "$", "EUR": "EUR ", "GBP": "GBP "}


@dataclass
class LineItem:
    description: str
    quantity: float
    unit_price: float
    amount: float


@dataclass
class Invoice:
    invoice_number: str
    invoice_date: str
    supplier_name: str
    customer_name: str
    currency: str
    subtotal: float
    tax: float
    total: float
    line_items: list[LineItem] = field(default_factory=list)


def _round(value: float) -> float:
    return float(round(value + 1e-9, 2))


def make_invoice(rng: random.Random, index: int) -> Invoice:
    supplier = rng.choice(SUPPLIERS)
    customer = rng.choice(CUSTOMERS)
    currency = rng.choice(CURRENCIES)
    issued = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))

    chosen = rng.sample(ITEMS, rng.randint(2, 5))
    items: list[LineItem] = []
    for description, unit_price in chosen:
        quantity = rng.randint(1, 20)
        amount = _round(quantity * unit_price)
        items.append(LineItem(description, float(quantity), unit_price, amount))

    subtotal = _round(sum(i.amount for i in items))
    tax_rate = rng.choice([0.0, 0.05, 0.075, 0.10, 0.20])
    tax = _round(subtotal * tax_rate)
    total = _round(subtotal + tax)

    return Invoice(
        invoice_number=f"INV-2026-{index:05d}",
        invoice_date=issued.isoformat(),
        supplier_name=supplier,
        customer_name=customer,
        currency=currency,
        subtotal=subtotal,
        tax=tax,
        total=total,
        line_items=items,
    )


def render_pdf(inv: Invoice, path: str) -> None:
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    y = height - 25 * mm

    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, y, "INVOICE")
    c.setFont("Helvetica", 10)
    y -= 12 * mm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, inv.supplier_name)
    c.setFont("Helvetica", 9)
    y -= 5 * mm
    c.drawString(20 * mm, y, "Unit 4, Trade Park, Industrial Estate")
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, y, f"Invoice Number: {inv.invoice_number}")
    c.drawString(110 * mm, y, f"Invoice Date: {inv.invoice_date}")
    y -= 6 * mm
    c.drawString(20 * mm, y, f"Bill To: {inv.customer_name}")
    y -= 12 * mm

    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Description")
    c.drawRightString(120 * mm, y, "Qty")
    c.drawRightString(150 * mm, y, "Unit Price")
    c.drawRightString(185 * mm, y, "Amount")
    y -= 2 * mm
    c.line(20 * mm, y, 185 * mm, y)
    y -= 6 * mm

    c.setFont("Helvetica", 9)
    sym = SYMBOL[inv.currency]
    for item in inv.line_items:
        c.drawString(20 * mm, y, item.description[:58])
        c.drawRightString(120 * mm, y, f"{item.quantity:g}")
        c.drawRightString(150 * mm, y, f"{sym}{item.unit_price:,.2f}")
        c.drawRightString(185 * mm, y, f"{sym}{item.amount:,.2f}")
        y -= 6 * mm

    y -= 4 * mm
    c.line(120 * mm, y, 185 * mm, y)
    y -= 7 * mm
    c.drawRightString(150 * mm, y, "Subtotal:")
    c.drawRightString(185 * mm, y, f"{sym}{inv.subtotal:,.2f}")
    y -= 6 * mm
    c.drawRightString(150 * mm, y, "Tax:")
    c.drawRightString(185 * mm, y, f"{sym}{inv.tax:,.2f}")
    y -= 7 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(150 * mm, y, "TOTAL:")
    c.drawRightString(185 * mm, y, f"{sym}{inv.total:,.2f}")
    y -= 10 * mm
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, f"Currency: {inv.currency}   Payment terms: Net 30")

    c.showPage()
    c.save()


def degrade(image: Image.Image, rng: random.Random, severity: str) -> Image.Image:
    """Make a clean render look like a real scan."""
    arr = np.array(image.convert("L")).astype(np.float32)

    if severity == "light":
        noise_sigma, angle, blur = 6.0, rng.uniform(-0.6, 0.6), 0
    elif severity == "heavy":
        noise_sigma, angle, blur = 20.0, rng.uniform(-2.5, 2.5), 1
    else:
        noise_sigma, angle, blur = 12.0, rng.uniform(-1.5, 1.5), 0

    # Uneven illumination: a soft diagonal gradient, like a page lifted at one
    # edge on a flatbed scanner.
    h, w = arr.shape
    gradient = np.linspace(0.88, 1.08, w)[None, :] * np.linspace(1.05, 0.9, h)[:, None]
    arr = arr * gradient

    arr += np.random.normal(0, noise_sigma, arr.shape)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    out = Image.fromarray(arr)

    if blur:
        from PIL import ImageFilter
        out = out.filter(ImageFilter.GaussianBlur(radius=0.6))
    if abs(angle) > 0.1:
        out = out.rotate(angle, resample=Image.BICUBIC, fillcolor=245, expand=False)
    return out


def pdf_to_images(pdf_path: str, dpi: int = 200) -> list[Image.Image]:
    doc = pymupdf.open(pdf_path)
    images = []
    zoom = dpi / 72.0
    for page in doc:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    doc.close()
    return images


def write_scanned_pdf(inv: Invoice, path: str, rng: random.Random, severity: str) -> None:
    tmp = path + ".clean.pdf"
    render_pdf(inv, tmp)
    images = [degrade(img, rng, severity) for img in pdf_to_images(tmp)]
    images[0].save(path, "PDF", resolution=200.0,
                   save_all=True, append_images=images[1:])
    os.remove(tmp)


def write_image(inv: Invoice, path: str, rng: random.Random, fmt: str, severity: str) -> None:
    tmp = path + ".clean.pdf"
    render_pdf(inv, tmp)
    image = degrade(pdf_to_images(tmp)[0], rng, severity)
    if fmt == "JPEG":
        image.convert("RGB").save(path, "JPEG", quality=rng.randint(55, 80))
    else:
        image.save(path, fmt)
    os.remove(tmp)


def write_xlsx(invoices: list[Invoice], path: str) -> None:
    """A multi-sheet workbook: a register sheet plus a line-item sheet."""
    register = pd.DataFrame([
        {
            "Invoice Number": i.invoice_number,
            "Invoice Date": i.invoice_date,
            "Supplier": i.supplier_name,
            "Customer": i.customer_name,
            "Currency": i.currency,
            "Subtotal": i.subtotal,
            "Tax": i.tax,
            "Total": i.total,
        }
        for i in invoices
    ])
    lines = pd.DataFrame([
        {
            "Invoice Number": i.invoice_number,
            "Description": li.description,
            "Quantity": li.quantity,
            "Unit Price": li.unit_price,
            "Amount": li.amount,
        }
        for i in invoices for li in i.line_items
    ])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        register.to_excel(writer, sheet_name="Register", index=False)
        lines.to_excel(writer, sheet_name="Line Items", index=False)
        pd.DataFrame().to_excel(writer, sheet_name="Notes", index=False)


def write_csv(invoices: list[Invoice], path: str, delimiter: str = ",",
              encoding: str = "utf-8") -> None:
    df = pd.DataFrame([
        {
            "invoice_number": i.invoice_number,
            "invoice_date": i.invoice_date,
            "supplier_name": i.supplier_name,
            "customer_name": i.customer_name,
            "currency": i.currency,
            "subtotal": i.subtotal,
            "tax": i.tax,
            "total": i.total,
        }
        for i in invoices
    ])
    df.to_csv(path, index=False, sep=delimiter, encoding=encoding)


def write_corrupt_files() -> None:
    os.makedirs(CORRUPT, exist_ok=True)
    # Truncated PDF: valid header, nothing else.
    with open(os.path.join(CORRUPT, "truncated.pdf"), "wb") as f:
        f.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog")
    # Zero-byte file.
    open(os.path.join(CORRUPT, "empty.pdf"), "wb").close()
    # PDF extension, JPEG content.
    with open(os.path.join(CORRUPT, "mislabeled.pdf"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
    # Unsupported type.
    with open(os.path.join(CORRUPT, "notes.docx"), "wb") as f:
        f.write(b"PK\x03\x04" + b"\x00" * 100)
    # Garbage with an xlsx extension.
    with open(os.path.join(CORRUPT, "broken.xlsx"), "wb") as f:
        f.write(b"this is definitely not a spreadsheet")
    # Empty CSV.
    open(os.path.join(CORRUPT, "empty.csv"), "wb").close()


def build(n_digital: int = 14, n_scanned: int = 8, n_images: int = 6,
          seed: int = 20261103) -> dict:
    rng = random.Random(seed)
    np.random.seed(seed)

    if os.path.exists(DATASET):
        shutil.rmtree(DATASET)
    os.makedirs(FILES, exist_ok=True)

    ground_truth: dict[str, dict] = {}
    counter = 1

    for _ in range(n_digital):
        inv = make_invoice(rng, counter); counter += 1
        name = f"digital_{inv.invoice_number}.pdf"
        render_pdf(inv, os.path.join(FILES, name))
        ground_truth[name] = {**asdict(inv), "_variant": "digital_pdf"}

    for k in range(n_scanned):
        inv = make_invoice(rng, counter); counter += 1
        severity = ["light", "medium", "heavy"][k % 3]
        name = f"scanned_{severity}_{inv.invoice_number}.pdf"
        write_scanned_pdf(inv, os.path.join(FILES, name), rng, severity)
        ground_truth[name] = {**asdict(inv), "_variant": f"scanned_pdf_{severity}"}

    formats = [("JPEG", "jpg"), ("PNG", "png"), ("TIFF", "tiff")]
    for k in range(n_images):
        inv = make_invoice(rng, counter); counter += 1
        fmt, ext = formats[k % len(formats)]
        severity = ["light", "medium"][k % 2]
        name = f"image_{severity}_{inv.invoice_number}.{ext}"
        write_image(inv, os.path.join(FILES, name), rng, fmt, severity)
        ground_truth[name] = {**asdict(inv), "_variant": f"image_{fmt.lower()}_{severity}"}

    # Spreadsheet + CSV cover the same five invoices, so they can be
    # cross-verified against each other and against the PDFs.
    shared = [make_invoice(rng, counter + i) for i in range(5)]
    counter += 5
    write_xlsx(shared, os.path.join(FILES, "register_multi_sheet.xlsx"))
    ground_truth["register_multi_sheet.xlsx"] = {
        "_variant": "xlsx_multi_sheet",
        "_invoices": [asdict(i) for i in shared],
    }
    write_csv(shared, os.path.join(FILES, "register.csv"))
    ground_truth["register.csv"] = {
        "_variant": "csv", "_invoices": [asdict(i) for i in shared]
    }
    write_csv(shared, os.path.join(FILES, "register_semicolon_latin1.csv"),
              delimiter=";", encoding="latin-1")
    ground_truth["register_semicolon_latin1.csv"] = {
        "_variant": "csv_semicolon_latin1", "_invoices": [asdict(i) for i in shared]
    }
    # A matching PDF for each shared invoice, for pdf<->excel cross-check.
    for inv in shared:
        name = f"crosscheck_{inv.invoice_number}.pdf"
        render_pdf(inv, os.path.join(FILES, name))
        ground_truth[name] = {**asdict(inv), "_variant": "crosscheck_pdf"}

    # One deliberately inconsistent PDF: total does not equal subtotal + tax.
    bad = make_invoice(rng, 99001)
    bad.total = _round(bad.total + 25.00)
    name = "inconsistent_INV-2026-99001.pdf"
    render_pdf(bad, os.path.join(FILES, name))
    ground_truth[name] = {**asdict(bad), "_variant": "arithmetic_mismatch",
                          "_expect_issue": "total_mismatch"}

    # A genuine duplicate: the same invoice re-sent as a separate file, with
    # the same number, supplier and total. Reusing only the number would not
    # be a duplicate - different suppliers legitimately reuse short sequences,
    # and the validator is built to know the difference.
    first = ground_truth["digital_INV-2026-00001.pdf"]
    dup = Invoice(
        invoice_number=first["invoice_number"],
        invoice_date=first["invoice_date"],
        supplier_name=first["supplier_name"],
        customer_name=first["customer_name"],
        currency=first["currency"],
        subtotal=first["subtotal"],
        tax=first["tax"],
        total=first["total"],
        line_items=[LineItem(**li) for li in first["line_items"]],
    )
    name = "duplicate_INV-2026-00001.pdf"
    render_pdf(dup, os.path.join(FILES, name))
    ground_truth[name] = {**asdict(dup), "_variant": "duplicate",
                          "_expect_issue": "duplicate_invoice"}

    write_corrupt_files()

    with open(os.path.join(DATASET, "ground_truth.json"), "w") as handle:
        json.dump(ground_truth, handle, indent=2)

    return {
        "files": len(ground_truth),
        "dataset_dir": DATASET,
        "corrupt_files": len(os.listdir(CORRUPT)),
    }


if __name__ == "__main__":
    summary = build()
    print(json.dumps(summary, indent=2))
