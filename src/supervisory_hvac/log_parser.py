"""Parses run_ai_supervised_loop.py's structured stdout log (Section 6 format)
into per-chunk decision records. Exists so the dashboard can read a real,
program-written log file directly instead of a hand-transcribed one -- see
docs/architecture-notes.md and data/decision_log_70b.json's _source note for
why this module exists (the original run's stdout was never persisted to a
file, only displayed in a terminal/chat transcript)."""
import re
from pathlib import Path

_CHUNK_START = re.compile(r"\[CHUNK_START\] chunk=(\d+) window=(\S+)")
_CHUNK_RESULT = re.compile(r"\[CHUNK_RESULT\] chunk=\d+ kwh=([\d.]+) cumulative_kwh=[\d.]+ avg_pmv=([+-]?[\d.]+)")
_LLM_REASONING = re.compile(r"\[LLM_REASONING\] '(.*)'")
_PROPOSED_ACTION = re.compile(
    r"\[PROPOSED_ACTION\] field=(\S+) direction=(\S+) magnitude=(\S+) window='([^']*)'"
)
_VALIDATION_PASS = re.compile(r"\[VALIDATION\] PASS clamped=(\S+)")
_VALIDATION_REJECT = re.compile(
    r"\[VALIDATION\] REJECT(?:\s+\(([\w-]+)\))?\s+reason=(.+?) -- keeping last valid schedule"
)

# failure_type tags llm_reasoning.py can emit directly (see its FAILURE_* constants).
# When the captured tag is one of these, trust it over the reason-text heuristic below --
# it's an exact classification, not a guess. Older logs (e.g. data/run_ai_supervised_70b.log)
# predate this and always wrote the literal tag "schema" regardless of true cause, so those
# still fall through to _classify_rejection() for backward-compatible parsing.
_KNOWN_FAILURE_TYPES = {"transient_network", "non_transient_4xx", "tool_call_malformed", "schema_invalid"}
_APPLIED_ACTION = re.compile(r"\[APPLIED_ACTION\] \S+ -> ([\d.]+)C")


def _classify_rejection(reason: str) -> str:
    if "deadband" in reason:
        return "deadband"
    if "cumulative movement cap" in reason:
        return "cumulative_cap"
    if "directional-consistency" in reason:
        return "direction"
    if "request to" in reason or "did not return a tool call" in reason or "no PMV data" in reason:
        return "schema_or_network"
    return "unknown"


def parse_decision_log(log_path) -> list:
    """Returns one dict per chunk found in the log, in order. Each has at
    least: chunk, window, kwh, avg_pmv, status ("accepted"/"rejected"/None
    if no proposal was ever attempted for that chunk). Accepted/rejected
    chunks additionally carry field/direction/magnitude/proposal_window/
    reasoning/reason/(applied_value_c or check_that_decided)."""
    text = Path(log_path).read_text()
    chunks = []
    current = None

    for line in text.splitlines():
        m = _CHUNK_START.search(line)
        if m:
            if current is not None:
                chunks.append(current)
            current = {"chunk": int(m.group(1)), "window": m.group(2), "status": None}
            continue
        if current is None:
            continue

        m = _CHUNK_RESULT.search(line)
        if m:
            current["kwh"] = float(m.group(1))
            current["avg_pmv"] = float(m.group(2))
            continue

        m = _LLM_REASONING.search(line)
        if m:
            current["reasoning"] = m.group(1)
            continue

        m = _PROPOSED_ACTION.search(line)
        if m:
            current["field"] = m.group(1)
            current["direction"] = m.group(2)
            current["magnitude"] = m.group(3)
            current["proposal_window"] = m.group(4)
            continue

        m = _VALIDATION_PASS.search(line)
        if m:
            current["status"] = "accepted"
            current["clamped"] = m.group(1) == "True"
            continue

        m = _VALIDATION_REJECT.search(line)
        if m:
            current["status"] = "rejected"
            reason = m.group(2).strip().strip("'\"")
            current["reason"] = reason
            tag = m.group(1)
            current["check_that_decided"] = tag if tag in _KNOWN_FAILURE_TYPES else _classify_rejection(reason)
            continue

        m = _APPLIED_ACTION.search(line)
        if m:
            current["applied_value_c"] = float(m.group(1))
            continue

    if current is not None:
        chunks.append(current)
    return chunks