#!/usr/bin/env python3
"""Explicit, pinned skill installation. No hooks, auth, MCP or global config edits."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "budded-water/agent-skills"
MARKER = ".agent-skills-installation.json"
NOTICES = ("LICENSE", "NOTICE.md")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def hashes(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {path}")
        if path.is_file() and path.name != MARKER:
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def install(target, names, commit, apply=False):
    if git("rev-parse", "HEAD") != commit or len(commit) != 40:
        raise ValueError("Source must be at the requested full commit")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Source checkout must be clean, including untracked files")
    # Do not resolve away a symlink supplied by the caller before checking it.
    target = target.absolute()
    for parent in (target, *target.parents):
        if parent.is_symlink():
            raise ValueError(f"Refusing symlinked target path: {parent}")
    available = {p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file()}
    if not names or len(set(names)) != len(names) or not set(names) <= available:
        raise ValueError("Select distinct packaged skill names explicitly")
    plan = []
    for name in names:
        source, destination = ROOT / "skills" / name, target / name
        digest = hashes(source)
        for notice in NOTICES:
            path = ROOT / notice
            if path.is_symlink():
                raise ValueError(f"Refusing symlinked notice: {path}")
            digest[notice] = hashlib.sha256(path.read_bytes()).hexdigest()
        marker = {"package": PACKAGE, "sourceCommit": commit, "skill": name, "sha256": digest}
        if destination.is_symlink():
            raise ValueError(f"Refusing existing symlink: {destination}")
        if destination.exists():
            existing = json.loads((destination / MARKER).read_text())
            if existing != marker or hashes(destination) != digest:
                raise ValueError(f"Refusing overwrite of different or modified installation: {destination}")
            plan.append((name, "unchanged", marker))
        else:
            plan.append((name, "install", marker))
    # Validate the whole selection before any writes. Existing copies are never overwritten.
    if apply:
        target.mkdir(parents=True, exist_ok=True)
        lock = target / ".agent-skills-install.lock"
        lock.mkdir()  # Concurrent installers fail rather than race.
        created = []
        try:
            for name, action, marker in plan:
                if action == "unchanged":
                    continue
                with tempfile.TemporaryDirectory(prefix=".agent-skills-stage-", dir=target) as stage:
                    copied = Path(stage) / name
                    shutil.copytree(ROOT / "skills" / name, copied)
                    for notice in NOTICES:
                        shutil.copyfile(ROOT / notice, copied / notice)
                    (copied / MARKER).write_text(json.dumps(marker, indent=2) + "\n")
                    destination = target / name
                    if destination.exists() or destination.is_symlink():
                        raise ValueError(f"Destination appeared during installation: {destination}")
                    copied.rename(destination)
                    created.append(destination)
        except BaseException:
            for path in reversed(created):
                shutil.rmtree(path)
            raise
        finally:
            lock.rmdir()
    return [{"skill": name, "action": action, "applied": apply} for name, action, _ in plan]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--skill", action="append", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--apply", action="store_true", help="Default is a read-only installation plan")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.target, args.skill, args.source_commit, args.apply), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Installation refused: {error}\n")


if __name__ == "__main__":
    main()
