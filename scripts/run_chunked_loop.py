"""
Milestone 3 proof: sequential chunk execution with state hand-off, no AI,
no validation/rollback logic. Pure mechanical loop -- proves the plumbing
(chunk boundaries, subprocess invocation, output parsing, cumulative state)
before any reasoning gets added on top of it.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eppy.modeleditor import IDF  # noqa: E402

from supervisory_hvac.chunker import chunk_dates, set_run_period  # noqa: E402
from supervisory_hvac.eplus_runner import EnergyPlusFailed, EnergyPlusTimeout, run_energyplus  # noqa: E402
from supervisory_hvac.metrics import total_facility_kwh  # noqa: E402

EPLUS_DIR = Path("D:/EnergyPlusV25-1-0")
RUN_DIR = Path("D:/Honeywell-env/run1")
IDD_PATH = EPLUS_DIR / "Energy+.idd"
BASELINE_IDF = RUN_DIR / "baseline.idf"
WEATHER = RUN_DIR / "weather.epw"
CURRENT_IDF = RUN_DIR / "current.idf"
CHUNKS_DIR = RUN_DIR / "chunks"

CHUNK_DAYS = 7
WINDOW = dict(start_month=1, start_day=1, end_month=1, end_day=31)


def prepare_current_idf() -> str:
    """Start current.idf from baseline.idf, collapsed to a single RunPeriod
    whose dates get overwritten each chunk. Baseline itself is never touched."""
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(BASELINE_IDF))
    run_periods = idf.idfobjects["RunPeriod"]
    for rp in run_periods[1:]:
        idf.removeidfobject(rp)
    idf.saveas(str(CURRENT_IDF))
    return run_periods[0].Name


def main() -> None:
    if CHUNKS_DIR.exists():
        shutil.rmtree(CHUNKS_DIR)
    CHUNKS_DIR.mkdir(parents=True)

    run_period_name = prepare_current_idf()
    chunks = chunk_dates(**WINDOW, chunk_days=CHUNK_DAYS)

    cumulative_kwh = 0.0
    print(f"[LOOP_START] {len(chunks)} chunks, {CHUNK_DAYS}-day window each, run_period={run_period_name!r}")

    for i, (bm, bd, em, ed) in enumerate(chunks, start=1):
        IDF.setiddname(str(IDD_PATH))
        idf = IDF(str(CURRENT_IDF))
        set_run_period(idf, run_period_name, bm, bd, em, ed)
        idf.save()

        out_dir = CHUNKS_DIR / f"chunk_{i:02d}"
        print(f"[CHUNK_START] chunk={i} window={bm:02d}/{bd:02d}-{em:02d}/{ed:02d} "
              f"cumulative_kwh_so_far={cumulative_kwh:.1f}")

        try:
            run_energyplus(EPLUS_DIR / "energyplus.exe", CURRENT_IDF, WEATHER,
                            out_dir, cwd=RUN_DIR, timeout_s=60)
        except (EnergyPlusFailed, EnergyPlusTimeout) as e:
            # No rollback/retry here on purpose -- that's Milestone 5.
            print(f"[CHUNK_FAILED] chunk={i} error={e}")
            raise

        chunk_kwh = total_facility_kwh(out_dir)
        cumulative_kwh += chunk_kwh
        print(f"[CHUNK_RESULT] chunk={i} kwh={chunk_kwh:.1f} cumulative_kwh={cumulative_kwh:.1f}")

    print(f"[LOOP_END] total_kwh={cumulative_kwh:.1f} chunks_completed={len(chunks)}")


if __name__ == "__main__":
    main()