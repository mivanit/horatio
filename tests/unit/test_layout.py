"""Moving source must not relocate an installed instance's assets or state."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_source_layout_preserves_root_state_from_another_working_directory(
    app_dir: Path,
    tmp_path: Path,
) -> None:
    import server

    config = json.loads((app_dir / "config.json").read_text())
    config["title"] = "Existing installation"
    (app_dir / "config.json").write_text(json.dumps(config))
    (app_dir / ".password").write_text(server._hash_password("fixture-password"))
    (app_dir / "logs").mkdir(exist_ok=True)
    (app_dir / "logs" / "history.jsonl").write_text("existing history\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.json").write_text('{"title":"wrong configuration"}')
    code = """
import json, sys
sys.path.insert(0, sys.argv[1])
import server
print(json.dumps({
    "title": server._CONFIG["title"],
    "password": server._verify_password("fixture-password"),
    "history": server.HISTORY_FILE.read_text(),
    "version": server.VERSION,
    "index": server.INDEX_FILE.is_file(),
    "static": server.STATIC_DIR.is_dir(),
    "themes": server.THEMES_DIR.is_dir(),
    "tui": (server.SOURCE_DIR / "tui.py").is_file(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(app_dir / "python")],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "title": "Existing installation",
        "password": True,
        "history": "existing history\n",
        "version": (app_dir / "VERSION").read_text().strip(),
        "index": True,
        "static": True,
        "themes": True,
        "tui": True,
    }
    assert not (app_dir / "python" / "logs").exists()
    assert not (app_dir / "python" / "config.json").exists()
