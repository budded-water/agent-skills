import json
import os
import subprocess
import sys

import pytest

from .helpers import PIN, stage_pair


def check(skill, *args, env=None):
    return subprocess.run([sys.executable, str(skill / "check_installation.py"), *args],
                          cwd=skill.parent, env=env, capture_output=True, text=True)


def test_installed_pair_verifies_outside_source_checkout_without_network(tmp_path):
    skill = stage_pair(tmp_path)
    blocked = tmp_path / "bin"
    blocked.mkdir()
    for command in ("git", "gh", "curl", "node"):
        path = blocked / command
        path.write_text("#!/bin/sh\necho 'unexpected external command' >&2\nexit 99\n")
        path.chmod(0o755)
    result = check(skill, "--expected-commit", PIN, env={**os.environ, "PATH": str(blocked)})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["remote_freshness"] == "not_checked"


@pytest.mark.parametrize("mutation", ["content", "extra", "permission", "symlink", "missing_dependency", "dependency_pin", "foreign_package", "malformed_marker"])
def test_modified_or_incompatible_installation_is_refused(tmp_path, mutation):
    skill = stage_pair(tmp_path)
    if mutation == "content":
        (skill / "scripts/request_guard.py").write_text("unreviewed change")
    elif mutation == "extra":
        (skill / "unexpected.py").write_text("extra executable content")
    elif mutation == "permission":
        (skill.parent / "pr-review-preflight/preflight.sh").chmod(0o644)
    elif mutation == "symlink":
        (skill / "linked-file").symlink_to(skill / "SKILL.md")
    elif mutation == "missing_dependency":
        (skill.parent / "pr-review-preflight").rename(skill.parent / "saved-preflight")
    else:
        marker = (skill.parent / "pr-review-preflight" if mutation == "dependency_pin" else skill) / ".agent-skills-installation.json"
        data = json.loads(marker.read_text())
        if mutation == "dependency_pin": data["sourceCommit"] = "b" * 40
        elif mutation == "foreign_package": data["package"] = "other/package"
        else: data = []
        marker.write_text(json.dumps(data))
    result = check(skill)
    assert result.returncode == 2
    assert "Installation verification refused" in result.stderr
    assert "Traceback" not in result.stderr


def test_trusted_caller_pin_mismatch_and_refresh_do_not_modify_installation(tmp_path):
    skill = stage_pair(tmp_path)
    before = {str(p): p.read_bytes() for p in skill.parent.rglob("*") if p.is_file()}
    assert check(skill, "--expected-commit", "b" * 40).returncode == 2
    assert check(skill, "--refresh").returncode == 2
    assert before == {str(p): p.read_bytes() for p in skill.parent.rglob("*") if p.is_file()}
