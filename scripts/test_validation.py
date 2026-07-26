"""
Milestone 5 proof: validation layer tested against the REAL failure case
found in Milestone 4 (docs/architecture-notes.md), not a hypothetical one.

The three Proposal objects below are replayed verbatim from Milestone 4's
live Groq test run (scripts/test_llm_reasoning.py) -- same field, value,
window, and reasoning text the model actually returned. current setpoint
and avg PMV are read live from the real project data (baseline.idf and
Milestone 3's actual chunk_05 output), not hand-typed.

A couple of clearly-labeled synthetic cases are added after the real ones,
since all three real proposals turned out to be rejects -- worth also
proving the accept and clamp paths work, which the real data didn't
happen to exercise.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eppy.modeleditor import IDF  # noqa: E402

from supervisory_hvac.llm_reasoning import Proposal  # noqa: E402
from supervisory_hvac.metrics import average_pmv  # noqa: E402
from supervisory_hvac.validation import RejectedAction, ValidatedAction, validate_proposal  # noqa: E402

EPLUS_DIR = Path(os.environ.get("EPLUS_DIR", "D:/EnergyPlusV25-1-0"))
RUN_DIR = Path(os.environ.get("RUN_DIR", "D:/Honeywell-env/run1"))
IDD_PATH = EPLUS_DIR / "Energy+.idd"
BASELINE_IDF = RUN_DIR / "baseline.idf"
CHUNKS_DIR = RUN_DIR / "chunks"


def get_current_heating_setpoint_c() -> float:
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(BASELINE_IDF))
    sched = idf.getobject("Schedule:Compact", "HTGSETP_SCH_NO_OPTIMUM")
    return float(sched.Field_6)  # weekday 7:00 occupied setpoint -- see Milestone 2 notes


# Verbatim from the live Groq run in Milestone 4 -- see docs/architecture-notes.md.
REAL_PROPOSALS_FROM_MILESTONE_4 = [
    Proposal(
        field="heating_setpoint", value_celsius=20.0, window="",
        reasoning="Adjust heating setpoint to reduce energy consumption and increase comfort "
                  "based on recent simulated data showing low PMV values and relatively cool "
                  "occupied-zone temperatures",
    ),
    Proposal(
        field="heating_setpoint", value_celsius=18.3, window="",
        reasoning="The office has experienced cooler than normal recent temperatures and "
                  "higher energy consumption suggesting a potential heating setback.",
    ),
    Proposal(
        field="heating_setpoint", value_celsius=17.9, window="N/A",
        reasoning="The energy used has decreased by 59.8% and the comfort level has increased "
                  "with a more negative PMV value in the recent simulated chunks. This indicates "
                  "a potential opportunity for reducing the heating setpoint without impacting "
                  "occupancy comfort, suggesting a temperature decrease.",
    ),
]

# Synthetic, clearly labeled -- to exercise the accept and clamp paths.
SYNTHETIC_PROPOSALS = [
    Proposal(
        field="heating_setpoint", value_celsius=22.0, window="07:00-19:00",
        reasoning="[SYNTHETIC] Correct-direction case: zone is too cold, raising the setpoint "
                  "is the corrective move. Should be ACCEPTED.",
    ),
    Proposal(
        field="heating_setpoint", value_celsius=30.0, window="07:00-19:00",
        reasoning="[SYNTHETIC] Out-of-range case with a neutral comfort reading (no directional "
                  "constraint applies). Should be ACCEPTED but CLAMPED to 26.0C.",
    ),
]


def run_case(label: str, proposal: Proposal, current_setpoint: float, avg_pmv: float) -> None:
    result = validate_proposal(proposal, current_setpoint, avg_pmv)
    print(f"--- {label}: {proposal.field} -> {proposal.value_celsius}C "
          f"(current={current_setpoint:.2f}C, avg_pmv={avg_pmv:+.2f}) ---")
    print(f"  model's stated reasoning: {proposal.reasoning!r}")
    if isinstance(result, RejectedAction):
        print(f"  [REJECTED] {result.reason}")
    elif isinstance(result, ValidatedAction):
        print(f"  [ACCEPTED] value={result.value_celsius:.2f}C clamped={result.clamped}")
    else:
        raise AssertionError(f"unexpected result type: {type(result)}")
    print()


def main() -> None:
    current_setpoint = get_current_heating_setpoint_c()
    last_chunk_pmv = average_pmv(CHUNKS_DIR / "chunk_05")
    print(f"current heating setpoint (live from baseline.idf): {current_setpoint:.2f}C")
    print(f"most recent observed avg PMV (live from chunk_05): {last_chunk_pmv:+.2f}\n")

    print("=== Real proposals from Milestone 4's live Groq run ===\n")
    rejected_count = 0
    for i, proposal in enumerate(REAL_PROPOSALS_FROM_MILESTONE_4, start=1):
        result = validate_proposal(proposal, current_setpoint, last_chunk_pmv)
        if isinstance(result, RejectedAction):
            rejected_count += 1
        run_case(f"real proposal {i}", proposal, current_setpoint, last_chunk_pmv)

    assert rejected_count == 3, (
        f"expected all 3 real Milestone 4 proposals to be rejected (they all moved the "
        f"setpoint colder while the zone was already too cold), got {rejected_count}/3"
    )
    print(f"[PASS] all {rejected_count}/3 real backwards proposals correctly rejected\n")

    print("=== Synthetic completeness cases ===\n")
    r1 = validate_proposal(SYNTHETIC_PROPOSALS[0], current_setpoint, last_chunk_pmv)
    assert isinstance(r1, ValidatedAction) and not r1.clamped, r1
    run_case("synthetic: correct direction", SYNTHETIC_PROPOSALS[0], current_setpoint, last_chunk_pmv)

    r2 = validate_proposal(SYNTHETIC_PROPOSALS[1], current_setpoint, avg_pmv=0.0)
    assert isinstance(r2, ValidatedAction) and r2.clamped and r2.value_celsius == 26.0, r2
    run_case("synthetic: out-of-range + neutral PMV", SYNTHETIC_PROPOSALS[1], current_setpoint, 0.0)

    print("[PASS] accept path and clamp path both verified")


if __name__ == "__main__":
    main()