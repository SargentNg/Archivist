import fitz

W = "/tmp/opencode/scan-ort-test"
WATCH = W + "/watch"

P1 = """IRS FORM 1099-NEC
Internal Revenue Service
United States Federal Government
Official Use: Nonemployee Compensation

Tax Year: 2025
Payer: BigCo Consulting LLC
Payer TIN: 84-1234567
Recipient: John J. Doe
Box 1 Nonemployee compensation: $12,345
Withholding tax: $0"""

P2 = """First City Bank
Account Statement
Statement Date: October 1, 2025
Account Number: 1234567890
Routing Number: 021000021
Checking Account

Period: October 2025
Balance: $1,234.56

A deposit of $500.00 posted on October 12.
A withdrawal of $75.22 on October 15.
End of first page of statement."""

P3 = """First City Bank
Account Statement
Account Number: 1234567890

Transactions, continued
09/28 DEPOSIT $500.00
09/29 CHECK    $75.22
10/31 INTEREST $12.04

Balance forward: $1,234.56

Continued on next page
Page 2 of 4"""

P4 = """ACME CONTRACTING
Invoice #4021
Bill To: John J. Doe
Date: October 30, 2025
Terms: Net 30

Contract work - October
$1,500.00

Amount Due: $1,500.00
Thank you."""

OCR_LINES = [
    "City Power and Water Co.",
    "",
    "UTILITY BILL",
    "",
    "Service Period: 09/01/2025 - 09/30/2025",
    "Total Consumption: 312 kWh",
    "Amount Due: $48.72",
    "Please remit by October 15, 2025.",
]

def text_page(doc, text, fontsize=12):
    page = doc.new_page(width=612, height=792)
    y = 72.0
    for line in text.splitlines():
        if y > 720:
            break
        if line.strip():
            page.insert_text((72, y), line, fontsize=fontsize, fontname="helv")
        y += 18 if line.strip() else 10
    return page

doc = fitz.open()
for p in (P1, P2, P3, P4):
    text_page(doc, p)
doc.save(WATCH + "/multi_scan.pdf")
doc.close()

# image-only PDF: render text to a pixmap, embed as PNG (no text layer)
src = fitz.open()
text_page(src, "\n".join(OCR_LINES), fontsize=15)
pix = src[0].get_pixmap(dpi=200)
png = W + "/_ocr_page.png"
pix.save(png)
src.close()
out = fitz.open()
p = out.new_page(width=612, height=792)
p.insert_image(p.rect, filename=png)
out.save(WATCH + "/ocr_scan.pdf")
out.close()

# mixed bundle: page 1 = digital text layer, page 2 = image-only (scanner-style)
MIXED_OCR_LINES = [
    "City Power and Water Co.",
    "",
    "UTILITY BILL",
    "",
    "Service Period: 10/01/2025 - 10/31/2025",
    "Total Consumption: 288 kWh",
    "Amount Due: $41.20",
    "Please remit by November 14, 2025.",
]
mixed = fitz.open()
text_page(mixed, "ACME CONTRACTING\nInvoice #4022\nBill To: John J. Doe\nDate: November 3, 2025\nContract work - October\nAmount Due: $820.00\nThank you.")
src2 = fitz.open()
text_page(src2, "\n".join(MIXED_OCR_LINES), fontsize=15)
pix2 = src2[0].get_pixmap(dpi=200)
png2 = W + "/_mixed_pg2.png"
pix2.save(png2)
src2.close()
p2 = mixed.new_page(width=612, height=792)
p2.insert_image(p2.rect, filename=png2)
mixed.save(WATCH + "/mixed_scan.pdf")
mixed.close()

# single-page text PDF: a monthly LES statement (no day-level date)
les_doc = fitz.open()
text_page(les_doc, "LES Statement\n\nPeriod: May 2026\nAccount: 123-4567\nAmount received: $1,250.00\nPayment due: June 30, 2026.")
les_doc.save(WATCH + "/les_scan.pdf")
les_doc.close()

open(WATCH + "/junk.txt", "w").write("not a pdf\n")
print("fixtures written")
