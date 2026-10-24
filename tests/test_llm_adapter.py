import subprocess

import pytest

import config
import llm_adapter
from llm_adapter import LLMError, extract_json

MESSY_OUTPUTS = [
    ('Sure! Here is the JSON you asked for:\n```json\n{"a": 1, "b": [1, 2]}\n```\nLet me know!', {"a": 1, "b": [1, 2]}),
    ('```\n{"a": 1}\n```', {"a": 1}),
    ('{"a": 1}', {"a": 1}),
    ('Result: {"a": {"nested": "}"}} and that is {all}.', {"a": {"nested": "}"}}),
    ('First {"a": 1} then a second {"b": 2}', {"a": 1}),
    ("Here you go [1, 2, 3] done", [1, 2, 3]),
    ('Noise { not json } then {"ok": true}', {"ok": True}),
]


@pytest.mark.parametrize("raw,expected", MESSY_OUTPUTS)
def test_extract_json_from_messy_output(raw, expected):
    assert extract_json(raw) == expected


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("I could not find any product information.")


def test_call_json_retries_once_with_corrective_notice():
    prompts = []
    outputs = iter(["Sorry, here it is: {broken", '{"fixed": true}'])

    def runner(prompt):
        prompts.append(prompt)
        return next(outputs)

    assert llm_adapter.call_json("ORIGINAL", runner=runner) == {"fixed": True}
    assert len(prompts) == 2
    assert prompts[1].startswith(llm_adapter.MALFORMED_JSON_NOTICE)
    assert prompts[1].endswith("ORIGINAL")


def test_call_json_gives_up_after_one_retry():
    calls = []

    def runner(prompt):
        calls.append(prompt)
        return "no json here"

    with pytest.raises(LLMError):
        llm_adapter.call_json("P", runner=runner)
    assert len(calls) == 1 + config.LLM_JSON_RETRIES


def test_validator_failure_triggers_retry():
    outputs = iter(['{"x": "bad"}', '{"x": 5}'])

    def validator(data):
        if not isinstance(data["x"], int):
            raise ValueError("x must be int")
        return data["x"]

    assert llm_adapter.call_json("P", validator=validator, runner=lambda p: next(outputs)) == 5


def test_run_cli_invokes_claude_with_prompt_argument(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": 1}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert llm_adapter.run_cli("hello") == '{"ok": 1}'
    assert captured["cmd"][:3] == [config.CLAUDE_BIN, "-p", "hello"]
    assert captured["kwargs"]["timeout"] == config.LLM_TIMEOUT_SECONDS
    assert captured["kwargs"]["capture_output"] is True


def test_run_cli_pipes_long_prompts_through_stdin(monkeypatch):
    captured = {}
    monkeypatch.setattr(config, "LLM_ARGV_MAX_CHARS", 10)

    def fake_run(cmd, **kwargs):
        captured.update(cmd=cmd, input=kwargs["input"])
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    llm_adapter.run_cli("x" * 50)
    assert captured["cmd"] == [config.CLAUDE_BIN, "-p"] + config.CLAUDE_EXTRA_ARGS
    assert captured["input"] == "x" * 50


def test_run_cli_errors(monkeypatch):
    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 90)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(LLMError, match="timed out"):
        llm_adapter.run_cli("p")

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "not logged in"))
    with pytest.raises(LLMError, match="not logged in"):
        llm_adapter.run_cli("p")

    monkeypatch.undo()  # real subprocess.run against a binary that does not exist
    monkeypatch.setattr(config, "CLAUDE_BIN", "/nonexistent/claude-binary")
    with pytest.raises(LLMError, match="not found"):
        llm_adapter.run_cli("p")
