# Vinayak Group · AI-assisted procurement · demo on real data

A single-page walkthrough of one construction work package (internal waterproofing,
Tower-2) moving from a site email to a comparative statement the HOD can sign.
The agent reads, normalises and ranks. **It never selects, orders or pays: the agent
recommends and a human decides.**

Built for **Vibe Coders Hackathon Ed. 3** (21 Nov 2026) on Vinayak Group's data pack.

## The six steps

| # | Step | What the agent does |
|---|------|---------------------|
| 1 | Capture the requirement | Turns an email, WhatsApp text or voice note into a structured requirement and checks it against the site quantity sheet |
| 2 | Match item codes | Maps each BOQ line to an ERP item code and flags duplicate or conflicting codes |
| 3 | Draft & send RFQs | Suggests vendors, drafts an RFQ with the company's standard terms, and sends to a sandbox inbox only |
| 4 | Read & normalise quotes | Extracts rates and terms from 4 differently formatted quotes (one a handwritten scan), each value traced to its page or clause |
| 5 | Comparative statement | Re-prices every quote on the same tender quantities and ranks with adjustable weights for price, terms and history |
| 6 | Recommend to HOD | Writes the recommendation note. The HOD approves, sends back or overrides, and every decision is logged with a reason |

**The trap:** three of the four quotes were priced on an older, larger quantity set.
Add up each PDF's own totals and the most expensive vendor looks the cheapest.
Step 5's "Tender quantities / As quoted" toggle shows the difference.

## Data

The workflow, checks and findings come from the real hackathon data pack. In this
public version, **vendor names, people, project and site names, quantities and rates
are realistic stand-ins**, not Vinayak's actual figures. Agent outputs are pre-computed
so the demo runs offline.

## Run it

It's one static file with no build step:

```sh
open index.html          # or: python3 -m http.server and visit localhost:8000
```
