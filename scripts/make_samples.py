"""Build the anonymised sample email + quantity sheet in samples/.

The shape copies a real site email from the hackathon data pack (a 3-message thread with the
quantity sheet attached). Names, project, vendor and quantities are stand-ins, matching the
public demo page (index.html). Run:  uv run python scripts/make_samples.py
"""

from __future__ import annotations

import io
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

OUT = Path(__file__).resolve().parent.parent / "samples"
IST = timezone(timedelta(hours=5, minutes=30))
SITE_FACTOR = 10.76  # what the site sheet uses (the exact factor is 10.764)
TENDER_FACTOR = 10.764

FLATS = ["FLAT - A", "FLAT - B", "FLAT - C", "FLAT - D", "GF / F.R.P."]
SPLIT = [0.29, 0.215, 0.315, 0.165, 0.015]

# code, description, items included, uom, tender sqft (None = not measured)
LINES = [
    ("WP.01", "WATERPROOFING AT TOILET FLOOR", "Flat toilets, servant W.C., GF disabled toilet", "SQM", 12618.40),
    ("WP.02", "WATERPROOFING AT TOILET WALL (UPTO 450 MM HEIGHT)", "Same as WP.01; turn-up 0.45 m, door openings deducted", "SQM", 10872.15),
    ("WP.03", "WATERPROOFING COMPOUND ON TOILET DEPRESSION CHANNEL", "-", "RM", None),
    ("WP.04", "WATERPROOFING AT KITCHEN & WASH PART FLOOR", "Kitchen sink patches, utility and wash areas", "SQM", 1694.30),
    ("WP.05", "WATERPROOFING AT KITCHEN WITH WASH PART WALL (AT WET AREA)", "Same; turn-up 0.45 m, door openings deducted", "SQM", 1805.10),
    ("WP.06", "WATERPROOFING AT BALCONY & SKY TERRACE FLOOR", "Balconies, double-height balconies, F.R.P. (4 nos)", "SQM", 8216.85),
    ("WP.07", "WATERPROOFING AT BALCONY & SKY TERRACE WALL (UPTO 200 MM HEIGHT)", "Same as WP.06; turn-up 0.175 m, door openings deducted", "SQM", 2296.95),
    ("WP.08", "NOZZLE GROUTING (IF REQUIRED)", "-", "NOS", None),
]


def build_sheet() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "WO ITEM-WISE BOQ"
    ws.append(["VINAYAK RIVERSIDE  -  TOWER-2"])
    ws.append(["WATERPROOFING QUANTITY"])
    ws.append(["SL NO", "WO CODE", "DESCRIPTION (AS PER WORK ORDER)", "ITEMS INCLUDED", "UOM", *FLATS, "TOTAL (SQM)", "TOTAL (SFT)", "REMARKS"])
    for c in ws[3]:
        c.font = Font(bold=True)
    totals = [0.0] * len(FLATS)
    for i, (code, desc, items, uom, sqft) in enumerate(LINES, start=1):
        if sqft is None:
            ws.append([i, code, desc, items, uom, *[None] * len(FLATS), None, None, "Not measured"])
            continue
        sqm = sqft / TENDER_FACTOR
        parts = [round(sqm * s, 4) for s in SPLIT[:-1]]
        parts.append(round(sqm - sum(parts), 4))
        totals = [t + p for t, p in zip(totals, parts)]
        ws.append([i, code, desc, items, uom, *parts, sum(parts), sum(parts) * SITE_FACTOR, None])
    grand = sum(totals)
    ws.append(["GRAND TOTAL - TOWER-2", None, None, None, None, *totals, grand, grand * SITE_FACTOR, None])
    ws[ws.max_row][0].font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


BODY = """Dear Sir,
As you requested, the quantities are attached herewith.

On Thu, Nov 12, 2026 at 1:58 PM Contracts & Planning <contracts@example.com> wrote:

> Dear Sir,
>
> As discussed with the MD, the contractor for the internal waterproofing
> works of Tower 2, covering the Ground Floor, 1st Floor and 3rd Floor
> onwards (including all typical floors), shall be finalised after proper
> evaluation and comparative analysis of the received tenders, followed by
> approval of the proposed rates from the management. The scope shall
> include waterproofing of all toilets/W.C., kitchens, wash areas,
> balconies, open terraces and F.R.P. works.
>
> However, for the mock-up floor, the waterproofing work shall be carried
> out by Nirmal as approved.
>
> Thanks & Regards,
> Sr. Engineer | Contracts & Planning
>
> From: Vinayak Riverside <riverside.site@example.com>
> Sent: 11 November 2026 19:41
> To: Vinayak Projects
> Subject: WO Requirement of Waterproofing works, Nirmal at Tower-2
>
> Dear Sir,
>
> Please find attached the waterproofing quantities for Tower 2, broken
> down by floor and flat.
>
> As discussed in our meeting on November 3, 2026, the MD advised engaging
> Nirmal for the internal waterproofing work (including all toilets/W.C.,
> kitchen, wash area, balcony, open terrace, and F.R.P.) for Tower 2,
> covering the ground floor, 1st floor, and 3rd floor through typical
> floors.
>
> The work is scheduled to begin in November 2026 and is expected to be
> completed by June 2028. Please issue the Work Order (WO) so we can
> proceed with the work on site.
>
> Best regards,
> QS & Billing Engineer
> Vinayak Riverside
"""


def build_eml(sheet: bytes) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Re: WO Requirement of Waterproofing works, Nirmal at Tower-2"
    msg["From"] = "Vinayak Riverside <riverside.site@example.com>"
    msg["To"] = "Contracts & Planning <contracts@example.com>"
    msg["Date"] = format_datetime(datetime(2026, 11, 16, 13, 20, 26, tzinfo=IST))
    msg["Message-ID"] = "<sample-riverside-t2-wp@example.com>"
    msg.set_content(BODY)
    msg.add_attachment(
        sheet,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="T2_waterproofing_quantities.xlsx",
    )
    return bytes(msg)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    sheet = build_sheet()
    (OUT / "T2_waterproofing_quantities.xlsx").write_bytes(sheet)
    (OUT / "riverside_t2_waterproofing.eml").write_bytes(build_eml(sheet))
    print("wrote", *(p.name for p in sorted(OUT.iterdir())))
