"""
One-off setup script: adds zone temperature and Fanger PMV comfort reporting
to baseline.idf. This is purely additive to what EnergyPlus *reports* --
Output:Variable requests and People-object comfort-model fields don't affect
HVAC/zone physics or energy results, only what gets logged. Modifies
baseline.idf in place since it's the single canonical source both
run_chunked_loop.py and run_chunked_baseline.py derive current.idf /
baseline_run.idf from.

Clothing/air-velocity schedule values are taken from EnergyPlus's own
1ZoneUncontrolled_Win_ASH55_Thermal_Comfort.idf example (a working reference
for the Fanger model), not invented.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eppy.modeleditor import IDF  # noqa: E402

EPLUS_DIR = Path("D:/EnergyPlusV25-1-0")
RUN_DIR = Path("D:/Honeywell-env/run1")
IDD_PATH = EPLUS_DIR / "Energy+.idd"
BASELINE_IDF = RUN_DIR / "baseline.idf"

CLOTHING_SCHEDULE_NAME = "CLOTHING_SCH"
AIR_VELOCITY_SCHEDULE_NAME = "AIR_VELO_SCH"
WORK_EFFICIENCY_SCHEDULE_NAME = "WORK_EFF_SCH"

OUTPUT_VARIABLES = [
    "Zone Mean Air Temperature",
    "Zone Thermal Comfort Fanger Model PMV",
]


def ensure_schedule_exists(idf, name, type_limits, until_pairs):
    """until_pairs: list of (through_date, value) applied For: AllDays."""
    if idf.getobject("Schedule:Compact", name) is not None:
        return
    sched = idf.newidfobject("Schedule:Compact", Name=name)
    sched.Schedule_Type_Limits_Name = type_limits
    fields = []
    for through_date, value in until_pairs:
        fields.append(f"Through: {through_date}")
        fields.append("For: AllDays")
        fields.append(f"Until: 24:00,{value}")
    for i, value in enumerate(fields, start=1):
        setattr(sched, f"Field_{i}", value)


def main() -> None:
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(BASELINE_IDF))

    ensure_schedule_exists(
        idf, CLOTHING_SCHEDULE_NAME, "Any Number",
        [("04/30", 1.0), ("09/30", 0.5), ("12/31", 1.0)],
    )
    ensure_schedule_exists(
        idf, AIR_VELOCITY_SCHEDULE_NAME, "Any Number",
        [("12/31", 0.15)],
    )
    ensure_schedule_exists(
        idf, WORK_EFFICIENCY_SCHEDULE_NAME, "Fraction",
        [("12/31", 0.0)],
    )

    # Work Efficiency Schedule Name is required whenever any Thermal Comfort
    # Model Type is set (Fanger/Pierce/KSU/CoolingEffectASH55/AnkleDraftASH55) --
    # EnergyPlus fails Fatal at GetInternalHeatGains without it. Set unconditionally,
    # separate from the Fanger-enable step below, so re-running this script after a
    # partial prior run still backfills it.
    people_objects = idf.idfobjects["People"]
    work_eff_set = 0
    for p in people_objects:
        if p.Work_Efficiency_Schedule_Name != WORK_EFFICIENCY_SCHEDULE_NAME:
            p.Work_Efficiency_Schedule_Name = WORK_EFFICIENCY_SCHEDULE_NAME
            work_eff_set += 1
    print(f"[COMFORT] Set Work Efficiency Schedule on {work_eff_set}/{len(people_objects)} People objects")

    updated = 0
    for p in people_objects:
        if p.Thermal_Comfort_Model_1_Type == "Fanger":
            continue
        p.Clothing_Insulation_Calculation_Method = "ClothingInsulationSchedule"
        p.Clothing_Insulation_Schedule_Name = CLOTHING_SCHEDULE_NAME
        p.Air_Velocity_Schedule_Name = AIR_VELOCITY_SCHEDULE_NAME
        p.Thermal_Comfort_Model_1_Type = "Fanger"
        updated += 1
    print(f"[COMFORT] Enabled Fanger PMV on {updated}/{len(people_objects)} People objects")

    existing_vars = {
        (ov.Key_Value, ov.Variable_Name)
        for ov in idf.idfobjects["Output:Variable"]
    }
    added = 0
    for var_name in OUTPUT_VARIABLES:
        if ("*", var_name) in existing_vars:
            continue
        idf.newidfobject(
            "Output:Variable",
            Key_Value="*",
            Variable_Name=var_name,
            Reporting_Frequency="Hourly",
        )
        added += 1
    print(f"[COMFORT] Added {added} new Output:Variable request(s): {OUTPUT_VARIABLES}")

    idf.save()
    print(f"[COMFORT] Saved {BASELINE_IDF}")


if __name__ == "__main__":
    main()