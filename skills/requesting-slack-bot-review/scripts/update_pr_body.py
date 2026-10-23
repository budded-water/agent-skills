#!/usr/bin/env python3
"""Update a PR body as JSON data after verifying its current head."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEAD_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def remote_head(repo: str, pr: int) -> str:
    result = subprocess.run(
        [
            "gh", "pr", "view", str(pr), "--repo", repo,
            "--json", "headRefOid", "--jq", ".headRefOid",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def update(repo: str, pr: int, expected_head: str, body: str) -> str:
    actual = remote_head(repo, pr)
    if actual.casefold() != expected_head.casefold():
        raise RuntimeError(
            f"remote head mismatch: expected {expected_head}, actual {actual}"
        )
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr}", "--method", "PATCH", "--input", "-"],
        input=json.dumps({"body": body}, ensure_ascii=False),
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def display_output(output: str) -> str:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return output.strip()
    return str(payload.get("html_url") or payload.get("url") or output.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument(
        "--body-file", required=True,
        help="UTF-8 Markdown file, or - to read the body from stdin",
    )
    args = parser.parse_args()
    if not REPO_RE.fullmatch(args.repo):
        parser.error("--repo must be owner/name")
    if args.pr <= 0:
        parser.error("--pr must be positive")
    if not HEAD_RE.fullmatch(args.expected_head):
        parser.error("--expected-head must be a full 40-character SHA")
    return args


def main() -> int:
    args = parse_args()
    body = (
        sys.stdin.read()
        if args.body_file == "-"
        else Path(args.body_file).read_text(encoding="utf-8")
    )
    try:
        output = update(args.repo, args.pr, args.expected_head, body)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(display_output(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
