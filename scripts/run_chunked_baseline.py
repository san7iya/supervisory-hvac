"""
Baseline comparison run: same chunking machinery as run_chunked_loop.py, but
against the unmodified rule-based schedule from baseline.idf, so the
chunk-boundary warmup artifact (~6.7%, see docs/architecture-notes.md)
cancels out of the eventual baseline-vs-AI delta instead of confounding it.

One-off script to generate the Section 3 baseline series -- not part of the
orchestrator, no setpoint editing, no LLM, no validation logic.
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
BASELINE_RUN_IDF = RUN_DIR / "baseline_run.idf"
CHUNKS_DIR = RUN_DIR / "chunks_baseline"

CHUNK_DAYS = 7
WINDOW = dict(start_month=1, start_day=1, end_month=1, end_day=31)

# The schedule Milestone 2's test_eppy_edit.py is known to modify (in
# modified.idf, which lives in the same run1 directory) -- checked below to
# prove this script's IDF wasn't accidentally sourced from that file.
WATCHED_SCHEDULE = "CLGSETP_SCH_NO_OPTIMUM"


def prepare_baseline_run_idf() -> str:
    """Start baseline_run.idf fresh from baseline.idf (never modified.idf),
    collapsed to a single RunPeriod the same way run_chunked_loop.py does."""
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(BASELINE_IDF))
    run_periods = idf.idfobjects["RunPeriod"]
    for rp in run_periods[1:]:
        idf.removeidfobject(rp)
    idf.saveas(str(BASELINE_RUN_IDF))
    return run_periods[0].Name


def assert_schedule_unmodified() -> None:
    """Field-by-field diff of WATCHED_SCHEDULE between baseline.idf and the
    freshly prepared baseline_run.idf. Fails loudly, before any EnergyPlus
    runtime is spent, if the two ever diverge."""
    IDF.setiddname(str(IDD_PATH))
    original = IDF(str(BASELINE_IDF)).getobject("Schedule:Compact", WATCHED_SCHEDULE)
    prepared = IDF(str(BASELINE_RUN_IDF)).getobject("Schedule:Compact", WATCHED_SCHEDULE)

    if original is None or prepared is None:
        raise AssertionError(f"schedule {WATCHED_SCHEDULE!r} missing from one of the IDFs")

    # NOTE: eppy's .fieldnames on an extensible object (Schedule:Compact) returns
    # the IDD's max extensible slot count (10001), almost all unused/empty on both
    # sides. Comparing the full list is still correct (empty == empty matches
    # trivially), but restrict to populated fields so the reported count reflects
    # what was actually checked instead of implying 10001 real values were diffed.
    original_values = {fn: getattr(original, fn) for fn in original.fieldnames}
    prepared_values = {fn: getattr(prepared, fn) for fn in prepared.fieldnames}
    populated_fields = [fn for fn, v in original_values.items() if v not in (None, "")]

    diffs = [
        (fn, original_values[fn], prepared_values.get(fn))
        for fn in populated_fields
        if original_values[fn] != prepared_values.get(fn)
    ]
    if diffs:
        raise AssertionError(
            f"{WATCHED_SCHEDULE} differs between baseline.idf and baseline_run.idf: {diffs}"
        )

    print(f"[SCHEDULE_CHECK] {WATCHED_SCHEDULE} matches baseline.idf exactly "
          f"({len(populated_fields)} populated fields compared) -- no Milestone 2 contamination.")


def main() -> None:
    if CHUNKS_DIR.exists():
        shutil.rmtree(CHUNKS_DIR)
    CHUNKS_DIR.mkdir(parents=True)

    run_period_name = prepare_baseline_run_idf()
    assert_schedule_unmodified()

    chunks = chunk_dates(**WINDOW, chunk_days=CHUNK_DAYS)

    cumulative_kwh = 0.0
    print(f"[BASELINE_LOOP_START] {len(chunks)} chunks, {CHUNK_DAYS}-day window each, "
          f"run_period={run_period_name!r}")

    for i, (bm, bd, em, ed) in enumerate(chunks, start=1):
        IDF.setiddname(str(IDD_PATH))
        idf = IDF(str(BASELINE_RUN_IDF))
        set_run_period(idf, run_period_name, bm, bd, em, ed)
        idf.save()

        out_dir = CHUNKS_DIR / f"chunk_{i:02d}"
        print(f"[CHUNK_START] chunk={i} window={bm:02d}/{bd:02d}-{em:02d}/{ed:02d} "
              f"cumulative_kwh_so_far={cumulative_kwh:.1f}")

        try:
            run_energyplus(EPLUS_DIR / "energyplus.exe", BASELINE_RUN_IDF, WEATHER,
                            out_dir, cwd=RUN_DIR, timeout_s=60)
        except (EnergyPlusFailed, EnergyPlusTimeout) as e:
            print(f"[CHUNK_FAILED] chunk={i} error={e}")
            raise

        chunk_kwh = total_facility_kwh(out_dir)
        cumulative_kwh += chunk_kwh
        print(f"[CHUNK_RESULT] chunk={i} kwh={chunk_kwh:.1f} cumulative_kwh={cumulative_kwh:.1f}")

    print(f"[BASELINE_LOOP_END] total_kwh={cumulative_kwh:.1f} chunks_completed={len(chunks)}")


if __name__ == "__main__":
    main()