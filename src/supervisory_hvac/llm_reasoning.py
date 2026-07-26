"""Standalone LLM reasoning step. Given a summarized window of recent chunk
metrics, asks an LLM to propose a setpoint adjustment via native tool-calling.
This module is advisory only -- it has no authority to touch the IDF or
apply anything. It returns either a structured Proposal or a
RejectedProposal; it is the caller's job (Milestone 5) to decide what to do
with either. Never wired into the orchestration loop yet.

Provider selection is a config flag (LLM_PROVIDER env var), per Section 2/8:
local Ollama is the default/primary path; Groq is a cloud fallback only,
switched to explicitly, never silently preferred over local. This exists
because Ollama's model pull can be blocked by slow/unreliable CDN throughput
independent of the local machine's general network quality -- see
docs/architecture-notes.md.
"""
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Union

import requests

from .env import load_dotenv

load_dotenv()

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")  # "ollama" (default) or "groq" (fallback)
REQUEST_TIMEOUT_S = 60

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:7b-instruct-q4_K_M"

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Default stays the small/fast model (Section 7's named alternative to
# Qwen2.5-7B-Instruct). Overridable via env var so a larger model can be
# compared without hardcoding a permanent swap -- see the Milestone 6
# model-capability-vs-approach investigation in docs/architecture-notes.md.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

PROPOSE_SETPOINT_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_setpoint_adjustment",
        "description": (
            "Propose an adjustment to a zone thermostat setpoint schedule, based on "
            "recent energy and comfort trends. This is a suggestion only -- it will be "
            "validated and clamped before anything is applied. You do not choose an exact "
            "temperature -- only a direction and a rough magnitude; the actual numeric "
            "change is computed deterministically in code from those two choices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["cooling_setpoint", "heating_setpoint"],
                    "description": "Which setpoint schedule to adjust.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["raise", "lower"],
                    "description": "Which way to move the setpoint.",
                },
                "magnitude": {
                    "type": "string",
                    "enum": ["small", "medium", "large"],
                    "description": "Roughly how big a change to make; mapped to a fixed "
                                    "degree-Celsius delta in code, not chosen by you as a number.",
                },
                "window": {
                    "type": "string",
                    "description": "Time-of-day window the change applies to, e.g. '13:00-17:00'.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentence justification for this proposal.",
                },
            },
            "required": ["field", "direction", "magnitude", "window", "reasoning"],
        },
    },
}

_REQUIRED_FIELDS = {"field": str, "direction": str, "magnitude": str, "window": str, "reasoning": str}
_ALLOWED_FIELD_VALUES = {"cooling_setpoint", "heating_setpoint"}
_ALLOWED_DIRECTIONS = {"raise", "lower"}
_ALLOWED_MAGNITUDES = {"small", "medium", "large"}


@dataclass
class Proposal:
    field: str
    direction: str
    magnitude: str
    window: str
    reasoning: str


@dataclass
class RejectedProposal:
    reason: str
    raw_response: Any


def _validate_arguments(args: dict) -> Proposal:
    """Raises ValueError with a specific reason on any schema violation."""
    for key, expected_type in _REQUIRED_FIELDS.items():
        if key not in args:
            raise ValueError(f"missing required field {key!r}")
        if not isinstance(args[key], expected_type):
            raise ValueError(f"field {key!r} has wrong type: {type(args[key]).__name__}")
    if args["field"] not in _ALLOWED_FIELD_VALUES:
        raise ValueError(f"field value {args['field']!r} not in {_ALLOWED_FIELD_VALUES}")
    if args["direction"] not in _ALLOWED_DIRECTIONS:
        raise ValueError(f"direction value {args['direction']!r} not in {_ALLOWED_DIRECTIONS}")
    if args["magnitude"] not in _ALLOWED_MAGNITUDES:
        raise ValueError(f"magnitude value {args['magnitude']!r} not in {_ALLOWED_MAGNITUDES}")
    return Proposal(
        field=args["field"],
        direction=args["direction"],
        magnitude=args["magnitude"],
        window=args["window"],
        reasoning=args["reasoning"],
    )


def _chat_ollama(messages: list) -> dict:
    """Returns the response's message dict, or raises requests.RequestException."""
    payload = {"model": OLLAMA_MODEL, "messages": messages, "tools": [PROPOSE_SETPOINT_TOOL], "stream": False}
    resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json().get("message", {})


def _chat_groq(messages: list) -> dict:
    """Returns the response's message dict (OpenAI-compatible shape), or raises
    requests.RequestException / RuntimeError if GROQ_API_KEY isn't set.
    Prints latency and token usage -- side-effect logging only, for the
    model-comparison investigation; doesn't change the return shape."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set (checked process env and .env)")
    payload = {"model": GROQ_MODEL, "messages": messages, "tools": [PROPOSE_SETPOINT_TOOL]}
    start = time.time()
    resp = requests.post(
        GROQ_API_URL, json=payload, timeout=REQUEST_TIMEOUT_S,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    elapsed = time.time() - start
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage", {})
    print(f"[GROQ_CALL] model={GROQ_MODEL} elapsed={elapsed:.2f}s "
          f"prompt_tokens={usage.get('prompt_tokens')} completion_tokens={usage.get('completion_tokens')}")
    return data["choices"][0].get("message", {})


def propose_setpoint_adjustment(window_summary: str) -> Union[Proposal, RejectedProposal]:
    """Ask the configured provider (LLM_PROVIDER) to reason over window_summary
    and return a structured proposal via native tool-calling. Never
    regex-salvages JSON from prose output -- if the model doesn't call the
    tool correctly, this returns a RejectedProposal with the reason, not a
    best-effort parse."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a supervisory HVAC setpoint advisor for a single small office "
                "building. You will be given a summary of recent EnergyPlus simulation "
                "chunks (energy use, and comfort/temperature data when available). "
                "You MUST respond by calling the propose_setpoint_adjustment tool with "
                "your recommendation. Do not respond in plain text.\n\n"
                "PMV sign convention (Fanger scale): negative PMV means the zone is TOO "
                "COLD; positive PMV means the zone is TOO WARM. The corrective direction "
                "is: if PMV is negative (too cold), RAISE the heating setpoint; if PMV is "
                "positive (too warm), LOWER the cooling setpoint (or raise it less than "
                "before). Do not confuse a more negative PMV with improved comfort -- it "
                "means the opposite: colder and further from neutral (0).\n\n"
                "Worked example (illustrative only, not real data): current heating "
                "setpoint is 21.0C, avg_pmv is -1.0 (too cold). Correct proposal: "
                "direction=\"raise\", e.g. magnitude=\"medium\". Incorrect: "
                "direction=\"lower\" -- that would make an already-too-cold zone colder.\n\n"
                "You do not choose an exact temperature. You choose direction (raise/lower) "
                "and magnitude (small/medium/large); the actual degree-Celsius change is "
                "computed deterministically in code from those two choices, not from a "
                "number you write. Before calling the tool, make sure the direction you "
                "select actually matches the corrective direction your own reasoning "
                "describes."
            ),
        },
        {"role": "user", "content": window_summary},
    ]

    try:
        if LLM_PROVIDER == "groq":
            message = _chat_groq(messages)
        else:
            message = _chat_ollama(messages)
    except (requests.RequestException, RuntimeError) as e:
        return RejectedProposal(reason=f"request to {LLM_PROVIDER} failed: {e}", raw_response=None)

    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return RejectedProposal(
            reason="model did not return a tool call",
            raw_response=message.get("content"),
        )
    if len(tool_calls) > 1:
        return RejectedProposal(
            reason=f"model returned {len(tool_calls)} tool calls, expected exactly 1",
            raw_response=tool_calls,
        )

    call = tool_calls[0].get("function", {})
    if call.get("name") != "propose_setpoint_adjustment":
        return RejectedProposal(
            reason=f"unexpected tool name: {call.get('name')!r}",
            raw_response=tool_calls,
        )

    args = call.get("arguments")
    if isinstance(args, str):
        # Some Ollama models emit arguments as a JSON string rather than a dict.
        # This is still the tool-call channel, not prose -- not a regex salvage.
        try:
            args = json.loads(args)
        except json.JSONDecodeError as e:
            return RejectedProposal(reason=f"arguments not valid JSON: {e}", raw_response=args)

    if not isinstance(args, dict):
        return RejectedProposal(reason=f"arguments not an object: {type(args).__name__}", raw_response=args)

    try:
        return _validate_arguments(args)
    except ValueError as e:
        return RejectedProposal(reason=str(e), raw_response=args)