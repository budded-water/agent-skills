import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in ("first", "second"):
        path = root / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"skill {name}\n")
        helper = path / "helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
    for notice in installer.NOTICES:
        (root / notice).write_text(f"fixture {notice}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "skills", *installer.NOTICES], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)
    monkeypatch.setattr(installer, "ROOT", root)
    return root, installer.git("rev-parse", "HEAD"), tmp_path / "target"


def test_plan_has_no_writes_and_install_records_pin(source):
    root, commit, target = source
    assert installer.install(target, ["first"], commit)[0]["action"] == "install"
    assert not target.exists()
    installer.install(target, ["first"], commit, True)
    record = json.loads((target / "first" / installer.MARKER).read_text())
    assert record["sourceCommit"] == commit
    for notice in installer.NOTICES:
        assert (target / "first" / notice).read_bytes() == (root / notice).read_bytes()
        assert notice in record["sha256"]
    assert (target / "first/SKILL.md").read_bytes() == (root / "skills/first/SKILL.md").read_bytes()
    assert installer.install(target, ["first"], commit, True)[0]["action"] == "unchanged"


def test_modified_install_refused_before_other_skill_writes(source):
    _, commit, target = source
    installer.install(target, ["second"], commit, True)
    (target / "second/SKILL.md").write_text("local edit")
    with pytest.raises(ValueError, match="overwrite"):
        installer.install(target, ["first", "second"], commit, True)
    assert not (target / "first").exists()
    assert (target / "second/SKILL.md").read_text() == "local edit"


def test_foreign_directory_preserved(source):
    _, commit, target = source
    (target / "first").mkdir(parents=True)
    (target / "first/keep").write_text("foreign")
    with pytest.raises(OSError):
        installer.install(target, ["first"], commit, True)
    assert (target / "first/keep").read_text() == "foreign"


def test_symlinked_target_and_destination_refused(source):
    root, commit, target = source
    target.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.install(target, ["first"], commit, True)
    target.unlink()
    target.mkdir()
    (target / "first").symlink_to(root / "skills/first", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.install(target, ["first"], commit, True)


def test_wrong_pin_dirty_source_and_unknown_selection_refused(source):
    root, commit, target = source
    with pytest.raises(ValueError, match="commit"):
        installer.install(target, ["first"], "0" * 40, True)
    with pytest.raises(ValueError, match="Select"):
        installer.install(target, ["../first"], commit, True)
    (root / "untracked").write_text("pending edit")
    with pytest.raises(ValueError, match="clean"):
        installer.install(target, ["first"], commit, True)
    assert not target.exists()


def test_partial_install_rolls_back_on_copy_failure(source, monkeypatch):
    _, commit, target = source
    original = installer.shutil.copytree

    def copy(source, destination):
        if source.name == "second":
            raise OSError("fixture disk failure")
        return original(source, destination)

    monkeypatch.setattr(installer.shutil, "copytree", copy)
    with pytest.raises(OSError, match="disk failure"):
        installer.install(target, ["first", "second"], commit, True)
    assert list(target.iterdir()) == []


def test_active_install_lock_is_preserved(source):
    _, commit, target = source
    (target / ".agent-skills-install.lock").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        installer.install(target, ["first"], commit, True)
    assert (target / ".agent-skills-install.lock").is_dir()
    assert not (target / "first").exists()



def test_ignored_extra_file_is_not_part_of_the_pin(source):
    root, commit, target = source
    (root / ".git/info/exclude").write_text("*.pyc\n")
    (root / "skills/first/extra.pyc").write_bytes(b"not in reviewed commit")
    assert installer.git("status", "--porcelain") == ""
    with pytest.raises(ValueError, match="pinned commit"):
        installer.install(target, ["first"], commit, True)
    assert not target.exists()


@pytest.mark.parametrize("relative", ["skills/first/SKILL.md", "LICENSE", "NOTICE.md"])
def test_hidden_tracked_edit_does_not_bypass_the_pin(source, relative):
    root, commit, target = source
    subprocess.run(["git", "-C", str(root), "update-index", "--assume-unchanged", relative], check=True)
    (root / relative).write_text("different content")
    assert installer.git("status", "--porcelain") == ""
    with pytest.raises(ValueError, match="pinned commit"):
        installer.install(target, ["first"], commit, True)
    assert not target.exists()


def test_source_changes_during_copy_roll_back_entire_batch(source, monkeypatch):
    _, commit, target = source
    original = installer.shutil.copytree

    def copy(source, destination):
        copied = original(source, destination)
        if source.name == "second":
            (destination / "SKILL.md").write_text("changed during copy")
        return copied

    monkeypatch.setattr(installer.shutil, "copytree", copy)
    with pytest.raises(ValueError, match="pinned commit"):
        installer.install(target, ["first", "second"], commit, True)
    assert list(target.iterdir()) == []



def test_installed_helper_losing_execute_permission_is_not_valid(source):
    _, commit, target = source
    installer.install(target, ["first"], commit, True)
    helper = target / "first/helper.sh"
    assert subprocess.run([str(helper)]).returncode == 0
    helper.chmod(0o644)
    with pytest.raises(ValueError, match="overwrite"):
        installer.install(target, ["first"], commit, True)


def test_hidden_source_executable_change_is_refused(source):
    root, commit, target = source
    subprocess.run(["git", "-C", str(root), "config", "core.filemode", "false"], check=True)
    (root / "skills/first/helper.sh").chmod(0o644)
    assert installer.git("status", "--porcelain") == ""
    with pytest.raises(ValueError, match="pinned commit"):
        installer.install(target, ["first"], commit, True)
    assert not target.exists()


def test_staged_executable_change_rolls_back(source, monkeypatch):
    _, commit, target = source
    original = installer.shutil.copytree

    def copy(source, destination):
        copied = original(source, destination)
        if source.name == "second":
            (destination / "helper.sh").chmod(0o644)
        return copied

    monkeypatch.setattr(installer.shutil, "copytree", copy)
    with pytest.raises(ValueError, match="pinned commit"):
        installer.install(target, ["first", "second"], commit, True)
    assert list(target.iterdir()) == []
