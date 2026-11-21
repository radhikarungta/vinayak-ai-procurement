"""Data shapes for email intake (step 1: capture the requirement)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── What the model returns ───────────────────────────────────────────────────────────────────
# Every field carries the verbatim `quote` it was read from. After extraction, each quote is
# checked against the email text (see extract.verify_quotes). A field whose quote isn't in the
# email is dropped, never shown. This is the same idea as IndustryPrime's anti-fabrication
# gate on QC.


class Cited(BaseModel):
    value: str | None = Field(description="The extracted value in plain words, or null if the email doesn't say.")
    quote: str | None = Field(description="Verbatim text copied from the email that supports the value. Null when value is null.")


class CitedList(BaseModel):
    values: list[str] = Field(description="Extracted items in plain words. Empty if the email doesn't say.")
    quote: str | None = Field(description="Verbatim text copied from the email that supports the items.")


class ThreadPosition(BaseModel):
    who: str = Field(description="Role or team of the writer as the email shows it, e.g. 'Site QS & Billing', 'Contracts & Planning'.")
    position: str = Field(description="What this message asks for or decides, in one plain sentence.")
    quote: str = Field(description="Verbatim text copied from that message.")


class ExtractedRequirement(BaseModel):
    project: Cited = Field(description="Project or site name.")
    block: Cited = Field(description="Tower, block or phase within the project.")
    work_type: Cited = Field(description="What kind of work or material, e.g. 'internal waterproofing'.")
    contract_type: Cited = Field(description="Work order (labour/service), purchase order (material) or both, as the email implies.")
    scope_areas: CitedList = Field(description="Areas or elements covered, e.g. toilets, kitchens, balconies.")
    floors: Cited = Field(description="Floors or levels covered.")
    start: Cited = Field(description="When work should start, as written.")
    finish: Cited = Field(description="When work should finish, as written.")
    requested_by: Cited = Field(description="Role and site of the person raising the requirement. Use roles, not personal names.")
    requested_action: Cited = Field(description="What the sender asks the back office to do, e.g. 'issue the work order'.")
    vendor_named: Cited = Field(description="A vendor or contractor the email proposes for this work before any tender. Null if none.")
    vendor_named_basis: Cited = Field(description="Why that vendor is named (who advised it, which meeting). Null if no vendor is named.")
    attachments_referenced: CitedList = Field(description="Documents the email says are attached, e.g. 'quantity sheet'.")
    exclusions: CitedList = Field(description="Anything the email explicitly leaves out of scope or gives to someone else.")
    thread: list[ThreadPosition] = Field(description="One entry per message in the thread that states a position, oldest first.")
    open_questions: list[str] = Field(description="Information the back office still needs before tendering, in plain sentences. No quotes needed.")


# ── What the quantity sheet parser returns ─────────────────────────────────────────────────────


class BoqLine(BaseModel):
    sl_no: str | None
    code: str | None
    description: str
    items_included: str | None = None
    uom: str | None
    breakdown: dict[str, float] = {}
    total_sqm: float | None
    total_sqft: float | None
    remarks: str | None = None
    measured: bool
    row: int


class QuantitySheet(BaseModel):
    filename: str
    sheet: str
    title: str | None
    lines: list[BoqLine]
    breakdown_columns: list[str]
    grand_total_sqm: float | None
    grand_total_sqft: float | None
    grand_total_row: int | None


# ── What the pipeline returns ──────────────────────────────────────────────────────────────────


class Check(BaseModel):
    id: str
    severity: Literal["block", "warn", "ok"]
    title: str
    detail: str
    source: str


class Attachment(BaseModel):
    filename: str
    content_type: str
    size: int
    used: bool


class ThreadMessage(BaseModel):
    header: str | None
    body: str


class ParsedEmail(BaseModel):
    subject: str | None
    sender: str | None
    date: str | None
    body: str
    thread: list[ThreadMessage]
    attachments: list[Attachment]


class DroppedField(BaseModel):
    field: str
    value: str | None
    quote: str | None
    reason: str


class IntakeResult(BaseModel):
    email: ParsedEmail
    requirement: ExtractedRequirement
    dropped_fields: list[DroppedField]
    quantities: QuantitySheet | None
    checks: list[Check]
    extraction_mode: Literal["live", "replay"]
    model: str | None
