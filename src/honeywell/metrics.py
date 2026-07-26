"""Output extraction only -- reads what EnergyPlus already wrote, no
interpretation or judgment about whether the numbers are good."""
import csv
from pathlib import Path


def total_facility_kwh(out_dir: Path) -> float:
    meter_csv = Path(out_dir) / "eplusmtr.csv"
    total_j = 0.0
    with open(meter_csv, newline="") as f:
        reader = csv.DictReader(f)
        col = next(c for c in reader.fieldnames if c.startswith("Electricity:Facility"))
        for row in reader:
            total_j += float(row[col])
    return total_j / 3_600_000  # J -> kWh


def _average_columns_matching(out_dir: Path, suffix: str, zone_prefixes: set = None) -> float | None:
    """Average of all timestep values across columns whose header contains
    `suffix` in eplusout.csv, optionally restricted to a set of zone-name
    prefixes. Returns None if no such columns exist (e.g. baseline.idf
    predates the comfort-output additions)."""
    eplusout_csv = Path(out_dir) / "eplusout.csv"
    if not eplusout_csv.exists():
        return None

    with open(eplusout_csv, newline="") as f:
        reader = csv.DictReader(f)
        cols = [c for c in reader.fieldnames if suffix in c]
        if zone_prefixes is not None:
            cols = [c for c in cols if c.split(":", 1)[0] in zone_prefixes]
        if not cols:
            return None
        total = 0.0
        count = 0
        for row in reader:
            for c in cols:
                total += float(row[c])
                count += 1

    return total / count if count else None


def _occupied_zone_names(out_dir: Path) -> set:
    """Zones that have a PMV column, i.e. zones with People objects --
    used to exclude unconditioned spaces like the attic from comfort-relevant
    temperature averages."""
    eplusout_csv = Path(out_dir) / "eplusout.csv"
    if not eplusout_csv.exists():
        return set()
    with open(eplusout_csv, newline="") as f:
        reader = csv.DictReader(f)
        pmv_cols = [c for c in reader.fieldnames if "Zone Thermal Comfort Fanger Model PMV" in c]
    return {c.split(":", 1)[0] for c in pmv_cols}


def average_zone_temp_c(out_dir: Path) -> float | None:
    occupied = _occupied_zone_names(out_dir)
    return _average_columns_matching(out_dir, "Zone Mean Air Temperature", zone_prefixes=occupied or None)


def average_pmv(out_dir: Path) -> float | None:
    return _average_columns_matching(out_dir, "Zone Thermal Comfort Fanger Model PMV")