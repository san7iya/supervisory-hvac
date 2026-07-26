"""Standalone LLM reasoning step. Given a summarized window of recent chunk
metrics, asks the local Ollama model to propose a setpoint adjustment via
native tool-calling. This module is advisory only -- it has no authority to
touch the IDF or apply anything. It returns either a structured Proposal or
a RejectedProposal; it is the caller's job (Milestone 5) to decide what to
do with either. Never wired into the orchestration loop yet.
"""
import json
from dataclasses import dataclass
from typing import Any, Union

import requests

OLLAMA_HOST = "http://localhost:11434"
MODEL = "qwen2.5:7b-instruct-q4_K_M"
REQUEST_TIMEOUT_S = 60

PROPOSE_SETPOINT_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_setpoint_adjustment",
        "description": (
            "Propose an adjustment to a zone thermostat setpoint schedule, based on "
            "recent energy and comfort trends. This is a suggestion only -- it will be "
            "validated and clamped before anything is applied."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["cooling_setpoint", "heating_setpoint"],
                    "description": "Which setpoint schedule to adjust.",
                },
                "value_celsius": {
                    "type": "number",
                    "description": "Proposed setpoint temperature in Celsius.",
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
            "required": ["field", "value_celsius", "window", "reasoning"],
        },
    },
}

_REQUIRED_FIELDS = {"field": str, "value_celsius": (int, float), "window": str, "reasoning": str}
_ALLOWED_FIELD_VALUES = {"cooling_setpoint", "heating_setpoint"}


@dataclass
class Proposal:
    field: str
    value_celsius: float
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
    return Proposal(
        field=args["field"],
        value_celsius=float(args["value_celsius"]),
        window=args["window"],
        reasoning=args["reasoning"],
    )


def propose_setpoint_adjustment(window_summary: str) -> Union[Proposal, RejectedProposal]:
    """Ask the local model to reason over window_summary and return a structured
    proposal via native tool-calling. Never regex-salvages JSON from prose output --
    if the model doesn't call the tool correctly, this returns a RejectedProposal
    with the reason, not a best-effort parse."""
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a supervisory HVAC setpoint advisor for a single small office "
                    "building. You will be given a summary of recent EnergyPlus simulation "
                    "chunks (energy use, and comfort/temperature data when available). "
                    "You MUST respond by calling the propose_setpoint_adjustment tool with "
                    "your recommendation. Do not respond in plain text."
                ),
            },
            {"role": "user", "content": window_summary},
        ],
        "tools": [PROPOSE_SETPOINT_TOOL],
        "stream": False,
    }

    try:
        resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return RejectedProposal(reason=f"request to Ollama failed: {e}", raw_response=None)

    tool_calls = data.get("message", {}).get("tool_calls") or []
    if not tool_calls:
        return RejectedProposal(
            reason="model did not return a tool call",
            raw_response=data.get("message", {}).get("content"),
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