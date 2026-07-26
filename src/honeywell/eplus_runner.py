"""Subprocess wrapper around energyplus.exe. Timeout-guarded per Section 4 --
this is the field/execution level: it runs what it's told and reports what
happened. No retry, no rollback -- that's the validation layer, built later."""
import shutil
import subprocess
from pathlib import Path


class EnergyPlusTimeout(Exception):
    pass


class EnergyPlusFailed(Exception):
    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"EnergyPlus exited with code {returncode}")


def run_energyplus(eplus_exe: Path, idf_path: Path, weather_path: Path,
                    out_dir: Path, cwd: Path, timeout_s: int = 120) -> Path:
    if out_dir.exists():
        shutil.rmtree(out_dir)

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
        raise EnergyPlusFailed(result.returncode, result.stderr)

    return out_dir