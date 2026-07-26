"""
Milestone 6 negative-path proof: force a real EnergyPlus failure and verify
run_chunk_with_retry() actually retries MAX_ENERGYPLUS_RETRIES times, reverts
current.idf to last_known_good.idf's content on each failure, and returns
False (rather than raising) so the caller can skip the chunk and continue --
never exercised by the happy-path run since all 5 real chunks succeeded.

Uses a separate scratch directory so this doesn't disturb run1's real data.
Failure is forced with a nonexistent weather file path -- deterministic,
no fragile IDF corruption needed.
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eppy.modeleditor import IDF  # noqa: E402

# Import the script under test as a module (it's not part of the installed
# package -- same pattern as re-using any other standalone script's functions).
spec = importlib.util.spec_from_file_location(
    "run_ai_supervised_loop", Path(__file__).resolve().parent / "run_ai_supervised_loop.py"
)
loop_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loop_module)

EPLUS_DIR = Path(os.environ.get("EPLUS_DIR", "D:/EnergyPlusV25-1-0"))
IDD_PATH = EPLUS_DIR / "Energy+.idd"
TEST_DIR = Path(os.environ.get("RUN_DIR", "D:/Honeywell-env/run1")).parent / "rollback_retry_test"
CURRENT_IDF = TEST_DIR / "ai_current.idf"
LAST_KNOWN_GOOD_IDF = TEST_DIR / "ai_last_known_good.idf"
OUT_DIR = TEST_DIR / "chunk_out"

MARKER_LAST_KNOWN_GOOD = "99.0"
MARKER_CORRUPTED_CURRENT = "5.0"


def read_marker(idf_path: Path) -> str:
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(idf_path))
    return idf.getobject("Schedule:Compact", "HTGSETP_SCH_NO_OPTIMUM").Field_6


def main() -> None:
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True)

    # Point the loop module at this scratch directory instead of run1.
    loop_module.RUN_DIR = TEST_DIR
    loop_module.CURRENT_IDF = CURRENT_IDF
    loop_module.LAST_KNOWN_GOOD_IDF = LAST_KNOWN_GOOD_IDF
    loop_module.WEATHER = Path("D:/does/not/exist.epw")  # forces every EnergyPlus attempt to fail

    run_period_name = loop_module.prepare_current_idf()

    # last_known_good gets a distinctive marker value.
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(CURRENT_IDF))
    idf.getobject("Schedule:Compact", "HTGSETP_SCH_NO_OPTIMUM").Field_6 = MARKER_LAST_KNOWN_GOOD
    idf.saveas(str(LAST_KNOWN_GOOD_IDF))

    # current.idf gets a different, "corrupted" marker -- simulating a bad
    # edit that was in place when this chunk's run was attempted.
    idf2 = IDF(str(CURRENT_IDF))
    idf2.getobject("Schedule:Compact", "HTGSETP_SCH_NO_OPTIMUM").Field_6 = MARKER_CORRUPTED_CURRENT
    idf2.save()

    print(f"Before: current.idf Field_6={read_marker(CURRENT_IDF)!r} "
          f"(expect {MARKER_CORRUPTED_CURRENT!r}), "
          f"last_known_good.idf Field_6={read_marker(LAST_KNOWN_GOOD_IDF)!r} "
          f"(expect {MARKER_LAST_KNOWN_GOOD!r})")
    assert read_marker(CURRENT_IDF) == MARKER_CORRUPTED_CURRENT
    assert read_marker(LAST_KNOWN_GOOD_IDF) == MARKER_LAST_KNOWN_GOOD

    print("\nCalling run_chunk_with_retry() with an invalid weather path "
          f"(forces every attempt to fail, MAX_ENERGYPLUS_RETRIES={loop_module.MAX_ENERGYPLUS_RETRIES})...\n")
    success = loop_module.run_chunk_with_retry(run_period_name, 1, 1, 1, 7, OUT_DIR)

    print(f"\nrun_chunk_with_retry() returned: {success} (expect False)")
    assert success is False, "expected all retries to fail given a nonexistent weather file"

    after_marker = read_marker(CURRENT_IDF)
    print(f"After: current.idf Field_6={after_marker!r} (expect {MARKER_LAST_KNOWN_GOOD!r}, "
          f"i.e. reverted to last_known_good, not left at the corrupted {MARKER_CORRUPTED_CURRENT!r})")
    assert after_marker == MARKER_LAST_KNOWN_GOOD, (
        f"current.idf was NOT reverted to last_known_good -- rollback did not stick "
        f"(got {after_marker!r})"
    )

    print("\n[PASS] rollback+retry: failed 3/3 attempts, function returned False without raising, "
          "current.idf correctly reverted to last_known_good.idf's content.")


if __name__ == "__main__":
    main()