"""Local Claude CLI bridge.

The LLM is reached only through `claude -p "<prompt>"` executed with subprocess.
No API keys, no SDKs, no conversation state: every call is one isolated prompt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Callable

import config

log = logging.getLogger(__name__)

MALFORMED_JSON_NOTICE = (
    "Malformed JSON detected. Return ONLY pure RFC 8259 JSON without any explanation "
    "or markdown formatting."
)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


class LLMError(RuntimeError):
    """The CLI could not be executed or never produced usable JSON."""


class JSONExtractionError(ValueError):
    """No valid JSON value could be isolated from the model output."""


def run_cli(prompt: str, timeout: int | None = None) -> str:
    """Execute the Claude CLI once and return its raw stdout."""
    timeout = timeout or config.LLM_TIMEOUT_SECONDS
    # npm installs a `claude.cmd` shim on Windows, which CreateProcess only finds by full path.
    binary = shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN
    cmd = [binary, "-p"]
    # On Windows the shim runs through cmd.exe, which cuts arguments at newlines and expands
    # %VARS%, and argv is capped at 32K chars, so prompts always travel through stdin there.
    use_stdin = os.name == "nt" or len(prompt) > config.LLM_ARGV_MAX_CHARS
    if not use_stdin:
        cmd.append(prompt)
    cmd.extend(config.CLAUDE_EXTRA_ARGS)
    try:
        proc = subprocess.run(
            cmd,
            # An empty stdin keeps the CLI from waiting on the server's inherited console.
            input=prompt if use_stdin else "",
            capture_output=True,
            text=True,
            encoding="utf-8",  # never the Windows ANSI code page (cp1254 breaks "₺")
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise LLMError(f"Claude CLI binary not found: {config.CLAUDE_BIN}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"Claude CLI timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise LLMError(f"Claude CLI exited with code {proc.returncode}: {detail}")
    return proc.stdout


def _candidates(text: str) -> list[str]:
    """Ordered list of substrings that may hold the JSON payload."""
    out: list[str] = []
    out.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))
    stripped = text.strip()
    out.append(stripped)
    obj = _OBJECT_RE.search(stripped)
    arr = _ARRAY_RE.search(stripped)
    # Prefer whichever bracket type starts first in the text.
    for m in sorted((m for m in (obj, arr) if m), key=lambda m: m.start()):
        out.append(m.group(0))
    return out


def _scan_first_value(text: str) -> Any:
    """Walk every '{' or '[' and return the first span that decodes as JSON.

    The greedy regex can over-capture when the model writes two JSON blocks or
    trailing prose containing a brace; raw_decode stops at the end of the first value.
    """
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    raise JSONExtractionError("No JSON object or array found in model output")


def extract_json(text: str) -> Any:
    """Isolate the first valid JSON object/array from noisy LLM output."""
    if not text or not text.strip():
        raise JSONExtractionError("Empty model output")
    for candidate in _candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    return _scan_first_value(text)


def call_json(
    prompt: str,
    validator: Callable[[Any], Any] | None = None,
    runner: Callable[[str], str] | None = None,
) -> Any:
    """Run a prompt and return parsed (and optionally validated) JSON.

    `validator` receives the decoded JSON and must return the final object or raise
    ValueError (pydantic.ValidationError is a ValueError). Malformed or invalid
    output triggers exactly LLM_JSON_RETRIES corrective retries.
    """
    runner = runner or run_cli
    current_prompt = prompt
    last_error: Exception | None = None
    for attempt in range(config.LLM_JSON_RETRIES + 1):
        raw = runner(current_prompt)
        try:
            data = extract_json(raw)
            return validator(data) if validator else data
        except ValueError as exc:
            last_error = exc
            log.warning("LLM output rejected on attempt %d: %s", attempt + 1, str(exc)[:200])
            current_prompt = f"{MALFORMED_JSON_NOTICE}\n\n{prompt}"
    raise LLMError(f"Model did not return valid JSON: {last_error}")
