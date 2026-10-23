#!/usr/bin/env python3
"""Build and validate a canonical watcher snapshot from MCP-normalized JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


COLLECTIONS = ("slack_messages", "github_reviews", "github_comments", "review_threads")
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
VERDICTS = {"approved", "with_findings"}
REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
ARTIFACT_STATES = {"OPEN", "MERGED", "CLOSED"}
REQUIRED_GITHUB_REVIEW_STATES = {"APPROVED"}


def same_identity(actual: str | None, expected: str | None) -> bool:
    return bool(actual and expected and actual.casefold() == expected.casefold())


def read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def require_string(item: dict[str, Any], field: str, collection: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{collection}.{field} must be a non-empty string")
    return value


def validate_collection(
    name: str, value: Any, request: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be a JSON array of objects")

    accepted: list[dict[str, Any]] = []
    warnings: list[str] = []
    github_reviewer = request.get("github_reviewer")
    slack_reviewer = request.get("slack_reviewer")

    def drop(index: int, reason: str) -> None:
        warnings.append(f"dropped {name}[{index}]: {reason}")

    for index, item in enumerate(value):
        if name == "slack_messages":
            user = item.get("user")
            if not isinstance(user, str) or not user:
                drop(index, "cannot attribute message to requested reviewer")
                continue
            if not same_identity(user, slack_reviewer):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text:
                drop(index, "reviewer message has no classifiable text")
                continue
            if "acknowledgement" in item and not isinstance(item["acknowledgement"], bool):
                raise ValueError("slack_messages.acknowledgement must be boolean")
        elif name == "github_reviews":
            user = item.get("user")
            if not isinstance(user, str) or not user:
                drop(index, "cannot attribute review to requested reviewer")
                continue
            if not same_identity(user, github_reviewer):
                continue
            commit_id = item.get("commit_id")
            if not isinstance(commit_id, str) or not commit_id:
                drop(index, "review has no revision identity")
                continue
            if not FULL_SHA.fullmatch(commit_id):
                drop(index, "review commit_id is not a full commit SHA")
                continue
            state = require_string(item, "state", name).upper()
            if state not in REVIEW_STATES:
                raise ValueError(f"github_reviews.state is invalid: {state}")
            if commit_id.casefold() == str(request.get("head") or "").casefold():
                review_url = item.get("html_url")
                if not isinstance(review_url, str) or not review_url.strip():
                    raise ValueError(
                        "exact-head github_reviews.html_url must be a non-empty string"
                    )
                body = item.get("body")
                if state == "APPROVED" and (
                    not isinstance(body, str) or not body.strip()
                ):
                    raise ValueError(
                        "exact-head github_reviews.body must be a non-empty string "
                        "for APPROVED state"
                    )
        elif name == "github_comments":
            user = item.get("user")
            if not isinstance(user, str) or not user:
                drop(index, "cannot attribute comment to requested reviewer")
                continue
            if not same_identity(user, github_reviewer):
                continue
            body = item.get("body")
            if not isinstance(body, str) or not body:
                drop(index, "reviewer comment has no classifiable body")
                continue
        elif name == "review_threads":
            user = item.get("user")
            if not isinstance(user, str) or not user:
                drop(index, "cannot attribute thread to requested reviewer")
                continue
            if not same_identity(user, github_reviewer):
                continue
            commit_id = item.get("commit_id")
            if not isinstance(commit_id, str) or not commit_id:
                drop(index, "thread has no revision identity")
                continue
            if not FULL_SHA.fullmatch(commit_id):
                drop(index, "thread commit_id is not a full commit SHA")
                continue
            for field in ("is_resolved", "is_outdated"):
                if not isinstance(item.get(field), bool):
                    raise ValueError(f"review_threads.{field} must be boolean")
        accepted.append(item)
    return accepted, warnings


def build(request: dict[str, Any], collections: dict[str, Any]) -> dict[str, Any]:
    head = request.get("head")
    remote_head = request.get("remote_head")
    if not isinstance(head, str) or not FULL_SHA.fullmatch(head):
        raise ValueError("request.head must be a full 40-character hexadecimal commit SHA")
    if not isinstance(remote_head, str) or not FULL_SHA.fullmatch(remote_head):
        raise ValueError("request.remote_head must be a full 40-character hexadecimal commit SHA")
    artifact_state = str(request.get("artifact_state") or "").upper()
    if artifact_state not in ARTIFACT_STATES:
        raise ValueError("request.artifact_state must be OPEN, MERGED, or CLOSED")
    merge_commit_sha = request.get("merge_commit_sha")
    if merge_commit_sha is not None and (
        not isinstance(merge_commit_sha, str) or not FULL_SHA.fullmatch(merge_commit_sha)
    ):
        raise ValueError("request.merge_commit_sha must be null or a full commit SHA")
    ledger = request.get("expected_ledger", "github_review")
    if ledger not in {"github_review", "slack", "either"}:
        raise ValueError("request.expected_ledger is invalid")
    if ledger in {"github_review", "either"} and not request.get("github_reviewer"):
        raise ValueError(f"{ledger} ledger requires request.github_reviewer")
    required_github_state = request.get("required_github_review_state")
    if required_github_state is not None:
        if not isinstance(required_github_state, str):
            raise ValueError("request.required_github_review_state must be APPROVED")
        required_github_state = required_github_state.upper()
        if required_github_state not in REQUIRED_GITHUB_REVIEW_STATES:
            raise ValueError("request.required_github_review_state must be APPROVED")
        if ledger not in {"github_review", "either"}:
            raise ValueError(
                "request.required_github_review_state requires a GitHub-capable ledger"
            )
    if ledger in {"slack", "either"} and (
        not isinstance(request.get("slack_reviewer"), str)
        or not request.get("slack_reviewer")
    ):
        raise ValueError(f"{ledger} ledger requires request.slack_reviewer")
    acceptable = request.get("acceptable_verdicts", ["approved", "with_findings"])
    if not isinstance(acceptable, list) or not acceptable or any(item not in VERDICTS for item in acceptable):
        raise ValueError("request.acceptable_verdicts must be a non-empty subset of approved/with_findings")

    normalized_request = dict(request)
    normalized_request["expected_ledger"] = ledger
    normalized_request["head"] = head.lower()
    normalized_request["remote_head"] = remote_head.lower()
    normalized_request["artifact_state"] = artifact_state
    if required_github_state:
        normalized_request["required_github_review_state"] = required_github_state
    if merge_commit_sha:
        normalized_request["merge_commit_sha"] = merge_commit_sha.lower()
    snapshot: dict[str, Any] = {"request": normalized_request}
    warnings: list[str] = []
    for name in COLLECTIONS:
        value = collections.get(name, [])
        snapshot[name], collection_warnings = validate_collection(name, value, normalized_request)
        warnings.extend(collection_warnings)
    if warnings:
        snapshot["warnings"] = warnings
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--slack-messages")
    parser.add_argument("--github-reviews")
    parser.add_argument("--github-comments")
    parser.add_argument("--review-threads")
    parser.add_argument("--output", default="-")
    args = parser.parse_args()

    try:
        request = read_json(args.request)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        paths = {
            "slack_messages": args.slack_messages,
            "github_reviews": args.github_reviews,
            "github_comments": args.github_comments,
            "review_threads": args.review_threads,
        }
        snapshot = build(
            request,
            {name: read_json(path) if path else [] for name, path in paths.items()},
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"snapshot build failed: {error}", file=sys.stderr)
        return 2

    payload = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
