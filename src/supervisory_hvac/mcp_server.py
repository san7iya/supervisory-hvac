"""MCP server wrapping already-tested tool functions -- per Section 2/8/10's
design, this is a thin protocol layer over proven logic, not new reasoning or
validation code. Every tool body is a direct call into an existing,
already-verified function; nothing here reimplements chunker.py,
eplus_runner.py, idf_edit.py, validation.py, or llm_reasoning.py internals.

Not wired into run_ai_supervised_loop.py -- that's a separate integration
decision, made after this wrapper is proven in isolation (see
scripts/test_mcp_server.py), consistent with how every other module in this
build was tested before integration.
"""
from pathlib import Path
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP

from . import llm_reasoning as _llm_reasoning
from . import metrics as _metrics
from . import validation as _validation
from .llm_reasoning import Proposal, RejectedProposal
from .validation import RejectedAction, ValidatedAction

mcp = FastMCP("supervisory-hvac")


@mcp.tool()
def get_zone_temps(chunk_dir: str) -> Optional[float]:
    """Average occupied-zone air temperature (Celsius) for a completed
    chunk's EnergyPlus output directory. Wraps metrics.average_zone_temp_c()
    unchanged -- returns None if the chunk has no temperature data."""
    return _metrics.average_zone_temp_c(Path(chunk_dir))


@mcp.tool()
def get_energy_kwh(chunk_dir: str) -> float:
    """Total facility electricity (kWh) for a completed chunk's EnergyPlus
    output directory. Wraps metrics.total_facility_kwh() unchanged."""
    return _metrics.total_facility_kwh(Path(chunk_dir))


@mcp.tool()
def get_pmv_comfort(chunk_dir: str) -> Optional[float]:
    """Average occupied-zone Fanger PMV for a completed chunk's EnergyPlus
    output directory. Wraps metrics.average_pmv() unchanged -- returns None
    if the chunk has no PMV data."""
    return _metrics.average_pmv(Path(chunk_dir))


@mcp.tool()
def propose_setpoint_adjustment(window_summary: str) -> dict:
    """Asks the configured LLM provider (LLM_PROVIDER env var) to propose a
    setpoint adjustment for the given window-summary text. Wraps
    llm_reasoning.propose_setpoint_adjustment() unchanged -- same provider
    selection, same schema validation, same never-regex-salvage-prose
    behavior. MCP tools can only return JSON-serializable data, so the
    Proposal/RejectedProposal dataclass is flattened into a dict here rather
    than the dataclass itself -- that flattening is the only new code; the
    reasoning call underneath is untouched.

    Returns {"status": "proposal", field, direction, magnitude, window,
    reasoning} or {"status": "rejected", reason, raw_response}."""
    result = _llm_reasoning.propose_setpoint_adjustment(window_summary)
    if isinstance(result, Proposal):
        return {
            "status": "proposal",
            "field": result.field,
            "direction": result.direction,
            "magnitude": result.magnitude,
            "window": result.window,
            "reasoning": result.reasoning,
        }
    assert isinstance(result, RejectedProposal)
    return {
        "status": "rejected",
        "reason": result.reason,
        "raw_response": None if result.raw_response is None else str(result.raw_response),
    }


@mcp.tool()
def validate_proposal(
    field: str,
    direction: str,
    magnitude: str,
    window: str,
    reasoning: str,
    current_setpoint_celsius: float,
    avg_pmv: float,
    other_field_current_celsius: float,
    field_baseline_celsius: float,
) -> dict:
    """Validates a setpoint proposal against the 4 guardrails (range clamp,
    directional-consistency, deadband, cumulative-movement cap). Wraps
    validation.validate_proposal() unchanged. MCP tool arguments must be
    JSON-primitive types, so this takes the proposal's fields as plain
    scalars and reconstructs a Proposal object internally before calling
    through -- no validation logic lives in this function.

    Returns {"status": "accepted", field, value_celsius, window, reasoning,
    clamped} or {"status": "rejected", reason}."""
    proposal = Proposal(field=field, direction=direction, magnitude=magnitude,
                         window=window, reasoning=reasoning)
    result: Union[ValidatedAction, RejectedAction] = _validation.validate_proposal(
        proposal, current_setpoint_celsius, avg_pmv,
        other_field_current_celsius, field_baseline_celsius,
    )
    if isinstance(result, ValidatedAction):
        return {
            "status": "accepted",
            "field": result.field,
            "value_celsius": result.value_celsius,
            "window": result.window,
            "reasoning": result.reasoning,
            "clamped": result.clamped,
        }
    assert isinstance(result, RejectedAction)
    return {"status": "rejected", "reason": result.reason}


if __name__ == "__main__":
    mcp.run()