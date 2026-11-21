"""Build samples/demo_inbox.json: a sample set of received requirement emails for the dashboard.

Each email is a stand-in (anonymised projects, people and vendors). Its extraction is written by
hand below instead of calling Claude, but everything after that is the real pipeline: quotes are
verified against the email, the quantity sheet is parsed and the SOP step 02 checks run.
Run:  uv run python scripts/make_demo_inbox.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from procurement.intake.pipeline import load_email, run_intake  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def C(value=None, quote=None):
    return {"value": value, "quote": quote}


def L(values=(), quote=None):
    return {"values": list(values), "quote": quote}


def req(**kw):
    base = {k: C() for k in ("project", "block", "work_type", "contract_type", "floors", "start", "finish", "requested_by", "requested_action", "vendor_named", "vendor_named_basis")}
    base |= {k: L() for k in ("scope_areas", "attachments_referenced", "exclusions")}
    base |= {"thread": [], "open_questions": []}
    base.update(kw)
    return base


# Plain requirement emails: one ask, no vendor named, no quantity sheet, no reply thread.
# This is the set the dashboard shows for now. (uid, subject, from, date, body, extraction)
REQUIREMENTS = [
    # Three waterproofing asks, so step 2 (item codes) has lines the waterproofing ERP master can match.
    ("1011", "Waterproofing for toilets, kitchens and balconies, Greenfield Tower-1", "Greenfield Site <greenfield.site@example.com>", "Sat, 21 Nov 2026 11:10:00 +0530",
     """Dear Sir,

Internal waterproofing is needed for the toilets, kitchens and balconies of Vinayak Greenfield Tower-1, 2nd to 10th floor.
Brickwork in the wet areas is complete, so work can start from 1 December 2026 and should finish by 15 January 2027.
Please float the enquiry. The quantity sheet will follow once the site measurement is done.

Regards,
Site Engineer, Vinayak Greenfield""",
     req(project=C("Vinayak Greenfield", "Vinayak Greenfield"), block=C("Tower 1", "Greenfield Tower-1"),
         work_type=C("Internal waterproofing", "Internal waterproofing"), contract_type=C("Work order (waterproofing work package)", "Please float the enquiry."),
         scope_areas=L(["Toilets", "Kitchens", "Balconies"], "the toilets, kitchens and balconies"), floors=C("2nd to 10th floor", "2nd to 10th floor"),
         start=C("1 Dec 2026", "work can start from 1 December 2026"), finish=C("15 Jan 2027", "should finish by 15 January 2027"),
         requested_by=C("Site Engineer, Greenfield", "Site Engineer, Vinayak Greenfield"), requested_action=C("Float the enquiry", "Please float the enquiry."),
         thread=[{"who": "Site Engineer", "position": "Wet-area brickwork is done; float the waterproofing enquiry. Quantities to follow.", "quote": "The quantity sheet will follow once the site measurement is done."}],
         open_questions=["Floor and wall areas for toilets, kitchens and balconies are needed before vendors can price."])),
    ("1010", "Roof waterproofing with brick bat coba, Lakeview Tower-C", "Lakeview Site <lakeview.site@example.com>", "Sat, 21 Nov 2026 09:30:00 +0530",
     """Dear Sir,

The Tower-C main roof at Vinayak Lakeview is ready for waterproofing.
We need roof waterproofing with brick bat coba over the full roof area.
Work should start by 10 December 2026. Please float the enquiry.

Regards,
Site Engineer, Vinayak Lakeview""",
     req(project=C("Vinayak Lakeview", "Vinayak Lakeview"), block=C("Tower C", "Tower-C main roof"),
         work_type=C("Roof waterproofing with brick bat coba", "roof waterproofing with brick bat coba"), contract_type=C("Work order (waterproofing work package)", "Please float the enquiry."),
         scope_areas=L(["Main roof"], "Tower-C main roof"), floors=C("Roof", "main roof"),
         start=C("By 10 Dec 2026", "Work should start by 10 December 2026"),
         requested_by=C("Site Engineer, Lakeview", "Site Engineer, Vinayak Lakeview"), requested_action=C("Float the enquiry", "Please float the enquiry."),
         open_questions=["Roof area in sqm is needed before vendors can price.", "Is the mumty roof included?"])),
    ("1009", "Nozzle grouting at UGR construction joints, Riverside", "Riverside Site <riverside.site@example.com>", "Fri, 20 Nov 2026 15:40:00 +0530",
     """Dear Sir,

During the leak test of the underground reservoir (UGR) at Vinayak Riverside, seepage was seen at the construction joints.
Please arrange nozzle grouting at the construction joints before the tank is filled again.
Site wants the work done in the week of 30 November 2026.

Regards,
Site Engineer, Vinayak Riverside""",
     req(project=C("Vinayak Riverside", "Vinayak Riverside"), block=C("UGR (underground reservoir)", "underground reservoir (UGR)"),
         work_type=C("Nozzle grouting at construction joints", "nozzle grouting at the construction joints"), contract_type=C("Work order", "Please arrange nozzle grouting"),
         scope_areas=L(["UGR construction joints"], "seepage was seen at the construction joints"),
         start=C("Week of 30 Nov 2026", "in the week of 30 November 2026"), finish=C("Before the tank is filled again", "before the tank is filled again"),
         requested_by=C("Site Engineer, Riverside", "Site Engineer, Vinayak Riverside"), requested_action=C("Arrange nozzle grouting", "Please arrange nozzle grouting"),
         thread=[{"who": "Site Engineer", "position": "Leak test showed seepage at UGR construction joints; grout before refilling.", "quote": "seepage was seen at the construction joints"}],
         open_questions=["How many joints, or how many running metres, need grouting?"])),
    ("1008", "Requirement: OPC 53 cement for Tower-1 slab casting", "Riverside Site Store <riverside.store@example.com>", "Fri, 20 Nov 2026 10:05:00 +0530",
     """Dear Sir,

Please arrange 400 bags of OPC 53 grade cement for the Tower-1 9th floor slab casting at Vinayak Riverside.
Casting is planned for 28 November 2026, so material is needed at site by 26 November 2026.
Current stock is 60 bags.

Regards,
Site Store, Vinayak Riverside""",
     req(project=C("Vinayak Riverside", "Vinayak Riverside"), block=C("Tower 1", "Tower-1 9th floor slab casting"),
         work_type=C("OPC 53 cement, 400 bags", "400 bags of OPC 53 grade cement"), contract_type=C("Purchase order (material)", "Please arrange 400 bags"),
         scope_areas=L(["9th floor slab"], "Tower-1 9th floor slab casting"), floors=C("9th floor", "9th floor slab casting"),
         start=C("Needed at site by 26 Nov 2026", "needed at site by 26 November 2026"), finish=C("Casting on 28 Nov 2026", "Casting is planned for 28 November 2026"),
         requested_by=C("Site store, Riverside", "Site Store, Vinayak Riverside"), requested_action=C("Arrange the cement", "Please arrange 400 bags of OPC 53 grade cement"),
         thread=[{"who": "Site store", "position": "Needs 400 bags by 26 Nov for the 28 Nov casting.", "quote": "material is needed at site by 26 November 2026"}],
         open_questions=["Which brands are approved for structural concrete at Riverside?"])),
    ("1006", "TMT bars 12 mm and 16 mm, Lakeview Tower-B", "Lakeview Site <lakeview.site@example.com>", "Wed, 18 Nov 2026 11:15:00 +0530",
     """Dear Sir,

Requirement for Vinayak Lakeview, Tower-B, 6th to 8th floor columns and beams:
TMT Fe 550D, 12 mm: 9 MT
TMT Fe 550D, 16 mm: 14 MT
Required in two lots, first lot by 30 November 2026.
Bar bending schedule is attached.

Regards,
QS & Billing, Vinayak Lakeview""",
     req(project=C("Vinayak Lakeview", "Vinayak Lakeview"), block=C("Tower B", "Tower-B"),
         work_type=C("TMT Fe 550D bars, 23 MT", "TMT Fe 550D"), contract_type=C("Purchase order (material)", "Requirement for Vinayak Lakeview"),
         scope_areas=L(["12 mm: 9 MT", "16 mm: 14 MT"], "TMT Fe 550D, 12 mm: 9 MT"), floors=C("6th to 8th floor", "6th to 8th floor columns and beams"),
         start=C("First lot by 30 Nov 2026", "first lot by 30 November 2026"), requested_by=C("QS & Billing, Lakeview", "QS & Billing, Vinayak Lakeview"),
         requested_action=C("Supply in two lots", "Required in two lots"), attachments_referenced=L(["Bar bending schedule"], "Bar bending schedule is attached."),
         open_questions=["When is the second lot needed?"])),
    ("1004", "Scaffolding hire for facade work, Lakeview Tower-A", "Lakeview Site <lakeview.site@example.com>", "Mon, 16 Nov 2026 09:50:00 +0530",
     """Dear Sir,

Facade painting of Lakeview Tower-A starts on 7 December 2026.
We need cuplock scaffolding on hire for the north and east faces, ground to 14th floor, for about 10 weeks.
Please float the enquiry. Erection and dismantling to be in the vendor's scope.

Regards,
Site Engineer, Vinayak Lakeview""",
     req(project=C("Vinayak Lakeview", "Vinayak Lakeview"), block=C("Tower A", "Lakeview Tower-A"),
         work_type=C("Cuplock scaffolding on hire", "cuplock scaffolding on hire"), contract_type=C("Work order (hire with erection and dismantling)", "Erection and dismantling to be in the vendor's scope."),
         scope_areas=L(["North face", "East face"], "for the north and east faces"), floors=C("Ground to 14th floor", "ground to 14th floor"),
         start=C("7 Dec 2026", "starts on 7 December 2026"), finish=C("About 10 weeks of hire", "for about 10 weeks"),
         requested_by=C("Site Engineer, Lakeview", "Site Engineer, Vinayak Lakeview"), requested_action=C("Float the enquiry", "Please float the enquiry."),
         open_questions=["Face areas in sqm are needed for vendors to price hire per sqm per month."])),
    ("1003", "Tile adhesive, Greenfield Tower-3 bathrooms", "Greenfield Store <greenfield.store@example.com>", "Fri, 13 Nov 2026 12:20:00 +0530",
     """Dear Sir,

Please procure 650 bags (20 kg) of polymer tile adhesive, type C2TE, for the bathroom wall tiling in Greenfield Tower-3.
Delivery needed in the first week of December 2026.

Regards,
Site Store, Vinayak Greenfield""",
     req(project=C("Vinayak Greenfield", "Vinayak Greenfield"), block=C("Tower 3", "Greenfield Tower-3"),
         work_type=C("Polymer tile adhesive C2TE, 650 x 20 kg bags", "650 bags (20 kg) of polymer tile adhesive, type C2TE"), contract_type=C("Purchase order (material)", "Please procure 650 bags"),
         scope_areas=L(["Bathroom wall tiling"], "bathroom wall tiling"), start=C("First week of Dec 2026", "first week of December 2026"),
         requested_by=C("Site store, Greenfield", "Site Store, Vinayak Greenfield"), requested_action=C("Procure the adhesive", "Please procure 650 bags"))),
]

# Parked until the plain set works end to end: these add a vendor named before tender or
# missing quantities. The fully worked waterproofing thread (with its quantity sheet) is parked too.
LATER = [
    ("1007", "Fwd: Paver block road, Greenfield phase 2", "Greenfield Projects <greenfield.pm@example.com>", "Thu, 19 Nov 2026 17:42:00 +0530",
     """Dear Sir,

Forwarding the paver block requirement for the phase 2 internal road at Vinayak Greenfield.
As decided in the site visit on 14 November 2026, the MD suggested Shree Pavers since they did the phase 1 road.
Area is about 2,400 sqm, 80 mm interlocking pavers rated for fire tender load.
The detailed BOQ will follow from the structural consultant.

Work should start on 1 December 2026 and finish by 31 January 2027.

Thanks & Regards,
Project Manager, Vinayak Greenfield""",
     req(project=C("Vinayak Greenfield", "Vinayak Greenfield"), block=C("Phase 2", "phase 2 internal road"),
         work_type=C("Paver block internal road", "paver block requirement for the phase 2 internal road"), contract_type=C("Work order", "paver block requirement"),
         scope_areas=L(["Internal road, about 2,400 sqm", "80 mm interlocking pavers, fire-tender rated"], "Area is about 2,400 sqm, 80 mm interlocking pavers rated for fire tender load."),
         start=C("1 Dec 2026", "start on 1 December 2026"), finish=C("31 Jan 2027", "finish by 31 January 2027"),
         requested_by=C("Project Manager, Greenfield", "Project Manager, Vinayak Greenfield"), requested_action=C("Raise the paver road requirement", "Forwarding the paver block requirement"),
         vendor_named=C("Shree Pavers", "the MD suggested Shree Pavers"), vendor_named_basis=C("MD's suggestion at the 14 Nov site visit; they did the phase 1 road", "since they did the phase 1 road"),
         thread=[{"who": "Project Manager", "position": "Paver road requirement; MD suggested Shree Pavers. BOQ to follow.", "quote": "The detailed BOQ will follow from the structural consultant."}],
         open_questions=["The BOQ isn't attached yet. Wait for the structural consultant's BOQ before tendering.", "Confirm the 2,400 sqm area against the road drawing."])),
    ("1005", "Granite for lift lobbies, Riverside Tower-2", "Riverside Site <riverside.site@example.com>", "Tue, 17 Nov 2026 16:30:00 +0530",
     """Dear Sir,

We need to start the lift lobby flooring in Riverside Tower-2 from next month.
Please arrange black galaxy granite as per the architect's finish schedule.
Quantities will be shared once the lobby layout is frozen.

Regards,
Site Engineer, Vinayak Riverside""",
     req(project=C("Vinayak Riverside", "Vinayak Riverside"), block=C("Tower 2", "Riverside Tower-2"),
         work_type=C("Black galaxy granite, lift lobby flooring", "black galaxy granite"), contract_type=C("Purchase order (material)", "Please arrange black galaxy granite"),
         scope_areas=L(["Lift lobbies"], "lift lobby flooring"), start=C("Next month (December 2026)", "from next month"),
         requested_by=C("Site Engineer, Riverside", "Site Engineer, Vinayak Riverside"), requested_action=C("Arrange the granite", "Please arrange black galaxy granite"),
         open_questions=["No quantities yet: the lobby layout isn't frozen.", "Which floors? Thickness and finish (flamed or polished)?"])),
    ("1002", "WO for anti-termite treatment, Lakeview Tower-C", "Lakeview Site <lakeview.site@example.com>", "Wed, 11 Nov 2026 15:05:00 +0530",
     """Dear Sir,

Anti-termite treatment is needed for Lakeview Tower-C plinth before the PCC on 24 November 2026.
Please issue a work order to Kesari Construction, who did Tower-A and Tower-B.
Plinth area is 1,850 sqm as per the drawing.

Regards,
Site Engineer, Vinayak Lakeview""",
     req(project=C("Vinayak Lakeview", "Vinayak Lakeview"), block=C("Tower C", "Lakeview Tower-C"),
         work_type=C("Anti-termite treatment, plinth", "Anti-termite treatment"), contract_type=C("Work order", "Please issue a work order"),
         scope_areas=L(["Plinth, 1,850 sqm"], "Plinth area is 1,850 sqm"), start=C("Before PCC on 24 Nov 2026", "before the PCC on 24 November 2026"),
         requested_by=C("Site Engineer, Lakeview", "Site Engineer, Vinayak Lakeview"), requested_action=C("Issue a work order to the named vendor", "Please issue a work order to Kesari Construction"),
         vendor_named=C("Kesari Construction", "Please issue a work order to Kesari Construction"), vendor_named_basis=C("Did Tower-A and Tower-B", "who did Tower-A and Tower-B"),
         open_questions=["The PCC date is close. Is there time for three quotes, or does management want to approve a single-vendor exception?"])),
]


def main() -> None:
    records = []
    for uid, subject, sender, date, body, extraction in REQUIREMENTS:
        mail = load_email(f"SUBJECT: {subject}\nFROM: {sender}\nDATE: {date}\n\n{body}")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(extraction, f)
        res = run_intake(mail, replay=Path(f.name))
        Path(f.name).unlink()
        assert not res.dropped_fields, (uid, res.dropped_fields)
        records.append({"uid": uid, "received": date, "result": res.model_dump(mode="json")})

    out = SAMPLES / "demo_inbox.json"
    out.write_text(json.dumps(records, indent=1))
    print(f"wrote {out.name}: {len(records)} emails")


if __name__ == "__main__":
    main()
