"""OpenAI, in one place: the client, the model name, whether a key is set, and readable errors.

Every model call in the app (email intake, item-code picks, the Ask assistant) goes through here.
Config comes from the repo-root .env (never committed): OPENAI_API_KEY, and optionally OPENAI_MODEL.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_MODEL = "gpt-6.2-sol"
MODEL = os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
NO_KEY = "OPENAI_API_KEY isn't set. Add it to .env and restart the server."


class LLMError(RuntimeError):
    """A model call failed. The message is written for the person using the app; `status` is an HTTP code."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class Refused(LLMError):
    """The model declined to answer (a refusal, not an error)."""


def model() -> str:
    """The model to use, read live so a changed OPENAI_MODEL applies without a re-import."""
    return os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL


def ready() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def status() -> dict:
    """For status endpoints and the app bar."""
    m = model()
    if ready():
        return {"ready": True, "model": m, "label": f"Live · {m}", "message": None}
    return {"ready": False, "model": m, "label": "Replay mode · no OPENAI_API_KEY", "message": NO_KEY}


def client() -> openai.OpenAI:
    if not ready():
        raise LLMError(NO_KEY, 503)
    return openai.OpenAI()


def describe(e: Exception) -> LLMError:
    """Turn an OpenAI SDK error into a short message that says what to do about it."""
    m = model()
    if isinstance(e, openai.AuthenticationError):
        return LLMError("The OpenAI API key was rejected. Check OPENAI_API_KEY in .env.", 503)
    if isinstance(e, openai.PermissionDeniedError):
        return LLMError(f"OpenAI refused access to model {m}. Set OPENAI_MODEL in .env to a model this key can use.", 502)
    if isinstance(e, openai.NotFoundError):
        return LLMError(f"OpenAI doesn't know model {m}. Set OPENAI_MODEL in .env.", 502)
    if isinstance(e, openai.RateLimitError):
        return LLMError("Rate limited by the OpenAI API (or out of credit). Try again in a minute.", 429)
    if isinstance(e, openai.APITimeoutError | openai.APIConnectionError):
        return LLMError("Couldn't reach the OpenAI API. Check the network.", 502)
    if isinstance(e, openai.APIStatusError):
        return LLMError(f"OpenAI API error {e.status_code}: {e.message}", 502)
    return LLMError(f"The OpenAI call failed ({type(e).__name__}).", 502)


def refusal(resp: Any) -> str | None:
    """The refusal text if the model declined, else None."""
    for o in getattr(resp, "output", None) or []:
        if getattr(o, "type", None) == "message":
            for c in getattr(o, "content", None) or []:
                if getattr(c, "type", None) == "refusal":
                    return getattr(c, "refusal", None) or "refused"
    return None


def parse(c: Any, *, text_format: type, instructions: str, user: str, max_output_tokens: int, effort: str | None = None) -> Any:
    """One structured-output call (Responses API, `responses.parse` with a pydantic `text_format`).

    The API holds the answer to the model's JSON schema (strict mode) and the SDK validates it into
    `text_format`. Nothing is stored on OpenAI's side (`store=False`): these are client emails.
    Returns the parsed model, or raises LLMError with a readable message.
    """
    kwargs: dict[str, Any] = {}
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    def call(**kw: Any) -> Any:
        return c.responses.parse(model=model(), instructions=instructions, input=[{"role": "user", "content": user}],
                                 text_format=text_format, max_output_tokens=max_output_tokens, store=False, **kw)

    try:
        try:
            resp = call(**kwargs)
        except openai.BadRequestError as e:  # a non-reasoning model (e.g. gpt-4o) rejects `reasoning`: once more without it
            if "reasoning" not in kwargs or not any(w in str(e).lower() for w in ("reasoning", "effort", "unsupported parameter")):
                raise
            resp = call()
    except openai.OpenAIError as e:
        raise describe(e) from e
    except ValueError as e:  # pydantic.ValidationError: the text wasn't valid JSON for the schema (e.g. cut short)
        raise LLMError("The model's answer didn't match the expected format.", 502) from e
    if refusal(resp) is not None:
        raise Refused("The model declined to answer.", 502)
    if getattr(resp, "status", None) == "incomplete":
        raise LLMError("The model ran out of room before finishing.", 502)
    parsed = getattr(resp, "output_parsed", None)
    if parsed is None:
        raise LLMError("The model's answer didn't match the expected format.", 502)
    return parsed
