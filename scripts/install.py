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


def hashes(directory, *, skip_marker=False):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {path}")
        if path.is_file() and not (skip_marker and path == directory / MARKER):
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def pinned_files(commit, *paths):
    entries = subprocess.check_output([
        "git", "-C", str(ROOT), "ls-tree", "-rz", "--full-tree", commit, "--", *paths,
    ])
    result = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("Only regular files can be installed from the pinned commit")
        content = subprocess.check_output(["git", "-C", str(ROOT), "cat-file", "blob", oid])
        result[path.decode("utf-8")] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "executable": mode == "100755",
        }
    return result


def executable_flags(directory, names):
    return {name: bool((directory / name).stat().st_mode & 0o100) for name in names}


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
        if source.is_symlink():
            raise ValueError(f"Refusing symlinked source: {source}")
        prefix = f"skills/{name}/"
        expected = pinned_files(commit, prefix)
        digest = {path.removeprefix(prefix): value["sha256"] for path, value in expected.items()}
        executable = {path.removeprefix(prefix): value["executable"] for path, value in expected.items()}
        if ("SKILL.md" not in digest or hashes(source) != digest
                or executable_flags(source, digest) != executable):
            raise ValueError(f"Source files differ from the pinned commit: {name}")
        if set(digest) & {MARKER, *NOTICES}:
            raise ValueError(f"Reserved installation filename in skill: {name}")
        expected_notices = pinned_files(commit, *NOTICES)
        for notice in NOTICES:
            path = ROOT / notice
            if path.is_symlink():
                raise ValueError(f"Refusing symlinked notice: {path}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if (actual != expected_notices.get(notice, {}).get("sha256")
                    or executable_flags(ROOT, [notice])[notice]
                    != expected_notices.get(notice, {}).get("executable")):
                raise ValueError(f"Notice differs from the pinned commit: {notice}")
            digest[notice] = actual
            executable[notice] = expected_notices[notice]["executable"]
        marker = {"package": PACKAGE, "sourceCommit": commit, "skill": name, "sha256": digest, "executable": executable}
        if destination.is_symlink():
            raise ValueError(f"Refusing existing symlink: {destination}")
        if destination.exists():
            existing = json.loads((destination / MARKER).read_text())
            if (existing != marker or hashes(destination, skip_marker=True) != digest
                    or executable_flags(destination, digest) != executable):
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
                    if (hashes(copied) != marker["sha256"]
                            or executable_flags(copied, marker["sha256"]) != marker["executable"]):
                        raise ValueError(f"Staged files differ from the pinned commit: {name}")
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
