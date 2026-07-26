"""
Milestone 4 proof: the LLM reasoning step, in isolation, against real chunk
data from Milestone 3. Not wired into the orchestration loop -- no clamping,
no whitelisting, no IDF application. This only proves the model can reliably
produce a structured decision object (or a clean, explicit rejection) from
real chunk metrics.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from honeywell.llm_reasoning import Proposal, RejectedProposal, propose_setpoint_adjustment  # noqa: E402
from honeywell.window_summary import summarize_chunks  # noqa: E402

CHUNKS_DIR = Path("D:/Honeywell-env/run1/chunks")

# A few different real windows, not synthetic data: last 2 chunks, last 3, all 5.
TEST_WINDOWS = {
    "last_2_chunks": [CHUNKS_DIR / "chunk_04", CHUNKS_DIR / "chunk_05"],
    "last_3_chunks": [CHUNKS_DIR / "chunk_03", CHUNKS_DIR / "chunk_04", CHUNKS_DIR / "chunk_05"],
    "all_5_chunks": [CHUNKS_DIR / f"chunk_{i:02d}" for i in range(1, 6)],
}


def main() -> None:
    for name, chunk_dirs in TEST_WINDOWS.items():
        missing = [d for d in chunk_dirs if not d.exists()]
        if missing:
            print(f"[SKIP] {name}: missing chunk dirs {missing} -- run scripts/run_chunked_loop.py first")
            continue

        summary = summarize_chunks(chunk_dirs)
        print(f"\n=== {name} ===")
        print(summary)

        result = propose_setpoint_adjustment(summary)

        if isinstance(result, Proposal):
            print(f"[PROPOSAL] field={result.field} value_celsius={result.value_celsius} "
                  f"window={result.window!r}")
            print(f"[REASONING] {result.reasoning}")
        elif isinstance(result, RejectedProposal):
            print(f"[REJECTED] reason={result.reason}")
            print(f"[RAW] {result.raw_response!r}")
        else:
            raise AssertionError(f"unexpected return type: {type(result)}")


if __name__ == "__main__":
    main()