"""Validation layer for LLM-proposed setpoint adjustments. Sits between the
LLM reasoning step (advisory only, Milestone 4) and eppy/IDF application
(a later milestone) -- the LLM proposes, this validates, nothing downstream
of this module trusts the LLM's own restraint.

Four independent, deliberately different checks, per Section 5 and the real
failure cases in docs/architecture-notes.md:

  1. Range clamp -- catches "the number is absurd," regardless of reasoning.
  2. Directional-consistency check -- catches "the direction is backwards
     for the observed comfort state." A range clamp cannot catch this: an
     in-range but wrong-direction proposal sails through untouched.
  3. Deadband check -- catches a proposal that, considered alone, looks
     fine (in range, right direction) but would push its field's setpoint
     too close to -- or past -- the OTHER field's current setpoint. This is
     the gap that let heating (24.11C) cross above cooling (23.89C) and
     cause a real, deterministic, fatal EnergyPlus failure
     (DualSetPointWithDeadBand) -- confirmed reproducible standalone. Range
     clamp and directional-consistency both check a field against itself;
     neither ever compares the two fields against each other.
  4. Cumulative-movement cap -- catches unbounded drift across many
     accepted proposals over time. The deadband check only looks at the
     instant before/after a single proposal; nothing before this stopped a
     field from wandering arbitrarily far from where it started as long as
     each individual step passed the other checks.

The model no longer outputs a raw setpoint value -- only a direction
("raise"/"lower") and a magnitude ("small"/"medium"/"large"). The actual
degree-Celsius change is computed here, deterministically, from
MAGNITUDE_DELTA_C -- so there is no longer a second, independent number for
the model's own reasoning text to disagree with. The directional check now
compares proposal.direction directly against the observed PMV state,
instead of inferring direction by comparing two numbers.

None of these checks verify whether the model's *reasoning* is sound --
only that the proposed *action* is structurally consistent with the
*observed state* (checks 2-4) or an absolute safety bound (check 1). The
reasoning-text log (Section 6) remains the line of defense for subtler
cases none of these can catch.
"""
from dataclasses import dataclass
from typing import Union

from .llm_reasoning import Proposal

# Section 5's example clamp band.
SETPOINT_MIN_C = 18.0
SETPOINT_MAX_C = 26.0

# PMV thresholds beyond which the zone is clearly outside comfort, not just
# slightly off -- matches the ASHRAE/Fanger convention that the recommended
# comfort range is roughly -0.5 to +0.5.
PMV_TOO_COLD = -0.5
PMV_TOO_WARM = 0.5

# Fixed degree-Celsius delta per magnitude choice -- deterministic, not
# something the model supplies. Applied to current_setpoint_celsius,
# signed by direction, before clamping.
MAGNITUDE_DELTA_C = {"small": 0.5, "medium": 1.5, "large": 3.0}

# Minimum gap required between the heating and cooling setpoints. Below this,
# a dual-setpoint thermostat has no valid deadband -- EnergyPlus fails fatally
# (DualSetPointWithDeadBand) rather than simulate an undefined control state.
# 1.0C is a standard HVAC minimum; tune here if the building model needs a
# wider/narrower band.
MIN_DEADBAND_C = 1.0

# Maximum total deviation (either direction) a field's setpoint may accumulate
# across any number of accepted proposals, measured from that field's
# original baseline value -- independent of the deadband check, which only
# looks at one proposal at a time and says nothing about cumulative drift.
CUMULATIVE_CAP_C = 2.5


@dataclass
class ValidatedAction:
    field: str
    value_celsius: float
    window: str
    reasoning: str
    clamped: bool  # True if value_celsius was adjusted to fit the range


@dataclass
class RejectedAction:
    reason: str
    proposal: Proposal


def _clamp(value: float) -> tuple:
    clamped_value = max(SETPOINT_MIN_C, min(SETPOINT_MAX_C, value))
    return clamped_value, clamped_value != value


def _direction_violation_reason(direction: str, avg_pmv: float) -> str:
    """Returns a specific reason string if the chosen direction is wrong
    given the observed PMV, or an empty string if there's no violation.
    Checks the enum directly -- no number comparison involved."""
    if avg_pmv <= PMV_TOO_COLD and direction == "lower":
        return (
            f"zone is already too cold (avg_pmv={avg_pmv:+.2f}), but proposal direction "
            f"is 'lower' -- wrong direction"
        )
    if avg_pmv >= PMV_TOO_WARM and direction == "raise":
        return (
            f"zone is already too warm (avg_pmv={avg_pmv:+.2f}), but proposal direction "
            f"is 'raise' -- wrong direction"
        )
    return ""


def _deadband_violation_reason(field: str, new_value: float, other_field_current_celsius: float) -> str:
    """Returns a specific reason string if new_value would leave less than
    MIN_DEADBAND_C between the heating and cooling setpoints, or an empty
    string if there's no violation. Symmetric: works the same whether the
    proposal being checked is for heating_setpoint or cooling_setpoint --
    only which side of the gap new_value lands on changes."""
    if field == "heating_setpoint":
        heating_value, cooling_value = new_value, other_field_current_celsius
    else:  # cooling_setpoint
        heating_value, cooling_value = other_field_current_celsius, new_value

    gap = cooling_value - heating_value
    if gap < MIN_DEADBAND_C:
        return (
            f"would violate minimum {MIN_DEADBAND_C}C deadband: "
            f"heating {heating_value:.2f}C vs cooling {cooling_value:.2f}C (gap={gap:.2f}C)"
        )
    return ""


def _cumulative_cap_violation_reason(new_value: float, field_baseline_celsius: float) -> str:
    """Returns a specific reason string if new_value deviates from
    field_baseline_celsius by more than CUMULATIVE_CAP_C in either
    direction, or an empty string if there's no violation."""
    deviation = new_value - field_baseline_celsius
    if abs(deviation) > CUMULATIVE_CAP_C:
        return (
            f"exceeds cumulative movement cap ({CUMULATIVE_CAP_C}C): "
            f"{deviation:+.2f}C from baseline {field_baseline_celsius:.2f}C"
        )
    return ""


def validate_proposal(
    proposal: Proposal,
    current_setpoint_celsius: float,
    avg_pmv: float,
    other_field_current_celsius: float,
    field_baseline_celsius: float,
) -> Union[ValidatedAction, RejectedAction]:
    """proposal: a schema-valid Proposal from llm_reasoning (field/whitelist
    already enforced upstream -- this module doesn't re-check those).
    current_setpoint_celsius: the setpoint currently in effect for
    proposal.field, read from the live IDF by the caller.
    avg_pmv: most recent observed comfort reading, e.g. from the latest
    chunk's metrics.average_pmv().
    other_field_current_celsius: the setpoint currently in effect for the
    OTHER field (cooling if proposal.field is heating, and vice versa) --
    needed for the deadband check. Caller-supplied, same file-I/O-free
    boundary as current_setpoint_celsius.
    field_baseline_celsius: proposal.field's ORIGINAL, never-edited value
    (not its current value, which may already reflect prior accepted
    proposals) -- needed for the cumulative-movement cap.

    Checks run in this order: direction, then range clamp, then deadband,
    then cumulative cap -- each is checked before any computation only the
    later checks need, so a proposal rejected early never needs its
    numeric value computed at all."""
    violation = _direction_violation_reason(proposal.direction, avg_pmv)
    if violation:
        return RejectedAction(
            reason=f"directional-consistency violation: {violation}",
            proposal=proposal,
        )

    delta = MAGNITUDE_DELTA_C[proposal.magnitude]
    signed_delta = delta if proposal.direction == "raise" else -delta
    raw_value = current_setpoint_celsius + signed_delta
    clamped_value, was_clamped = _clamp(raw_value)

    deadband_violation = _deadband_violation_reason(proposal.field, clamped_value, other_field_current_celsius)
    if deadband_violation:
        return RejectedAction(reason=deadband_violation, proposal=proposal)

    cap_violation = _cumulative_cap_violation_reason(clamped_value, field_baseline_celsius)
    if cap_violation:
        return RejectedAction(reason=cap_violation, proposal=proposal)

    return ValidatedAction(
        field=proposal.field,
        value_celsius=clamped_value,
        window=proposal.window,
        reasoning=proposal.reasoning,
        clamped=was_clamped,
    )