"""
Milestone 2 proof: eppy can read a setpoint schedule field, edit it, save a new
IDF, and the edit measurably changes EnergyPlus's simulated output.

Not the orchestrator, not the validation layer -- just proving the eppy
mechanics work in isolation before anything else is built on top of it.
"""
import csv
import os
import shutil
import subprocess
from pathlib import Path

from eppy.modeleditor import IDF

EPLUS_DIR = Path(os.environ.get("EPLUS_DIR", "D:/EnergyPlusV25-1-0"))
RUN_DIR = Path(os.environ.get("RUN_DIR", "D:/Honeywell-env/run1"))
IDD_PATH = EPLUS_DIR / "Energy+.idd"
BASELINE_IDF = RUN_DIR / "baseline.idf"
WEATHER = RUN_DIR / "weather.epw"
MODIFIED_IDF = RUN_DIR / "modified.idf"
BASELINE_OUT = RUN_DIR / "output"
MODIFIED_OUT = RUN_DIR / "output_modified"

SCHEDULE_NAME = "CLGSETP_SCH_NO_OPTIMUM"
OCCUPIED_SETPOINT_OLD = 23.89
OCCUPIED_SETPOINT_NEW = 25.5  # +1.61C setback, same move as the Section 6 example log


def run_energyplus(idf_path: Path, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    subprocess.run(
        [str(EPLUS_DIR / "energyplus.exe"), "-w", str(WEATHER), "-r", "-d", str(out_dir), str(idf_path)],
        cwd=RUN_DIR,
        check=True,
        capture_output=True,
        timeout=120,
    )


def total_facility_kwh(out_dir: Path) -> float:
    meter_csv = out_dir / "eplusmtr.csv"
    total_j = 0.0
    with open(meter_csv, newline="") as f:
        reader = csv.DictReader(f)
        col = next(c for c in reader.fieldnames if c.startswith("Electricity:Facility"))
        for row in reader:
            total_j += float(row[col])
    return total_j / 3_600_000  # J -> kWh


def main() -> None:
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(BASELINE_IDF))

    sched = idf.getobject("Schedule:Compact", SCHEDULE_NAME)
    assert sched is not None, f"schedule {SCHEDULE_NAME} not found"

    # Find the field holding the occupied weekday cooling setpoint (23.89) and bump it.
    # NOTE: eppy stores Schedule:Compact field values as strings, not floats --
    # `value == 23.89` silently matches nothing even when the field holds "23.89".
    # Don't "clean up" the float(value) conversion below; it's load-bearing.
    #
    # NOTE: this matches by VALUE, which is deliberately wrong for production use --
    # see docs/architecture-notes.md. It caught 3 fields here (two occupied-setpoint
    # slots plus the SummerDesignDay branch, which happened to share the same value),
    # not just the one field this script meant to target. The real validation-layer
    # whitelist (Milestone 5) must address fields by name/position, never by value.
    changed = 0
    for fieldname in sched.fieldnames:
        value = getattr(sched, fieldname, None)
        if value is None:
            is_match = False
        else:
            try:
                is_match = float(value) == OCCUPIED_SETPOINT_OLD
            except ValueError:
                is_match = False
        if is_match:
            setattr(sched, fieldname, str(OCCUPIED_SETPOINT_NEW))
            changed += 1
    assert changed > 0, "no matching field found to edit"
    print(f"[EPPY] Changed {changed} field(s) in {SCHEDULE_NAME}: "
          f"{OCCUPIED_SETPOINT_OLD} -> {OCCUPIED_SETPOINT_NEW} C")

    idf.saveas(str(MODIFIED_IDF))
    print(f"[EPPY] Saved edited IDF to {MODIFIED_IDF}")

    print("[RUN] Baseline...")
    run_energyplus(BASELINE_IDF, BASELINE_OUT)
    print("[RUN] Modified...")
    run_energyplus(MODIFIED_IDF, MODIFIED_OUT)

    baseline_kwh = total_facility_kwh(BASELINE_OUT)
    modified_kwh = total_facility_kwh(MODIFIED_OUT)
    delta_pct = (modified_kwh - baseline_kwh) / baseline_kwh * 100

    print(f"\nBaseline facility electricity: {baseline_kwh:,.1f} kWh")
    print(f"Modified facility electricity:  {modified_kwh:,.1f} kWh")
    print(f"Delta: {delta_pct:+.2f}%")

    assert abs(delta_pct) > 0.01, "edit produced no measurable change in output"
    print("\n[PASS] eppy edit measurably changed simulated output.")


if __name__ == "__main__":
    main()
