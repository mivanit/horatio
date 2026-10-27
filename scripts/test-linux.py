"""Run the existing Linux suite against current source, without local state."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    # Include edits and new source files, but not ignored configs/logs/secrets.
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
    ).split(b"\0")
    with tempfile.TemporaryDirectory(prefix="horatio-tests-") as directory:
        snapshot = Path(directory)
        for name in files:
            if not name:
                continue
            relative = Path(os.fsdecode(name))
            # Never copy live state, even if it was accidentally added to Git.
            if relative.parts[0] in {"logs", "config.json", ".password", ".venv"}:
                continue
            if relative == Path("collector/health-config.json"):
                continue
            source = root / relative
            if not source.is_file():  # Includes tracked files deleted locally.
                continue
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        shutil.copy2(root / "config.example.json", snapshot / "config.json")
        print(f"Running Linux tests in {snapshot}", flush=True)
        return subprocess.run([sys.executable, "test.py"], cwd=snapshot).returncode


if __name__ == "__main__":
    sys.exit(main())
