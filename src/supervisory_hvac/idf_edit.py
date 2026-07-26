"""Applies a ValidatedAction to an IDF. The only module with authority to
write setpoint values into the IDF -- everything upstream (LLM reasoning,
validation) only produces data structures, never touches the file.

FIELD_WHITELIST addresses edits by (object_type, object_name, field_names),
never by value-matching -- see docs/architecture-notes.md's Milestone 2
finding for why value-matching is unsafe (it can silently reach into an
unintended field, like a design-day sizing schedule, that happens to share
a numeric value with the intended one).

Field_6 and Field_8 are the two weekday-occupied-value slots (7:00 and
18:00 boundaries) in the *_NO_OPTIMUM Schedule:Compact objects -- confirmed
directly against baseline.idf, not assumed from field position alone.
Covers Core_ZN, Perimeter_ZN_1, Perimeter_ZN_3, Perimeter_ZN_4 (the zones
wired to these schedules); Perimeter_ZN_2 uses a separate *_w_SB schedule
not covered here, consistent with every prior milestone's scope.
"""
from .validation import ValidatedAction

FIELD_WHITELIST = {
    "heating_setpoint": {
        "object_type": "Schedule:Compact",
        "object_name": "HTGSETP_SCH_NO_OPTIMUM",
        "field_names": ["Field_6", "Field_8"],
    },
    "cooling_setpoint": {
        "object_type": "Schedule:Compact",
        "object_name": "CLGSETP_SCH_NO_OPTIMUM",
        "field_names": ["Field_6", "Field_8"],
    },
}


def apply_action(idf, action: ValidatedAction) -> None:
    """Mutates idf in place. Raises KeyError if action.field isn't in
    FIELD_WHITELIST (should be unreachable -- validate_proposal only ever
    passes through fields llm_reasoning's schema already restricted to
    this same set, but this module doesn't trust that upstream guarantee
    either)."""
    spec = FIELD_WHITELIST[action.field]
    obj = idf.getobject(spec["object_type"], spec["object_name"])
    if obj is None:
        raise ValueError(f"{spec['object_type']} {spec['object_name']!r} not found in IDF")
    for field_name in spec["field_names"]:
        setattr(obj, field_name, str(action.value_celsius))


def get_current_setpoint_celsius(idf, field: str) -> float:
    """Reads the setpoint currently in effect for `field` from idf, using
    the same whitelist mapping apply_action writes through -- so "current"
    always means the same field apply_action would edit."""
    spec = FIELD_WHITELIST[field]
    obj = idf.getobject(spec["object_type"], spec["object_name"])
    if obj is None:
        raise ValueError(f"{spec['object_type']} {spec['object_name']!r} not found in IDF")
    return float(getattr(obj, spec["field_names"][0]))