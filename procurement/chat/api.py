"""Ask endpoints. Included by procurement/api.py.

POST /api/chat        {messages: [{role, content}], page?: {hash, uid}} -> {reply, tool_calls, model, rounds}
GET  /api/chat/status {ready, message, model}

Stateless: the browser keeps the history and resends the last turns. The model (OpenAI Responses
API) answers only from the read-only tools in tools.py, which run in-process.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Literal

import openai
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import llm
from .. import mailbox  # noqa: F401  (loads .env, as the rest of the app does)
from . import tools as T

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = logging.getLogger(__name__)

DEFAULT_MODEL = llm.DEFAULT_MODEL
MAX_ROUNDS = 8  # tool rounds per question
MAX_TURNS = 20  # history turns sent to the model
MAX_OUTPUT_TOKENS = 6000
NO_KEY = "Chat needs OPENAI_API_KEY in .env. Add it and restart the server."

SYSTEM = """You are "Ask", the assistant inside Vinayak's procurement app, used by the contracts & planning team of a residential real-estate developer in Kolkata, India.

The app follows each site requirement email through: item codes (match each line to the ERP item master; ask the site when unclear) -> approval -> vendor shortlist -> RFQs -> quotes and the comparative statement -> award and the HOD note.

How to answer:
- Answer only from the tools' data. Call the tools you need before answering; don't answer from memory or general trade knowledge.
- If the data isn't there, say so plainly and say which tool or screen you checked. Never invent rates, quantities, dates, vendor details or item codes.
- Cite where a fact comes from: requirement ids (e.g. "requirement 1010"), ERP item codes and sheet rows (e.g. "WP.02, row 48"), quotation file names and page numbers.
- Keep answers short: a sentence or two, then a list or a markdown table when comparing several things. Use **bold** sparingly.
- Money: rupees with Indian grouping (₹18,75,112) or in lakhs (₹18.75 lakh). Say whether a rate is before GST when the data says so.
- Dates: give the date as written (e.g. 16 Nov 2026) and, when asked "since when", how many days ago relative to today's date given in the message.
- "Waiting on the site" means a reply went to the site and the questions are still open; its start is the last reply's date.
- The user may be looking at a page; the message says which. "This requirement" means the one on that page.
- You can only read. If asked to send, approve or change something, say which screen in the app does it."""


class ChatError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def model() -> str:
    return llm.model()


def has_key() -> bool:
    return llm.ready()


def _client() -> openai.OpenAI:
    if not has_key():
        raise ChatError(NO_KEY, 503)
    return llm.client()


# ── Request shape ──────────────────────────────────────────────────────────────────────────────


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class Page(BaseModel):
    hash: str | None = Field(None, max_length=200)
    uid: str | None = Field(None, max_length=40)


class ChatIn(BaseModel):
    messages: list[Turn] = Field(min_length=1)
    page: Page | None = None


def _context(page: Page | None) -> str:
    bits = [f"Today is {date.today():%d %b %Y}."]
    if page and page.uid:
        bits.append(f"The user is looking at requirement {page.uid}.")
    elif page and page.hash:
        bits.append(f"The user is on the {page.hash.lstrip('#') or 'overview'} page.")
    return "[" + " ".join(bits) + "]"


def build_input(messages: list[Turn], page: Page | None) -> list[dict]:
    turns = [m for m in messages if m.content.strip()][-MAX_TURNS:]
    while turns and turns[0].role != "user":
        turns.pop(0)
    if not turns or turns[-1].role != "user":
        raise ChatError("The last message should be a question.", 400)
    out = [{"role": m.role, "content": m.content} for m in turns]
    out[-1]["content"] = f"{_context(page)}\n\n{out[-1]['content']}"
    return out


# ── The loop ───────────────────────────────────────────────────────────────────────────────────


def _item(o: Any) -> dict:
    """An output item, as an input item for the next request."""
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "model_dump"):
        return o.model_dump(exclude_none=True)
    return dict(o)


_refusal = llm.refusal


def _create(client: openai.OpenAI, items: list, final: bool) -> Any:
    try:
        return client.responses.create(
            model=model(),
            instructions=SYSTEM,
            input=items,
            tools=T.openai_tools(),
            tool_choice="none" if final else "auto",
            max_output_tokens=MAX_OUTPUT_TOKENS,
            # Client data: don't keep it on OpenAI's side. Reasoning is passed back encrypted instead.
            store=False,
            include=["reasoning.encrypted_content"],
        )
    except openai.OpenAIError as e:
        err = llm.describe(e)
        raise ChatError(str(err), err.status) from e


def answer(body: ChatIn, client: Any = None) -> dict:
    items: list = build_input(body.messages, body.page)
    client = client or _client()
    calls: list[dict] = []
    for rnd in range(MAX_ROUNDS + 1):
        final = rnd == MAX_ROUNDS  # out of tool rounds: ask for an answer from what was read
        resp = _create(client, items, final)
        if (why := _refusal(resp)) is not None:
            log.info("Chat refusal: %s", why)
            return {"reply": "Sorry, I can't help with that one. Try asking about requirements, item codes, vendors or quotes.",
                    "tool_calls": calls, "model": model(), "rounds": rnd, "refused": True}
        fn_calls = [o for o in resp.output or [] if getattr(o, "type", None) == "function_call"]
        if not fn_calls or final:
            text = (getattr(resp, "output_text", "") or "").strip()
            if getattr(resp, "status", None) == "incomplete":
                text = (text + "\n\n" if text else "") + "_(The answer was cut short. Ask a narrower question.)_"
            if not text:
                text = ("I read a lot of data but couldn't finish an answer. Try a narrower question, e.g. one requirement or one vendor."
                        if final else "I don't have an answer for that.")
            return {"reply": text, "tool_calls": calls, "model": model(), "rounds": rnd}
        items += [_item(o) for o in resp.output]
        for c in fn_calls:
            try:
                args = json.loads(c.arguments or "{}")
            except json.JSONDecodeError:
                out, err, args = json.dumps({"error": "The tool arguments weren't valid JSON."}), True, {}
            else:
                out, err = T.run(c.name, args if isinstance(args, dict) else {})
            calls.append({"name": c.name, "input_summary": T.summary(c.name, args if isinstance(args, dict) else {}), "error": err})
            items.append({"type": "function_call_output", "call_id": c.call_id, "output": out})
    raise AssertionError("unreachable")


# ── Endpoints ──────────────────────────────────────────────────────────────────────────────────


@router.get("/status")
def status() -> dict:
    if not has_key():
        return {"ready": False, "message": NO_KEY, "model": model()}
    return {"ready": True, "message": None, "model": model()}


@router.post("")
def chat(body: ChatIn) -> dict:
    try:
        return answer(body)
    except ChatError as e:
        raise HTTPException(e.status, str(e)) from e
