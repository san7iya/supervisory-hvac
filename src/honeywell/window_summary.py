"""Builds a plain-text summary of recent chunk metrics to feed the LLM
reasoning step. Reports only what's actually present in each chunk's
EnergyPlus output -- average_zone_temp_c/average_pmv return None (and get
reported as "not available") for any chunk run before baseline.idf gained
its comfort Output:Variable requests, rather than fabricating values."""
from pathlib import Path

from .metrics import average_pmv, average_zone_temp_c, total_facility_kwh


def summarize_chunks(chunk_dirs: list[Path]) -> str:
    lines = ["Recent simulated chunks (most recent last):"]
    any_missing_comfort = False
    for i, chunk_dir in enumerate(chunk_dirs, start=1):
        kwh = total_facility_kwh(chunk_dir)
        temp = average_zone_temp_c(chunk_dir)
        pmv = average_pmv(chunk_dir)

        parts = [f"{kwh:.1f} kWh facility electricity"]
        if temp is not None:
            parts.append(f"avg occupied-zone temp {temp:.1f}C")
        else:
            any_missing_comfort = True
        if pmv is not None:
            parts.append(f"avg PMV {pmv:+.2f}")
        else:
            any_missing_comfort = True

        lines.append(f"  Chunk {i} ({chunk_dir.name}): " + ", ".join(parts))

    if any_missing_comfort:
        lines.append(
            "\nNote: PMV and/or zone temperature data is missing for at least one chunk "
            "above (no Output:Variable request for it in that run's IDF). Do not invent "
            "comfort or temperature figures for those chunks -- reason from what's present."
        )
    return "\n".join(lines)