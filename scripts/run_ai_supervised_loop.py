"""
Milestone 6: the first loop that integrates all four previously-isolated
modules -- chunker, LLM reasoning, validation, eppy application -- plus
rollback/retry on EnergyPlus failure and Section 6's structured logging on
every path (accepted, rejected, clamped, rolled back).

Design choices locked in for this milestone (see conversation/architecture
notes for the full reasoning):
  - LLM reasoning + validation happen AFTER a chunk's EnergyPlus run
    completes, deciding the schedule for the NEXT chunk. Chunk 1 always
    runs on the unedited baseline schedule.
  - A REJECTED proposal (schema-invalid, or clamp/direction-rejected) is
    logged and discarded; the loop proceeds with the last valid schedule.
    No same-chunk LLM retry.
  - A FAILED EnergyPlus run gets up to MAX_ENERGYPLUS_RETRIES attempts,
    rolling current.idf back to last_known_good.idf before each retry.
    After exhausting retries, the chunk is skipped (logged as a failure)
    and the loop continues -- one bad chunk no longer halts everything,
    unlike Milestone 3.
  - LLM context is a trailing window of the last LLM_CONTEXT_WINDOW chunks.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eppy.modeleditor import IDF  # noqa: E402

from supervisory_hvac.chunker import chunk_dates, set_run_period  # noqa: E402
from supervisory_hvac.eplus_runner import EnergyPlusFailed, EnergyPlusTimeout, run_energyplus  # noqa: E402
from supervisory_hvac.idf_edit import apply_action, get_current_setpoint_celsius  # noqa: E402
from supervisory_hvac.llm_reasoning import LLM_PROVIDER, Proposal, RejectedProposal, propose_setpoint_adjustment  # noqa: E402
from supervisory_hvac.metrics import average_pmv, total_facility_kwh  # noqa: E402
from supervisory_hvac.validation import RejectedAction, ValidatedAction, validate_proposal  # noqa: E402
from supervisory_hvac.window_summary import summarize_chunks  # noqa: E402

EPLUS_DIR = Path("D:/EnergyPlusV25-1-0")
RUN_DIR = Path("D:/Honeywell-env/run1")
IDD_PATH = EPLUS_DIR / "Energy+.idd"
BASELINE_IDF = RUN_DIR / "baseline.idf"
WEATHER = RUN_DIR / "weather.epw"
CURRENT_IDF = RUN_DIR / "ai_current.idf"
LAST_KNOWN_GOOD_IDF = RUN_DIR / "ai_last_known_good.idf"
CHUNKS_DIR = RUN_DIR / "chunks_ai_supervised"

CHUNK_DAYS = 7
WINDOW = dict(start_month=1, start_day=1, end_month=1, end_day=31)  # same window as the baseline run
MAX_ENERGYPLUS_RETRIES = 3
LLM_CONTEXT_WINDOW = 3  # trailing chunks fed to the LLM each cycle

OTHER_FIELD = {"heating_setpoint": "cooling_setpoint", "cooling_setpoint": "heating_setpoint"}


def read_field_baselines() -> dict:
    """Reads both fields' TRUE original values from baseline.idf -- a fresh
    load, independent of CURRENT_IDF, computed once before the loop starts
    and never touched again. These must stay fixed for the whole run (the
    cumulative-movement cap measures deviation from them); recomputing this
    from CURRENT_IDF instead, or re-reading it mid-loop, would silently let
    the baseline drift with each accepted edit and defeat the cap entirely."""
    IDF.setiddname(str(IDD_PATH))
    baseline_idf = IDF(str(BASELINE_IDF))
    return {
        field: get_current_setpoint_celsius(baseline_idf, field)
        for field in ("heating_setpoint", "cooling_setpoint")
    }


def prepare_current_idf() -> str:
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(BASELINE_IDF))
    run_periods = idf.idfobjects["RunPeriod"]
    for rp in run_periods[1:]:
        idf.removeidfobject(rp)
    idf.saveas(str(CURRENT_IDF))
    return run_periods[0].Name


def run_chunk_with_retry(run_period_name: str, bm: int, bd: int, em: int, ed: int, out_dir: Path) -> bool:
    """Returns True on success. On each failure, rolls current.idf back to
    last_known_good.idf and re-applies this chunk's RunPeriod dates before
    retrying -- the revert would otherwise also discard the date edit."""
    for attempt in range(1, MAX_ENERGYPLUS_RETRIES + 1):
        IDF.setiddname(str(IDD_PATH))
        idf = IDF(str(CURRENT_IDF))
        set_run_period(idf, run_period_name, bm, bd, em, ed)
        idf.save()

        try:
            run_energyplus(EPLUS_DIR / "energyplus.exe", CURRENT_IDF, WEATHER, out_dir, cwd=RUN_DIR, timeout_s=60)
            return True
        except (EnergyPlusFailed, EnergyPlusTimeout) as e:
            print(f"[ROLLBACK] EnergyPlus failed (attempt {attempt}/{MAX_ENERGYPLUS_RETRIES}): {e} "
                  f"-- reverting to last_known_good.idf")
            shutil.copyfile(LAST_KNOWN_GOOD_IDF, CURRENT_IDF)
    return False


def main() -> None:
    if CHUNKS_DIR.exists():
        shutil.rmtree(CHUNKS_DIR)
    CHUNKS_DIR.mkdir(parents=True)

    run_period_name = prepare_current_idf()
    shutil.copyfile(CURRENT_IDF, LAST_KNOWN_GOOD_IDF)

    field_baselines = read_field_baselines()
    print(f"[BASELINES] fixed for the whole run: {field_baselines}")

    chunks = chunk_dates(**WINDOW, chunk_days=CHUNK_DAYS)
    print(f"[LOOP_START] {len(chunks)} chunks, {CHUNK_DAYS}-day window each, provider={LLM_PROVIDER}")

    cumulative_kwh = 0.0
    successful_chunk_dirs = []  # in order, for the trailing LLM window

    for i, (bm, bd, em, ed) in enumerate(chunks, start=1):
        out_dir = CHUNKS_DIR / f"chunk_{i:02d}"
        print(f"\n[CHUNK_START] chunk={i} window={bm:02d}/{bd:02d}-{em:02d}/{ed:02d} "
              f"cumulative_kwh_so_far={cumulative_kwh:.1f}")

        success = run_chunk_with_retry(run_period_name, bm, bd, em, ed, out_dir)
        if not success:
            print(f"[CHUNK_FAILED] chunk={i} skipped after {MAX_ENERGYPLUS_RETRIES} failed attempts")
            continue

        shutil.copyfile(CURRENT_IDF, LAST_KNOWN_GOOD_IDF)

        chunk_kwh = total_facility_kwh(out_dir)
        chunk_pmv = average_pmv(out_dir)
        cumulative_kwh += chunk_kwh
        successful_chunk_dirs.append(out_dir)
        pmv_str = f"{chunk_pmv:+.2f}" if chunk_pmv is not None else "n/a"
        print(f"[CHUNK_RESULT] chunk={i} kwh={chunk_kwh:.1f} cumulative_kwh={cumulative_kwh:.1f} avg_pmv={pmv_str}")

        # Decide the schedule for the NEXT chunk based on what just happened.
        trailing = successful_chunk_dirs[-LLM_CONTEXT_WINDOW:]
        summary = summarize_chunks(trailing)
        result = propose_setpoint_adjustment(summary)

        if isinstance(result, RejectedProposal):
            print(f"[VALIDATION] REJECT (schema) reason={result.reason!r} -- keeping last valid schedule")
            continue

        proposal: Proposal = result
        print(f"[LLM_REASONING] {proposal.reasoning!r}")
        print(f"[PROPOSED_ACTION] field={proposal.field} direction={proposal.direction} "
              f"magnitude={proposal.magnitude} window={proposal.window!r}")

        if chunk_pmv is None:
            print("[VALIDATION] REJECT (no PMV data available to validate against) -- keeping last valid schedule")
            continue

        IDF.setiddname(str(IDD_PATH))
        idf_for_read = IDF(str(CURRENT_IDF))
        current_setpoint = get_current_setpoint_celsius(idf_for_read, proposal.field)
        other_field_current = get_current_setpoint_celsius(idf_for_read, OTHER_FIELD[proposal.field])
        field_baseline = field_baselines[proposal.field]

        validation_result = validate_proposal(
            proposal, current_setpoint, chunk_pmv, other_field_current, field_baseline
        )
        if isinstance(validation_result, RejectedAction):
            print(f"[VALIDATION] REJECT reason={validation_result.reason} -- keeping last valid schedule")
            continue

        action: ValidatedAction = validation_result
        print(f"[VALIDATION] PASS clamped={action.clamped}")
        apply_action(idf_for_read, action)
        idf_for_read.save()
        print(f"[APPLIED_ACTION] {action.field} -> {action.value_celsius}C "
              f"(takes effect starting chunk {i + 1})")

    print(f"\n[LOOP_END] total_kwh={cumulative_kwh:.1f} "
          f"chunks_completed={len(successful_chunk_dirs)}/{len(chunks)}")


if __name__ == "__main__":
    main()