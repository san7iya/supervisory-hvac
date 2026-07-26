"""Subprocess wrapper around energyplus.exe. Timeout-guarded per Section 4 --
this is the field/execution level: it runs what it's told and reports what
happened. No retry, no rollback -- that's the validation layer, built later."""
import shutil
import subprocess
from pathlib import Path


class EnergyPlusTimeout(Exception):
    pass


class EnergyPlusFailed(Exception):
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        # Surface the tail of stdout in the message itself -- EnergyPlus writes
        # its Fatal/Severe error summary to the console (stdout), not just to
        # eplusout.err, so this is usually enough to see what happened without
        # digging into the preserved output directory.
        tail = "\n".join(stdout.strip().splitlines()[-15:]) if stdout and stdout.strip() else "(no stdout captured)"
        stderr_note = f"\nstderr: {stderr.strip()}" if stderr and stderr.strip() else ""
        super().__init__(f"EnergyPlus exited with code {returncode}\n--- stdout tail ---\n{tail}{stderr_note}")


def _preserve_failed_attempt(out_dir: Path) -> None:
    """If out_dir exists (left behind by a prior failed attempt in the same
    retry loop), rename it instead of deleting it, so its eplusout.err and
    other diagnostics survive the next attempt. Auto-numbered so each
    retry's failure gets its own preserved copy."""
    if not out_dir.exists():
        return
    n = 1
    while True:
        preserved = out_dir.parent / f"{out_dir.name}_attempt_{n}_failed"
        if not preserved.exists():
            break
        n += 1
    shutil.move(str(out_dir), str(preserved))


def run_energyplus(eplus_exe: Path, idf_path: Path, weather_path: Path,
                    out_dir: Path, cwd: Path, timeout_s: int = 120) -> Path:
    _preserve_failed_attempt(out_dir)

    try:
        result = subprocess.run(
            [str(eplus_exe), "-w", str(weather_path), "-r", "-d", str(out_dir), str(idf_path)],
            cwd=cwd,
            capture_output=True,
            timeout=timeout_s,
            text=True,
        )
    except subprocess.TimeoutExpired as e:
        raise EnergyPlusTimeout(f"EnergyPlus exceeded {timeout_s}s timeout") from e

    if result.returncode != 0:
        raise EnergyPlusFailed(result.returncode, result.stdout, result.stderr)

    return out_dir