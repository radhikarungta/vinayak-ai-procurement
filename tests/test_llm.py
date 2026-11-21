"""procurement/llm.py: key status and readable OpenAI errors. No network."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import openai
import pytest

from procurement import llm

REQ = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def status_error(cls, code: int):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


def test_ready_follows_the_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not llm.ready()
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert not llm.ready()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert llm.ready()


def test_status_labels(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    s = llm.status()
    assert s["ready"] is False and s["label"] == "Replay mode · no OPENAI_API_KEY" and "OPENAI_API_KEY" in s["message"]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = llm.status()
    assert s == {"ready": True, "model": llm.DEFAULT_MODEL, "label": f"Live · {llm.DEFAULT_MODEL}", "message": None}
    monkeypatch.setenv("OPENAI_MODEL", "some-other-model")
    assert llm.status()["label"] == "Live · some-other-model"


def test_client_refuses_without_a_key():
    with pytest.raises(llm.LLMError) as e:
        llm.client()
    assert e.value.status == 503 and "OPENAI_API_KEY" in str(e.value)


@pytest.mark.parametrize(
    ("err", "status", "text"),
    [
        (status_error(openai.AuthenticationError, 401), 503, "key was rejected"),
        (status_error(openai.PermissionDeniedError, 403), 502, "refused access to model"),
        (status_error(openai.NotFoundError, 404), 502, "doesn't know model"),
        (status_error(openai.RateLimitError, 429), 429, "Rate limited"),
        (status_error(openai.InternalServerError, 500), 502, "OpenAI API error 500"),
        (openai.APIConnectionError(request=REQ), 502, "Couldn't reach"),
        (openai.APITimeoutError(request=REQ), 502, "Couldn't reach"),
        (RuntimeError("x"), 502, "RuntimeError"),
    ],
)
def test_error_mapping(err, status, text):
    e = llm.describe(err)
    assert e.status == status and text in str(e)


def fake(resp=None, exc=None):
    calls = []

    def parse(**kw):
        calls.append(kw)
        if exc:
            raise exc
        return resp

    return SimpleNamespace(responses=SimpleNamespace(parse=parse)), calls


def test_parse_request_shape():
    parsed = object()
    c, calls = fake(SimpleNamespace(status="completed", output=[], output_parsed=parsed))
    assert llm.parse(c, text_format=dict, instructions="sys", user="hi", max_output_tokens=10, effort="low") is parsed
    kw = calls[0]
    assert kw["store"] is False and kw["text_format"] is dict and kw["instructions"] == "sys"
    assert kw["input"] == [{"role": "user", "content": "hi"}] and kw["reasoning"] == {"effort": "low"}


def test_parse_failures():
    c, _ = fake(exc=status_error(openai.RateLimitError, 429))
    with pytest.raises(llm.LLMError, match="Rate limited"):
        llm.parse(c, text_format=dict, instructions="", user="", max_output_tokens=1)
    refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="no")])
    c, _ = fake(SimpleNamespace(status="completed", output=[refusal], output_parsed=None))
    with pytest.raises(llm.Refused):
        llm.parse(c, text_format=dict, instructions="", user="", max_output_tokens=1)
    c, _ = fake(SimpleNamespace(status="incomplete", output=[], output_parsed=None))
    with pytest.raises(llm.LLMError, match="ran out of room"):
        llm.parse(c, text_format=dict, instructions="", user="", max_output_tokens=1)
    c, _ = fake(SimpleNamespace(status="completed", output=[], output_parsed=None))
    with pytest.raises(llm.LLMError, match="expected format"):
        llm.parse(c, text_format=dict, instructions="", user="", max_output_tokens=1)


def test_parse_retries_without_reasoning_when_the_model_rejects_it():
    calls = []
    ok = SimpleNamespace(status="completed", output=[], output_parsed="parsed")

    def fake(**kw):
        calls.append(kw)
        if "reasoning" in kw:
            raise openai.BadRequestError("Unsupported parameter: 'reasoning.effort' is not supported with this model.",
                                         response=httpx2.Response(400, request=REQ), body=None)
        return ok

    c = SimpleNamespace(responses=SimpleNamespace(parse=fake))
    assert llm.parse(c, text_format=dict, instructions="i", user="u", max_output_tokens=10, effort="low") == "parsed"
    assert len(calls) == 2 and calls[0]["reasoning"] == {"effort": "low"} and "reasoning" not in calls[1]

    def other(**kw):
        calls.append(kw)
        raise openai.BadRequestError("Invalid input.", response=httpx2.Response(400, request=REQ), body=None)

    calls.clear()
    with pytest.raises(llm.LLMError):
        llm.parse(SimpleNamespace(responses=SimpleNamespace(parse=other)), text_format=dict, instructions="i", user="u",
                  max_output_tokens=10, effort="low")
    assert len(calls) == 1
