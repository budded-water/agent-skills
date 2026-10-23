#!/usr/bin/env python3
"""Verify recorded local package integrity; never fetch, refresh or change auth."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

PACKAGE = "budded-water/agent-skills"
MARKER = ".agent-skills-installation.json"
REVIEW_SKILL = "requesting-slack-bot-review"
DEPENDENCY = "pr-review-preflight"


def verify(directory, name, expected_commit=None):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Expected a regular installed directory: {directory}")
    marker = directory / MARKER
    if marker.is_symlink():
        raise ValueError("Installation marker must not be a symlink")
    record = json.loads(marker.read_text())
    if not isinstance(record, dict) or record.get("package") != PACKAGE or record.get("skill") != name:
        raise ValueError(f"Not an agent-skills installation of {name}")
    commit = record.get("sourceCommit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Installation has no full source commit")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError(f"Package pin mismatch for {name}")
    digests, executable = record.get("sha256"), record.get("executable")
    if not isinstance(digests, dict) or not isinstance(executable, dict):
        raise ValueError("Installation must record file hashes and executable flags")
    if not {"SKILL.md", "LICENSE", "NOTICE.md"} <= digests.keys() or digests.keys() != executable.keys():
        raise ValueError("Installation file manifest is incomplete")
    for relative, digest in digests.items():
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or ".." in path.parts or str(path) != relative
                or relative == MARKER or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(executable[relative]) is not bool):
            raise ValueError("Invalid installation file manifest")
    actual = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink: {path}")
        if path.is_file() and path != marker:
            relative = path.relative_to(directory).as_posix()
            actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            if relative in executable and bool(path.stat().st_mode & 0o100) != executable[relative]:
                raise ValueError(f"Executable permission differs: {relative}")
    if actual != digests:
        raise ValueError(f"Installed content differs from recorded package: {name}")
    return commit


def check(directory, expected_commit=None):
    commit = verify(directory, REVIEW_SKILL, expected_commit)
    verify(directory.parent / DEPENDENCY, DEPENDENCY, commit)
    return {"package": PACKAGE, "sourceCommit": commit, "skills": [REVIEW_SKILL, DEPENDENCY],
            "integrity": "matches_recorded_installation", "remote_freshness": "not_checked"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--expected-commit", help="Trusted full reviewed package commit, when supplied by the caller")
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.skill_dir.absolute(), args.expected_commit), indent=2))
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Installation verification refused: {error}. Stage both review skills from one clean pinned agent-skills checkout; do not refresh during a review.\n")


if __name__ == "__main__":
    main()
