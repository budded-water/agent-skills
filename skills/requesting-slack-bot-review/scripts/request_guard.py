#!/usr/bin/env python3
"""Persist review-request identities and reject duplicate in-flight requests."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ACTIVE_STATES = {"requested", "acknowledged", "in_progress"}
TERMINAL_STATES = {"delivered", "failed", "expired", "superseded", "artifact_closed"}
NON_RETRYABLE_STATES = ACTIVE_STATES | {"delivered", "expired", "artifact_closed"}
DEFAULT_ACK_TIMEOUT = 60
DEFAULT_POST_TIMEOUT = 300
DEFAULT_RESULT_TIMEOUT = 600
DEFAULT_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex/slack-review-requests.json"


def identity(reviewer: str, repo: str, artifact: str, head: str) -> str:
    return f"{reviewer}:{repo}:{artifact}:{head.lower()}"


def remote_head(repo: str, pr: str) -> str:
    process = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
        check=True,
        text=True,
        capture_output=True,
    )
    return process.stdout.strip()


def load(file: Any) -> dict[str, Any]:
    file.seek(0)
    text = file.read()
    payload = json.loads(text) if text.strip() else {"version": 2, "requests": {}}
    if not isinstance(payload, dict):
        raise ValueError("request state envelope is invalid")
    if not isinstance(payload.get("requests"), dict):
        raise ValueError("request state collection is invalid")
    return payload


def consume_host_recovery(
    state_file: Path, request_key: str, failure_code: str
) -> dict[str, Any]:
    try:
        payload = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("host recovery ledger is missing or invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("host recovery ledger envelope is invalid")
    escalations = payload.get("escalations")
    if not isinstance(escalations, dict):
        raise ValueError("host recovery ledger escalations are invalid")
    escalation_key = f"{request_key}:{failure_code}"
    record = escalations.get(escalation_key)
    if not isinstance(record, dict):
        raise ValueError("host recovery ledger record is absent or invalid")
    attestation = record.get("recovery_attestation")
    if not isinstance(attestation, dict):
        raise ValueError("host recovery ledger attestation is absent or invalid")
    expected_condition_hash = hashlib.sha256(
        str(record.get("recovery_condition", "")).encode()
    ).hexdigest()
    valid = (
        record.get("request_identity") == request_key
        and record.get("state") == "recovered"
        and record.get("dependency_state") == "ready_to_resume"
        and record.get("recovery_mode") == "trusted_probe"
        and record.get("completion_contract_unchanged") is True
        and record.get("expected_ledger") in {"github_review", "slack", "either"}
        and attestation.get("succeeded") is True
        and attestation.get("probe_exit_code") == 0
        and attestation.get("probe_hash") == record.get("recovery_probe_hash")
        and attestation.get("probe_executable_hash")
        == record.get("recovery_probe_executable_hash")
        and isinstance(record.get("recovery_probe_id"), str)
        and bool(record.get("recovery_probe_id"))
        and isinstance(record.get("recovery_probe_trust_hash"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", record["recovery_probe_trust_hash"]))
        and attestation.get("probe_trust_hash")
        == record.get("recovery_probe_trust_hash")
        and attestation.get("condition_hash") == expected_condition_hash
        and attestation.get("condition_hash") == record.get("recovery_condition_hash")
        and isinstance(attestation.get("observed_at"), int)
        and attestation.get("observed_at") > 0
        and isinstance(attestation.get("observed_at_ns"), int)
        and attestation.get("observed_at_ns") > 0
        and isinstance(attestation.get("attestation_id"), str)
        and bool(re.fullmatch(r"[0-9a-f]{32}", attestation.get("attestation_id")))
    )
    if not valid:
        raise ValueError("host recovery attestation is absent, stale, or inconsistent")
    return {
        "failure_code": failure_code,
        "condition_hash": attestation["condition_hash"],
        "probe_hash": attestation["probe_hash"],
        "probe_executable_hash": attestation["probe_executable_hash"],
        "probe_trust_hash": attestation["probe_trust_hash"],
        "observed_at": attestation["observed_at"],
        "observed_at_ns": attestation["observed_at_ns"],
        "attestation_id": attestation["attestation_id"],
        "expected_ledger": record["expected_ledger"],
    }


def save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        os.unlink(temp_path)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "register", "set-state", "show"])
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--artifact", required=True, help="PR number or stable diff identifier")
    parser.add_argument("--head", required=True)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--state", choices=sorted(ACTIVE_STATES | TERMINAL_STATES))
    parser.add_argument(
        "--ack-timeout",
        type=int,
        default=DEFAULT_ACK_TIMEOUT,
        help="seconds to wait for an acknowledgement while state is requested",
    )
    parser.add_argument(
        "--result-timeout",
        type=int,
        default=DEFAULT_RESULT_TIMEOUT,
        help="seconds to wait for a result while acknowledged or in_progress",
    )
    parser.add_argument(
        "--silence-timeout",
        type=int,
        help="deprecated compatibility override applied to both active timeouts",
    )
    parser.add_argument("--skip-remote-head-check", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--github-reviewer")
    parser.add_argument("--expected-ledger", choices=["github_review", "slack", "either"])
    parser.add_argument(
        "--required-github-review-state",
        choices=["APPROVED"],
        help="optional provider action required in addition to the semantic verdict",
    )
    parser.add_argument("--channel")
    parser.add_argument("--thread-ts")
    parser.add_argument("--request-url", help="permalink of the verified Slack request post")
    parser.add_argument("--result-url")
    parser.add_argument("--terminal-reason")
    parser.add_argument("--host-recovery-state-file", type=Path)
    parser.add_argument("--host-failure-code")
    args = parser.parse_args()
    if args.action == "set-state" and not args.state:
        parser.error("set-state requires --state")
    if args.request_url and (args.action != "set-state" or args.state != "requested"):
        parser.error("--request-url requires set-state --state requested")
    if bool(args.host_recovery_state_file) != bool(args.host_failure_code):
        parser.error(
            "--host-recovery-state-file and --host-failure-code must be used together"
        )
    if args.host_recovery_state_file and args.action != "register":
        parser.error("host recovery attestation is consumed only during register")
    if (
        args.required_github_review_state
        and args.expected_ledger not in {"github_review", "either"}
    ):
        parser.error(
            "--required-github-review-state requires --expected-ledger github_review or either"
        )
    if args.required_github_review_state and not args.github_reviewer:
        parser.error(
            "--required-github-review-state requires --github-reviewer"
        )
    for name in ("ack_timeout", "result_timeout", "silence_timeout"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def active_timeout(args: argparse.Namespace, record: dict[str, Any]) -> tuple[int, str]:
    if args.silence_timeout is not None:
        return args.silence_timeout, "legacy_silence_timeout"
    if record["state"] == "requested":
        pending_post = (
            record.get("delivery_confirmation_required") is True
            and record.get("sent_at_ns") is None
        )
        if pending_post:
            return DEFAULT_POST_TIMEOUT, "posting_timeout"
        return args.ack_timeout, "acknowledgement_timeout"
    return args.result_timeout, "result_timeout"


def main() -> int:
    args = parse_args()
    key = identity(args.reviewer, args.repo, args.artifact, args.head)
    host_recovery = None
    if args.host_recovery_state_file:
        try:
            host_recovery = consume_host_recovery(
                args.host_recovery_state_file, key, args.host_failure_code
            )
        except ValueError as error:
            print(json.dumps({
                "allowed": False,
                "reason": "invalid_host_recovery_attestation",
                "error": str(error),
                "identity": key,
            }, sort_keys=True))
            return 7
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_file.with_suffix(args.state_file.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if args.state_file.exists():
                with args.state_file.open() as source:
                    payload = load(source)
            else:
                payload = {"version": 2, "requests": {}}
            requests = payload["requests"]
            record = requests.get(key)
            if key in requests and not isinstance(record, dict):
                raise ValueError("request state record is invalid")
        except ValueError as error:
            print(json.dumps({
                "allowed": False,
                "reason": "invalid_request_state",
                "error": str(error),
                "identity": key,
            }, sort_keys=True))
            return 2
        payload["version"] = 2
        now = int(time.time())
        now_ns = time.time_ns()
        if args.action == "register" and not args.skip_remote_head_check:
            actual = remote_head(args.repo, args.artifact)
            if actual != args.head:
                if record and record.get("state") in ACTIVE_STATES:
                    record["state"] = "superseded"
                    record["updated_at"] = now
                    record["updated_at_ns"] = now_ns
                    save(args.state_file, payload)
                print(json.dumps({
                    "allowed": False,
                    "reason": "superseded",
                    "expected": args.head,
                    "actual": actual,
                    "record": record,
                }, sort_keys=True))
                return 5
        activates_request = (
            args.action == "register"
            or (args.action == "set-state" and args.state in ACTIVE_STATES)
        )
        if activates_request and not args.force:
            blocking_request = next(
                (
                    (other_key, other_record)
                    for other_key, other_record in requests.items()
                    if other_key != key
                    and isinstance(other_record, dict)
                    and other_record.get("repo") == args.repo
                    and other_record.get("artifact") == args.artifact
                    and str(other_record.get("head", "")).lower()
                    == args.head.lower()
                    and other_record.get("state") in ACTIVE_STATES
                ),
                None,
            )
            if blocking_request:
                blocking_key, blocking_record = blocking_request
                print(json.dumps({
                    "allowed": False,
                    "reason": "another_reviewer_active",
                    "identity": key,
                    "blocking_identity": blocking_key,
                    "blocking_state": blocking_record["state"],
                }, sort_keys=True))
                return 4
        if host_recovery:
            recovery_matches_failed_request = (
                record
                and record.get("state") == "failed"
                and isinstance(record.get("updated_at_ns"), int)
                and record.get("updated_at_ns") > 0
                and int(host_recovery["observed_at_ns"])
                > record["updated_at_ns"]
                and host_recovery["attestation_id"]
                != (record.get("host_recovery") or {}).get("attestation_id")
                and (
                    not record.get("expected_ledger")
                    or record.get("expected_ledger") == host_recovery["expected_ledger"]
                )
                and (
                    not args.expected_ledger
                    or args.expected_ledger == host_recovery["expected_ledger"]
                )
            )
            if not recovery_matches_failed_request:
                print(json.dumps({
                    "allowed": False,
                    "reason": "host_recovery_does_not_follow_failed_request",
                    "identity": key,
                    "record": record,
                }, sort_keys=True))
                return 7
        expired = False
        if (
            args.action != "set-state"
            and record
            and record.get("state") in ACTIVE_STATES
        ):
            timeout, expiration_reason = active_timeout(args, record)
            if now - int(record.get("updated_at", 0)) >= timeout:
                record["state"] = "expired"
                record["expiration_reason"] = expiration_reason
                record["timeout_seconds"] = timeout
                record["updated_at"] = now
                record["updated_at_ns"] = now_ns
                expired = True

        if expired:
            save(args.state_file, payload)

        if args.action == "show":
            print(json.dumps(record or {}, sort_keys=True))
            return 0
        if args.action == "check":
            allowed = not record or record.get("state") not in NON_RETRYABLE_STATES or args.force
            reason = None
            if record and record.get("state") == "expired" and not args.force:
                reason = "expired_requires_reconciliation"
            elif record and record.get("state") == "delivered" and not args.force:
                reason = "already_delivered"
            elif record and record.get("state") == "artifact_closed" and not args.force:
                reason = "artifact_closed_requires_reconciliation"
            elif record and record.get("state") in ACTIVE_STATES and not args.force:
                reason = "duplicate_active"
            print(json.dumps({"allowed": allowed, "identity": key, "reason": reason, "record": record}, sort_keys=True))
            return 0 if allowed else 4
        if args.action == "register":
            if record and record.get("state") in NON_RETRYABLE_STATES and not args.force:
                reason = "duplicate_active"
                if record.get("state") == "expired":
                    reason = "expired_requires_reconciliation"
                elif record.get("state") == "delivered":
                    reason = "already_delivered"
                elif record.get("state") == "artifact_closed":
                    reason = "artifact_closed_requires_reconciliation"
                print(json.dumps({"allowed": False, "identity": key, "reason": reason, "record": record}, sort_keys=True))
                return 4
            previous = record or {}
            requests[key] = {
                "reviewer": args.reviewer,
                "repo": args.repo,
                "artifact": args.artifact,
                "head": args.head,
                "state": "requested",
                "delivery_confirmation_required": True,
                "updated_at": now,
                "updated_at_ns": now_ns,
            }
            if host_recovery:
                for field in (
                    "github_reviewer", "expected_ledger",
                    "required_github_review_state", "channel", "thread_ts"
                ):
                    if previous.get(field) is not None:
                        requests[key][field] = previous[field]
            if host_recovery:
                requests[key]["host_recovery"] = host_recovery
        else:
            if not record:
                print(json.dumps({"updated": False, "reason": "unknown_identity", "identity": key}))
                return 6
            if args.state == "requested":
                if record.get("state") != "requested":
                    print(json.dumps({
                        "allowed": False, "reason": "request_not_awaiting_delivery",
                        "identity": key, "record": record,
                    }, sort_keys=True))
                    return 4
                if not args.request_url or not args.request_url.startswith("https://"):
                    print(json.dumps({
                        "allowed": False, "reason": "verified_request_url_required",
                        "identity": key, "record": record,
                    }, sort_keys=True))
                    return 4
                if record.get("sent_at_ns") is not None:
                    if args.request_url == record.get("request_url"):
                        print(json.dumps({
                            "allowed": True, "identity": key, "record": record,
                            "reason": "already_recorded",
                        }, sort_keys=True))
                        return 0
                    print(json.dumps({
                        "allowed": False, "reason": "request_delivery_already_recorded",
                        "identity": key, "record": record,
                    }, sort_keys=True))
                    return 4
                record["request_url"] = args.request_url
                record["sent_at_ns"] = now_ns
            record["state"] = args.state
            if args.state != "expired":
                record.pop("expiration_reason", None)
                record.pop("timeout_seconds", None)
            record["updated_at"] = now
            record["updated_at_ns"] = now_ns
        record = requests[key]
        for field in (
            "github_reviewer", "expected_ledger", "required_github_review_state",
            "channel", "thread_ts", "result_url", "terminal_reason",
        ):
            value = getattr(args, field)
            if value is not None:
                record[field] = value
        save(args.state_file, payload)
        print(json.dumps({"allowed": True, "identity": key, "record": requests[key]}, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
