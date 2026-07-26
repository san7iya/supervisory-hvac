"""
Milestone 7 proof: starts the real MCP server (mcp_server.py) via an
in-memory client/server session -- a genuine MCP protocol round-trip, not a
direct Python function call dressed up as one -- and confirms each tool's
result matches calling the underlying wrapped function directly, against
real data used throughout this build (committed evidence chunks for the
metrics tools, real captured proposal data for validate_proposal, a live
LLM call for propose_setpoint_adjustment).

Requires LLM_PROVIDER=groq (or a running local Ollama) for the
propose_setpoint_adjustment tool -- same requirement as every other script
in this build that calls the LLM.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from supervisory_hvac import llm_reasoning as direct_llm_reasoning  # noqa: E402
from supervisory_hvac import metrics as direct_metrics  # noqa: E402
from supervisory_hvac import validation as direct_validation  # noqa: E402
from supervisory_hvac.llm_reasoning import Proposal, RejectedProposal  # noqa: E402
from supervisory_hvac.mcp_server import mcp  # noqa: E402
from supervisory_hvac.validation import RejectedAction, ValidatedAction  # noqa: E402
from supervisory_hvac.window_summary import summarize_chunks  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "data" / "evidence" / "chunks_ai_supervised"
REAL_CHUNK_DIR = EVIDENCE_DIR / "chunk_02"  # real, has kwh + pmv + temp data

results = []


def check(label: str, mcp_value, direct_value):
    match = mcp_value == direct_value
    results.append(match)
    status = "PASS" if match else "FAIL"
    print(f"[{status}] {label}")
    print(f"    MCP tool result:    {mcp_value!r}")
    print(f"    direct call result: {direct_value!r}")


def unwrap(call_tool_result):
    """MCP tool results carry structured content when the tool has a typed
    return annotation. Falls back to parsing the text content block if
    structuredContent isn't present, so this doesn't silently pass by
    comparing the wrong thing."""
    if call_tool_result.structuredContent is not None:
        sc = call_tool_result.structuredContent
        # FastMCP wraps non-object returns (e.g. a bare float) as {"result": value}.
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    text = call_tool_result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def main():
    async with create_connected_server_and_client_session(mcp) as session:
        tools = await session.list_tools()
        print(f"[SERVER] {len(tools.tools)} tools registered: "
              f"{[t.name for t in tools.tools]}\n")

        # --- get_zone_temps ---------------------------------------------
        mcp_result = await session.call_tool("get_zone_temps", {"chunk_dir": str(REAL_CHUNK_DIR)})
        check("get_zone_temps (real chunk data)",
              unwrap(mcp_result), direct_metrics.average_zone_temp_c(REAL_CHUNK_DIR))

        # --- get_energy_kwh ------------------------------------------------
        mcp_result = await session.call_tool("get_energy_kwh", {"chunk_dir": str(REAL_CHUNK_DIR)})
        check("get_energy_kwh (real chunk data)",
              unwrap(mcp_result), direct_metrics.total_facility_kwh(REAL_CHUNK_DIR))

        # --- get_pmv_comfort -------------------------------------------
        mcp_result = await session.call_tool("get_pmv_comfort", {"chunk_dir": str(REAL_CHUNK_DIR)})
        check("get_pmv_comfort (real chunk data)",
              unwrap(mcp_result), direct_metrics.average_pmv(REAL_CHUNK_DIR))

        # --- propose_setpoint_adjustment (real LLM call, real window data) -
        real_chunk_dirs = [EVIDENCE_DIR / f"chunk_{i:02d}" for i in range(1, 4)]
        window_summary = summarize_chunks(real_chunk_dirs)
        print(f"\n[WINDOW_SUMMARY]\n{window_summary}\n")

        mcp_result = await session.call_tool("propose_setpoint_adjustment", {"window_summary": window_summary})
        mcp_value = unwrap(mcp_result)
        direct_result = direct_llm_reasoning.propose_setpoint_adjustment(window_summary)

        if isinstance(direct_result, Proposal):
            direct_value = {
                "status": "proposal", "field": direct_result.field,
                "direction": direct_result.direction, "magnitude": direct_result.magnitude,
                "window": direct_result.window, "reasoning": direct_result.reasoning,
            }
        else:
            assert isinstance(direct_result, RejectedProposal)
            direct_value = {"status": "rejected", "reason": direct_result.reason,
                             "raw_response": None if direct_result.raw_response is None
                             else str(direct_result.raw_response)}

        # Two separate live LLM calls (one via MCP, one direct) can't be
        # expected to return byte-identical reasoning text -- LLM output
        # isn't deterministic across calls (established in the Milestone 6
        # model-capability investigation). What's checked here is that both
        # calls produced the SAME KIND of structured result (same status,
        # same schema shape) via the same underlying function -- proving the
        # MCP wrapper didn't corrupt or reinterpret the call, not that two
        # independent LLM calls happened to agree word-for-word.
        shape_match = (
            mcp_value.get("status") == direct_value.get("status")
            and set(mcp_value.keys()) == set(direct_value.keys())
        )
        results.append(shape_match)
        status = "PASS" if shape_match else "FAIL"
        print(f"[{status}] propose_setpoint_adjustment (real LLM call, structural match -- "
              f"see note above on why this isn't a value-for-value diff)")
        print(f"    MCP tool result:    {mcp_value!r}")
        print(f"    direct call result: {direct_value!r}")

        # --- validate_proposal (real captured proposal data) ---------------
        # The real accepted chunk-1 proposal from the structural-fix run:
        # heating_setpoint, raise, medium, from baseline 21.11C -> 22.61C,
        # cooling untouched at 23.89C. Exact same inputs used throughout
        # Milestones 5-6's validation tests.
        real_args = {
            "field": "heating_setpoint", "direction": "raise", "magnitude": "medium",
            "window": "09:00-17:00", "reasoning": "real captured proposal from the structural-fix run",
            "current_setpoint_celsius": 21.11, "avg_pmv": -1.10,
            "other_field_current_celsius": 23.89, "field_baseline_celsius": 21.11,
        }
        mcp_result = await session.call_tool("validate_proposal", real_args)
        mcp_value = unwrap(mcp_result)

        direct_proposal = Proposal(field=real_args["field"], direction=real_args["direction"],
                                    magnitude=real_args["magnitude"], window=real_args["window"],
                                    reasoning=real_args["reasoning"])
        direct_result = direct_validation.validate_proposal(
            direct_proposal, real_args["current_setpoint_celsius"], real_args["avg_pmv"],
            real_args["other_field_current_celsius"], real_args["field_baseline_celsius"],
        )
        if isinstance(direct_result, ValidatedAction):
            direct_value = {"status": "accepted", "field": direct_result.field,
                             "value_celsius": direct_result.value_celsius, "window": direct_result.window,
                             "reasoning": direct_result.reasoning, "clamped": direct_result.clamped}
        else:
            assert isinstance(direct_result, RejectedAction)
            direct_value = {"status": "rejected", "reason": direct_result.reason}
        check("validate_proposal (real captured proposal -> expect ACCEPTED, 22.61C)",
              mcp_value, direct_value)

        # --- validate_proposal negative path: the real deadband-crash case -
        crash_args = dict(real_args, current_setpoint_celsius=22.61,
                           reasoning="real captured proposal that triggered the deadband check")
        mcp_result = await session.call_tool("validate_proposal", crash_args)
        mcp_value = unwrap(mcp_result)

        direct_proposal2 = Proposal(field=crash_args["field"], direction=crash_args["direction"],
                                     magnitude=crash_args["magnitude"], window=crash_args["window"],
                                     reasoning=crash_args["reasoning"])
        direct_result2 = direct_validation.validate_proposal(
            direct_proposal2, crash_args["current_setpoint_celsius"], crash_args["avg_pmv"],
            crash_args["other_field_current_celsius"], crash_args["field_baseline_celsius"],
        )
        assert isinstance(direct_result2, RejectedAction)
        direct_value2 = {"status": "rejected", "reason": direct_result2.reason}
        check("validate_proposal (real deadband-crash case -> expect REJECTED)",
              mcp_value, direct_value2)

    print(f"\n{'ALL' if all(results) else 'NOT ALL'} {len(results)} checks passed "
          f"({sum(results)}/{len(results)})")
    if not all(results):
        raise AssertionError("one or more MCP tool results did not match the direct function call")


if __name__ == "__main__":
    if "LLM_PROVIDER" not in os.environ:
        print("[WARN] LLM_PROVIDER not set -- defaulting to 'ollama'. If no local Ollama server "
              "is running, set LLM_PROVIDER=groq (with GROQ_API_KEY in .env) before running this.\n")
    asyncio.run(main())