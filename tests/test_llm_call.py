"""call_claude on Sonnet 5.5, offline against a fake client: request shape
(model, thinking setting), text read by block type, usage accounting,
refusal raised without retry, transient error retried once."""
from types import SimpleNamespace

import pytest

from markut import config
from markut.agents import llm


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(blocks, stop="end_turn", category=None):
    return SimpleNamespace(content=blocks, stop_reason=stop,
                           stop_details=SimpleNamespace(category=category) if category else None,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.fixture(autouse=True)
def fresh_tokens(monkeypatch):
    saved = dict(llm.TOKENS)
    llm.reset()
    monkeypatch.setattr(config, "THINKING_EFFORT", None)
    yield
    llm.TOKENS.update(saved)


def test_reads_text_block_not_first_block(monkeypatch):
    fake = FakeMessages([_response([SimpleNamespace(type="thinking", thinking=""),
                                    SimpleNamespace(type="text", text="the bull case")])])
    monkeypatch.setattr(llm, "client", SimpleNamespace(messages=fake))
    assert llm.call_claude("sys", "user", max_tokens=1000) == "the bull case"
    req = fake.calls[0]
    assert req["model"] == config.MODEL_NAME == "claude-sonnet-5-5"
    assert req["thinking"] == {"type": "between_tools"} and "output_config" not in req
    assert req["max_tokens"] == 1000 and req["system"] == "sys"
    assert llm.TOKENS == {"input": 10, "output": 5, "calls": 1, "cache_write": 0, "cache_read": 0}


def test_effort_setting_switches_to_adaptive_thinking(monkeypatch):
    monkeypatch.setattr(config, "THINKING_EFFORT", "low")
    fake = FakeMessages([_response([SimpleNamespace(type="text", text="ok")])])
    monkeypatch.setattr(llm, "client", SimpleNamespace(messages=fake))
    llm.call_claude("s", "u")
    assert fake.calls[0]["thinking"] == {"type": "adaptive"}
    assert fake.calls[0]["output_config"] == {"effort": "low"}


def test_refusal_raises_without_retry(monkeypatch):
    fake = FakeMessages([_response([], stop="refusal", category="general_harms")])
    monkeypatch.setattr(llm, "client", SimpleNamespace(messages=fake))
    with pytest.raises(llm.ModelRefusal, match="general_harms"):
        llm.call_claude("s", "u")
    assert len(fake.calls) == 1                      # no second attempt


def test_transient_error_retried_once(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    fake = FakeMessages([RuntimeError("blip"), _response([SimpleNamespace(type="text", text="fine")])])
    monkeypatch.setattr(llm, "client", SimpleNamespace(messages=fake))
    assert llm.call_claude("s", "u") == "fine"
    assert len(fake.calls) == 2 and llm.TOKENS["calls"] == 1
