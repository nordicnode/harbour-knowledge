"""Generate the older refund policy PDF used by the seeded demo corpus."""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

HERE = Path(__file__).resolve().parent
TARGET = HERE / "corpus" / "old-refund-policy.pdf"


def build() -> Path:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(0, 9, "Harbour Refund Policy", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(
        0, 6, "Effective January 2024. Applies to all self-serve plans.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 7, "Annual plans", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        6,
        "Annual plans can be refunded within 30 days of purchase. "
        "The refund is returned to the original card.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 7, "Monthly plans", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0, 6, "Monthly plans are not refundable after the first charge.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 7, "How to request a refund", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        6,
        "Email billing@harbour.example with your invoice number. "
        "Refunds take up to ten business days.",
        new_x="LMARGIN", new_y="NEXT",
    )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(TARGET))
    return TARGET


if __name__ == "__main__":
    print(build())
