import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/requesting-slack-bot-review"
PIN = "a" * 40


def record(directory, pin=PIN):
    marker = directory / ".agent-skills-installation.json"
    files = [p for p in directory.rglob("*") if p.is_file() and p != marker]
    marker.write_text(json.dumps({
        "package": "budded-water/agent-skills", "sourceCommit": pin, "skill": directory.name,
        "sha256": {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "executable": {p.relative_to(directory).as_posix(): bool(p.stat().st_mode & 0o100) for p in files},
    }))


def stage_pair(tmp_path, generic="#!/bin/sh\nexit 0\n"):
    target = tmp_path / "installed"
    for name in ("requesting-slack-bot-review", "pr-review-preflight"):
        directory = target / name
        shutil.copytree(ROOT / "skills" / name, directory)
        for notice in ("LICENSE", "NOTICE.md"):
            shutil.copyfile(ROOT / notice, directory / notice)
        if name == "pr-review-preflight":
            helper = directory / "preflight.sh"
            helper.write_text(generic)
            helper.chmod(0o755)
        record(directory)
    return target / "requesting-slack-bot-review"
