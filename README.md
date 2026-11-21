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

## Working code: email intake (step 1)

Step 1 now runs for real on email. A site email (`.eml`, or pasted text plus the attached
quantity sheet) becomes a structured requirement and a set of BOQ checks.

- **Claude reads the words.** It extracts project, scope, floors, schedule, who asked for what,
  any vendor named before tender, and the position each message in the thread takes. Every field
  has to quote the email verbatim. A program then checks each quote against the email, and any
  field whose quote isn't there is dropped and listed. Model: `claude-opus-5-5`, structured
  output, with server-side refusal fallback on.
- **Code reads the numbers.** The `.xlsx` quantity sheet is parsed without an LLM, and the
  SOP step 02 checks are plain code: vendor named before tender (SOP 05 needs 3 quotes), lines
  not measured, the sqm→sqft factor, row sums and the grand total.

```sh
uv sync
uv run uvicorn procurement.api:app --reload     # open http://localhost:8000 (or /#sample)
uv run python -m procurement samples/riverside_t2_waterproofing.eml   # CLI, prints JSON
uv run pytest                                    # 14 tests, no API calls
```

Set `ANTHROPIC_API_KEY` to read your own emails. Without it, only the sample runs, in
**replay mode**: it uses `samples/riverside_t2_waterproofing.extraction.json`, a hand-written
reference extraction (not a recorded model output), and the page says so.
`INTAKE_REPLAY=1` forces replay mode; `INTAKE_MODEL` overrides the model.

| Path | What it is |
|---|---|
| `procurement/intake/email_parser.py` | `.eml` / pasted text → headers, body, reply thread, attachments |
| `procurement/intake/quantity_sheet.py` | Site quantity sheet (.xlsx) → BOQ lines, no LLM |
| `procurement/intake/extract.py` | Claude extraction + quote verification |
| `procurement/intake/checks.py` | SOP step 02 BOQ checks |
| `procurement/api.py`, `procurement/web/intake.html` | FastAPI endpoints and the intake page |
| `scripts/make_samples.py` | Rebuilds the anonymised sample email and sheet |

### Gmail inbox

The intake page can also pull mail straight from Gmail. It reads every email in the inbox (or only
from the senders you list), runs each email through intake once, and saves the results in `data/inbox/`
(git-ignored). It reads over IMAP with `BODY.PEEK`, so nothing is marked as read.

1. Turn on 2-Step Verification for the Gmail account, then create an app password at
   https://myaccount.google.com/apppasswords
2. `cp .env.example .env` and fill in `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and
   `ANTHROPIC_API_KEY`. Best setup: a dedicated intake address (say `requirements@…`) that sites
   send or forward to. Leave `WATCH_FROM` empty and every email there is read. To use a shared
   inbox instead, set `WATCH_FROM` to the comma-separated senders to read.
3. Restart the server and press **Check for new mail**, or run `uv run python -m procurement.mailbox`.

Emails fetched before an API key is set are saved as "needs API key" and read on the next sync.
For the hackathon, use a test inbox and send it the sample: `samples/riverside_t2_waterproofing.eml`.

### Email rules: choose which emails get read

Rules run before Claude sees an email, so newsletters, vendor spam and internal chatter are
skipped and cost nothing. Each rule allows or blocks emails by sender, sender domain, subject,
email text, To/Cc address, whether there is an attachment, or attachment type (xlsx, pdf…), using
*contains*, *is exactly*, *starts with* or a pattern (regex). Matching ignores case, and a broken
pattern is refused when you save it.

**How rules combine:** block rules always win; if you have any allow rules, an email must match
one of them or it is held; with no allow rules, every email that isn't blocked is read.

- A blocked or held email is saved with status `skipped` and the reason, and isn't retried.
  Delete its `data/inbox/<uid>.json` to have the next sync look at it again.
- The screen previews every rule on the demo emails and your saved inbox before you save it.
- `WATCH_FROM` still works: each sender listed there acts as an allow rule and shows as
  read-only "from .env". It also limits which emails Gmail sends us, so an allow rule for any
  other sender only takes effect once `WATCH_FROM` is empty.
- Rules are saved in `data/email_rules.json` (or `EMAIL_RULES_PATH`). Code:
  `procurement/email_rules/` (API under `/api/email-rules`), UI: `procurement/web/email_rules.js`.

The sample is anonymised like the rest of this repo. Keep the real data pack out of this
public repo (`data/` is git-ignored).

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
