import json
import subprocess
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "workspace_probe.py"


def test_probe_confirms_writable_directory_without_leaving_artifacts(tmp_path):
    before = set(tmp_path.iterdir())
    result = subprocess.run(
        [str(SCRIPT), "--path", str(tmp_path)], text=True, capture_output=True
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["writable"] is True
    assert set(tmp_path.iterdir()) == before


def test_probe_fails_for_missing_directory(tmp_path):
    result = subprocess.run(
        [str(SCRIPT), "--path", str(tmp_path / "missing")], text=True, capture_output=True
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["writable"] is False
