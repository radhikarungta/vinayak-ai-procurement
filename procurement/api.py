"""HTTP API + intake page.  Run:  uv run uvicorn procurement.api:app --reload"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import mailbox  # loads .env
from .intake import extract
from .intake.email_parser import RawAttachment
from .intake.models import IntakeResult
from .intake.pipeline import load_email, run_intake

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_EML = ROOT / "samples" / "riverside_t2_waterproofing.eml"
SAMPLE_REPLAY = ROOT / "samples" / "riverside_t2_waterproofing.extraction.json"
MAX_UPLOAD = 15 * 1024 * 1024

app = FastAPI(title="Vinayak procurement · email intake")
# Feature UIs (vendors.js, email_rules.js, …) are plain scripts served from procurement/web.
app.mount("/web", StaticFiles(directory=Path(__file__).parent / "web"), name="web")
# Feature routers (vendor directory, email rules, item codes) are included at the end of this file.


def _replay_allowed() -> bool:
    return os.environ.get("INTAKE_REPLAY") == "1" or not extract.has_credentials()


@app.get("/")
def page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "web" / "intake.html")


@app.get("/api/status")
def status() -> dict:
    return {"live": extract.has_credentials() and os.environ.get("INTAKE_REPLAY") != "1", "model": extract.MODEL}


@app.get("/api/samples/riverside", response_model=IntakeResult)
def sample() -> IntakeResult:
    """The anonymised sample. Live extraction when an API key is set, the saved reference extraction otherwise."""
    mail = load_email(SAMPLE_EML.read_bytes())
    try:
        return run_intake(mail, replay=SAMPLE_REPLAY if _replay_allowed() else None)
    except extract.IntakeError as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/demo/inbox")
def demo_inbox() -> list[dict]:
    """Sample set of received emails for the dashboard (built by scripts/make_demo_inbox.py)."""
    return json.loads((ROOT / "samples" / "demo_inbox.json").read_text())


@app.get("/api/inbox")
def inbox() -> dict:
    try:
        cfg = mailbox.MailConfig.from_env()
    except mailbox.MailboxError as e:
        return {"connected": False, "error": str(e), "items": mailbox.list_inbox()}
    return {"connected": cfg is not None, "config": cfg.public() if cfg else None, "items": mailbox.list_inbox()}


@app.post("/api/inbox/sync")
def inbox_sync() -> dict:
    try:
        cfg = mailbox.MailConfig.from_env()
        if cfg is None:
            raise HTTPException(400, "Gmail isn't set up. Fill GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env and restart.")
        return mailbox.sync(cfg)
    except mailbox.MailboxError as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/inbox/{uid}")
def inbox_item(uid: str) -> dict:
    if not uid.isdigit() or (rec := mailbox.load_result(uid)) is None:
        raise HTTPException(404, "No saved email with that id.")
    return rec


@app.post("/api/intake/email", response_model=IntakeResult)
async def intake_email(
    eml: UploadFile | None = File(None, description="A downloaded email (.eml) with its attachments"),
    text: str | None = Form(None, description="Pasted email text, if no .eml"),
    attachments: list[UploadFile] = File(default=[], description="Attachments for pasted text, e.g. the quantity sheet"),
) -> IntakeResult:
    if eml is None and not (text and text.strip()):
        raise HTTPException(400, "Send an .eml file or paste the email text.")
    if not extract.has_credentials():
        raise HTTPException(
            503,
            "No Anthropic API key is set, so only the sample can run (in replay mode). Set ANTHROPIC_API_KEY and restart to read your own emails.",
        )
    raw: bytes | str = (await eml.read(MAX_UPLOAD + 1)) if eml else text  # type: ignore[assignment]
    if isinstance(raw, bytes) and len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "That email is over 15 MB. Remove large attachments other than the quantity sheet.")
    extra = []
    for f in attachments:
        if f.filename:
            extra.append(RawAttachment(filename=f.filename, content_type=f.content_type or "application/octet-stream", data=await f.read(MAX_UPLOAD)))
    mail = load_email(raw, extra)
    try:
        return run_intake(mail)
    except extract.IntakeError as e:
        raise HTTPException(502, str(e)) from e


# ── Feature routers ─────────────────────────────────────────────────────────────────────────────
# Each feature lives in its own package with an `api.router`. A feature that isn't built yet is
# skipped (its dashboard card says so); any other import error is a real bug and is raised.
def _include_features() -> None:
    import importlib

    for pkg in ("itemcodes", "vendors", "email_rules", "shortlist", "cases"):
        try:
            mod = importlib.import_module(f"procurement.{pkg}.api")
        except ModuleNotFoundError as e:
            if e.name in (f"procurement.{pkg}", f"procurement.{pkg}.api"):
                continue
            raise
        app.include_router(mod.router)


_include_features()
