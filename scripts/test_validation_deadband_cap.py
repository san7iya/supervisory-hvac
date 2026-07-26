"""
Validation-layer proof for the two new guardrails (deadband check,
cumulative-movement cap), added after the diagnostic report traced 3
natural EnergyPlus failures to a real gap: validation.py never checked
heating setpoint against cooling setpoint, letting an accepted proposal
push heating (24.11C) above cooling (23.89C) -- a deterministic, fatal
DualSetPointWithDeadBand failure, confirmed reproducible standalone.

Three real/known-case tests, per the task:
  1. Reconstruct the exact failing state and confirm the deadband check
     now rejects it -- the actual negative-path proof.
  2. Replay all 7 previously-accepted real proposals (2 from 8B, 5 from
     70B, structural-fix run) through the new checks and report exactly
     what happens -- verified, not assumed.
  3. A synthetic case for the cumulative cap (no real run has moved a
     field far enough to hit +-2.5C on its own yet), plus one synthetic
     case for the deadband check's cooling-side symmetry, since no real
     proposal has ever targeted cooling_setpoint.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from supervisory_hvac.llm_reasoning import Proposal  # noqa: E402
from supervisory_hvac.validation import (  # noqa: E402
    CUMULATIVE_CAP_C,
    MIN_DEADBAND_C,
    RejectedAction,
    ValidatedAction,
    validate_proposal,
)

HEATING_BASELINE_C = 21.11  # original, unedited HTGSETP_SCH_NO_OPTIMUM occupied value
COOLING_BASELINE_C = 23.89  # original, unedited CLGSETP_SCH_NO_OPTIMUM occupied value


def run_case(label, proposal, current, avg_pmv, other_current, baseline, expect):
    result = validate_proposal(proposal, current, avg_pmv, other_current, baseline)
    got = "ACCEPT" if isinstance(result, ValidatedAction) else "REJECT"
    status = "PASS" if got == expect else "FAIL"
    print(f"[{status}] {label}")
    print(f"    current={current:.2f}C other_field_current={other_current:.2f}C "
          f"baseline={baseline:.2f}C avg_pmv={avg_pmv:+.2f}")
    print(f"    proposal: {proposal.field} direction={proposal.direction} magnitude={proposal.magnitude}")
    if isinstance(result, RejectedAction):
        print(f"    -> REJECTED: {result.reason}")
    else:
        print(f"    -> ACCEPTED: value={result.value_celsius:.2f}C clamped={result.clamped}")
    print()
    return status == "PASS"


def main():
    results = []

    print("=" * 70)
    print("1. Reconstruct the exact failing state from the diagnostic report")
    print("=" * 70 + "\n")
    # The real failure: heating proposal landing at 24.11C while cooling
    # sits at its untouched baseline of 23.89C. current=22.61C (the state
    # right before the fatal proposal was applied) + medium raise (+1.5) = 24.11C.
    failing_proposal = Proposal(
        field="heating_setpoint", direction="raise", magnitude="medium",
        window="09:00-17:00", reasoning="reconstructed from the diagnostic report",
    )
    results.append(run_case(
        "exact failing state (heating -> 24.11C vs cooling 23.89C, the real crash)",
        failing_proposal, current=22.61, avg_pmv=-0.82, other_current=COOLING_BASELINE_C,
        baseline=HEATING_BASELINE_C, expect="REJECT",
    ))

    print("=" * 70)
    print("2. Replay all 7 previously-accepted real proposals")
    print("=" * 70 + "\n")
    # field/direction/magnitude/current/avg_pmv exactly as validated at the time,
    # from the structural-fix run's transcript. Cooling was never touched in any
    # of these runs, so other_field_current_celsius = COOLING_BASELINE_C throughout.
    real_proposals = [
        ("8B chunk1", Proposal("heating_setpoint", "raise", "medium", "", "..."), 21.11, -1.10),
        ("8B chunk2", Proposal("heating_setpoint", "raise", "medium", "", "..."), 22.61, -0.87),
        ("70B chunk1", Proposal("heating_setpoint", "raise", "medium", "09:00-17:00", "..."), 21.11, -1.10),
        ("70B chunk2", Proposal("heating_setpoint", "raise", "medium", "09:00-17:00", "..."), 22.61, -0.87),
        ("70B chunk3", Proposal("heating_setpoint", "raise", "medium", "09:00-17:00", "..."), 22.61, -0.82),
        ("70B chunk4", Proposal("heating_setpoint", "raise", "medium", "09:00-17:00", "..."), 22.61, -0.88),
        ("70B chunk5", Proposal("heating_setpoint", "raise", "medium", "08:00-17:00", "..."), 22.61, -1.10),
    ]
    accepted, rejected = 0, 0
    for label, proposal, current, avg_pmv in real_proposals:
        result = validate_proposal(proposal, current, avg_pmv, COOLING_BASELINE_C, HEATING_BASELINE_C)
        if isinstance(result, ValidatedAction):
            accepted += 1
            print(f"[{label}] current={current:.2f}C -> ACCEPTED: value={result.value_celsius:.2f}C")
        else:
            rejected += 1
            print(f"[{label}] current={current:.2f}C -> REJECTED: {result.reason}")
    print(f"\n{accepted} accepted, {rejected} rejected (of 7 real proposals)\n")

    print("=" * 70)
    print("3. Synthetic cases")
    print("=" * 70 + "\n")

    # Cumulative cap tested in ISOLATION from the deadband check: if the
    # other field is left at its real baseline, a heating raise big enough to
    # trip the +-2.5C cap (baseline 21.11C) also trips the deadband check at
    # the same value (24.11C is the real failing case from part 1). To prove
    # the cumulative cap independently, the other field is set comfortably
    # far away here (25.5C) so ONLY the cumulative cap can fire.
    results.append(run_case(
        "[SYNTHETIC] cumulative cap (isolated from deadband): heating already "
        "+1.5C from baseline, another medium raise would reach +3.0C (exceeds +-2.5C cap)",
        Proposal("heating_setpoint", "raise", "medium", "", "[SYNTHETIC]"),
        current=22.61, avg_pmv=0.0, other_current=25.5, baseline=HEATING_BASELINE_C, expect="REJECT",
    ))
    results.append(run_case(
        "[SYNTHETIC] cumulative cap (isolated from deadband): same field/direction, "
        "but only +0.5C total -- should pass",
        Proposal("heating_setpoint", "raise", "small", "", "[SYNTHETIC]"),
        current=HEATING_BASELINE_C, avg_pmv=0.0, other_current=25.5, baseline=HEATING_BASELINE_C, expect="ACCEPT",
    ))

    # Deadband symmetry: a cooling_setpoint proposal dropping too close to the
    # CURRENT heating setpoint -- never exercised by real data (no real
    # proposal has ever targeted cooling_setpoint).
    results.append(run_case(
        "[SYNTHETIC] deadband symmetry: cooling setpoint lowered to within "
        f"{MIN_DEADBAND_C}C of current heating setpoint",
        Proposal("cooling_setpoint", "lower", "small", "", "[SYNTHETIC]"),
        current=22.0, avg_pmv=0.6, other_current=21.61, baseline=COOLING_BASELINE_C, expect="REJECT",
    ))

    print("=" * 70)
    all_pass = all(results)
    print("ALL SYNTHETIC/RECONSTRUCTED CASES PASS" if all_pass else "SOME CASES FAILED")
    if not all_pass:
        raise AssertionError("one or more test cases did not match expectations")


if __name__ == "__main__":
    main()