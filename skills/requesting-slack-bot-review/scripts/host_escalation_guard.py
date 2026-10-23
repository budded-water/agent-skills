#!/usr/bin/env python3
"""Classify and deduplicate recovery requests for review-bot hosts."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CLASSIFICATIONS = {
    "agent_recoverable",
    "transient_retryable",
    "host_action_required",
    "user_decision_required",
    "external_provider_outage",
}
ROUTES = {
    "agent_recoverable": "recover_directly",
    "transient_retryable": "retry_bounded",
    "host_action_required": "escalate_host",
    "user_decision_required": "ask_user",
    "external_provider_outage": "monitor_provider",
}
ESCALATION_STATES = {"waiting_on_host", "notified", "recovered", "failed"}
TERMINAL_STATES = {"recovered", "failed"}
REQUIRED_OWNERSHIP_PREDICATES = {
    "host_environment_confirmed": "--host-environment-confirmed",
    "host_owns_recovery": "--host-owns-recovery",
    "self_recovery_unavailable": "--self-recovery-unavailable",
}
HOST_ROLES = {"operator", "owner"}
SEND_LEASE_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 30
DEFAULT_STATE = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "codex/slack-review-host-escalations.json"
)
RECOVERY_PROBE_REGISTRY = (
    Path(__file__).parents[1] / "references/recovery-probes.json"
)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEAD_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
SLACK_USER_RE = re.compile(r"^U[A-Z0-9]+$")
SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]+$")
SLACK_THREAD_RE = re.compile(r"^\d{10,}\.\d{6}$")
REVIEWER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$")
ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
FAILURE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
PROBE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,}|"
        r"AKIA[0-9A-Z]{16})\b"
    ),
)
CREDENTIAL_FIELD_RE = re.compile(
    r"(?i)\b(?:authorization|password|passwd|token|secret|api[_ -]?key|"
    r"private[_ -]?key)\s*[:=]\s*([^\r\n]*)"
)
CANONICAL_REDACTIONS = {"redacted", "<redacted>", "[redacted]"}
TEXT_LIMITS = {
    "evidence": 500,
    "recovery_action": 300,
    "recovery_condition": 300,
    "recovery_evidence": 500,
}


def request_identity(reviewer: str, repo: str, artifact: str, head: str) -> str:
    return f"{reviewer}:{repo}:{artifact}:{head.lower()}"


def failure_fingerprint(request_key: str, failure_code: str) -> str:
    return hashlib.sha256(f"{request_key}:{failure_code}".encode()).hexdigest()[:20]


def message_hash(message: str) -> str:
    return hashlib.sha256(message.encode()).hexdigest()


def escalation_identity(request_key: str, failure_code: str) -> str:
    return f"{request_key}:{failure_code}"


def classify(classification: str, predicates: dict[str, bool]) -> dict[str, Any]:
    missing = []
    if classification == "host_action_required":
        missing = [
            option
            for field, option in REQUIRED_OWNERSHIP_PREDICATES.items()
            if not predicates.get(field)
        ]
    return {
        "classification": classification,
        "route": ROUTES[classification],
        "eligible": not missing,
        "missing_requirements": missing,
        "continue_independent_work": classification == "host_action_required",
        "completion_contract_unchanged": True,
        "requires_live_host_verification": classification == "host_action_required",
    }


def validate_safe_text(field: str, value: str | None, *, required: bool = True) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if not text:
        return ""
    limit = TEXT_LIMITS[field]
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    if contains_secret_like(text):
        raise ValueError(f"{field} contains secret-like material; provide a sanitized summary")
    return text


def contains_secret_like(value: str) -> bool:
    for match in CREDENTIAL_FIELD_RE.finditer(value):
        if match.group(1).strip().casefold() not in CANONICAL_REDACTIONS:
            return True
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def slack_escape(value: str) -> str:
    return html.escape(value, quote=False)


def is_https_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def slack_permalink_message_ts(value: str | None, channel: str) -> str | None:
    if not is_https_url(value):
        return None
    parsed = urlparse(value)
    if not parsed.hostname or not parsed.hostname.endswith(".slack.com"):
        return None
    match = re.fullmatch(
        rf"/archives/{re.escape(channel)}/p(\d+)", parsed.path
    )
    if not match or len(match.group(1)) <= 6:
        return None
    digits = match.group(1)
    message_ts = f"{digits[:-6]}.{digits[-6:]}"
    return message_ts if SLACK_THREAD_RE.fullmatch(message_ts) else None


def is_slack_permalink(value: str | None, channel: str) -> bool:
    return slack_permalink_message_ts(value, channel) is not None


def load_slack_token() -> str:
    for name in ("SLACK_TOKEN", "SLACK_MCP_XOXP_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    raise ValueError("Set SLACK_TOKEN or SLACK_MCP_XOXP_TOKEN explicitly for Slack reconciliation")


def slack_api(method: str, parameters: dict[str, str]) -> dict[str, Any]:
    token = load_slack_token()
    request = Request(
        f"https://slack.com/api/{method}?{urlencode(parameters)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Slack thread reconciliation failed") from error
    if not payload.get("ok"):
        raise ValueError(
            "Slack thread reconciliation failed: "
            + str(payload.get("error") or "unknown provider error")
        )
    return payload


def resolve_slack_sender() -> str:
    sender = slack_api("auth.test", {}).get("user_id")
    if not isinstance(sender, str) or not SLACK_USER_RE.fullmatch(sender):
        raise ValueError("Slack authentication did not return a human sender identity")
    return sender


def reconcile_slack_notification(
    record: dict[str, Any], *, expected_message_ts: str | None = None
) -> str | None:
    expected_message = record.get("notification_message")
    expected_hash = record.get("notification_message_hash")
    if not isinstance(expected_message, str) or not isinstance(expected_hash, str):
        raise ValueError(
            "escalation lacks a persisted notification message binding"
        )
    if not hmac.compare_digest(message_hash(expected_message), expected_hash):
        raise ValueError("persisted notification message binding is invalid")
    cursor = ""
    marker = f"[host-recovery:{record['failure_fingerprint']}]"
    for _ in range(10):
        parameters = {
            "channel": record["channel"],
            "ts": record["thread_ts"],
            "limit": "100",
        }
        if cursor:
            parameters["cursor"] = cursor
        payload = slack_api("conversations.replies", parameters)
        for message in payload.get("messages") or []:
            text = str(message.get("text") or "")
            if (
                message.get("user") == record["sender_slack_user_id"]
                and marker in text
                and f"<@{record['host_slack_user_id']}>" in text
                and hmac.compare_digest(message_hash(text), expected_hash)
            ):
                message_ts = str(message.get("ts") or "")
                if not SLACK_THREAD_RE.fullmatch(message_ts):
                    raise ValueError("matching Slack notification has an invalid timestamp")
                if expected_message_ts and message_ts != expected_message_ts:
                    continue
                permalink = slack_api("chat.getPermalink", {
                    "channel": record["channel"],
                    "message_ts": message_ts,
                }).get("permalink")
                if not is_slack_permalink(permalink, record["channel"]):
                    raise ValueError("Slack returned an invalid notification permalink")
                return permalink
        cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return None
    raise ValueError("Slack thread reconciliation exceeded the pagination bound")


def confirm_slack_notification(record: dict[str, Any], result_url: str) -> str:
    message_ts = slack_permalink_message_ts(result_url, record["channel"])
    if not message_ts:
        raise ValueError("result_url must be a Slack permalink for the registered channel")
    verified_permalink = reconcile_slack_notification(
        record, expected_message_ts=message_ts
    )
    if not verified_permalink:
        raise ValueError(
            "result_url does not identify the authenticated recovery notification"
        )
    if (
        slack_permalink_message_ts(verified_permalink, record["channel"])
        != message_ts
    ):
        raise ValueError("Slack returned a different notification permalink")
    return verified_permalink


def resolve_host(reviewer: str) -> dict[str, str]:
    resolver = Path(os.environ["SLACK_REVIEW_RESOLVER"]).expanduser() if os.environ.get("SLACK_REVIEW_RESOLVER") else Path(__file__).parents[1] / "resolve_bot.sh"
    try:
        process = subprocess.run(
            [str(resolver), "--json", "--require-host", reviewer],
            check=False,
            text=True,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"live host verification failed: {type(error).__name__}"
        ) from error
    if process.returncode != 0:
        detail = process.stderr.strip()[:300] or "resolver failed"
        raise ValueError(f"live host verification failed: {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("live host verification returned invalid JSON") from error
    host = payload.get("host") or {}
    host_id = host.get("slack_user_id")
    role = host.get("role")
    if payload.get("reviewer") != reviewer:
        raise ValueError("live host verification returned a different reviewer")
    if not isinstance(host_id, str) or not SLACK_USER_RE.fullmatch(host_id):
        raise ValueError("live host verification did not return a human Slack user ID")
    if role not in HOST_ROLES:
        raise ValueError("live host verification returned an unsupported host role")
    if host_id == payload.get("slack_user_id"):
        raise ValueError("verified host must differ from the review bot")
    return {"slack_user_id": host_id, "role": role}


def normalize_probe(command: str | None, arguments: list[str]) -> list[str]:
    if not command:
        raise ValueError("recovery_probe is required")
    executable = shutil.which(command)
    if not executable and "/" in command:
        candidate = Path(command).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    if not executable:
        raise ValueError("recovery_probe must resolve to an executable")
    executable = str(Path(executable).resolve())
    if len(arguments) > 20:
        raise ValueError("recovery_probe accepts at most 20 arguments")
    normalized = [executable]
    for argument in arguments:
        if len(argument) > 300:
            raise ValueError("recovery_probe argument exceeds 300 characters")
        if contains_secret_like(argument):
            raise ValueError("recovery_probe contains secret-like material")
        normalized.append(argument)
    return normalized


def probe_hash(argv: list[str]) -> str:
    encoded = json.dumps(argv, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def executable_hash(executable: str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(executable).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ValueError("recovery_probe executable cannot be hashed") from error
    return digest.hexdigest()


def load_recovery_probe_registry() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(RECOVERY_PROBE_REGISTRY.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("trusted recovery probe registry is unavailable") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "probes"}
        or payload.get("version") != 1
    ):
        raise ValueError("trusted recovery probe registry has an invalid envelope")
    probes = payload.get("probes")
    if not isinstance(probes, dict):
        raise ValueError("trusted recovery probe registry must contain probes")
    for probe_id, entry in probes.items():
        if not isinstance(probe_id, str) or not PROBE_ID_RE.fullmatch(probe_id):
            raise ValueError("trusted recovery probe registry has an invalid probe ID")
        if not isinstance(entry, dict) or set(entry) - {
            "executable_sha256", "arguments", "failure_codes", "notes"
        }:
            raise ValueError(f"trusted recovery probe {probe_id} has invalid fields")
        if not SHA256_RE.fullmatch(str(entry.get("executable_sha256") or "")):
            raise ValueError(f"trusted recovery probe {probe_id} has an invalid digest")
        arguments = entry.get("arguments")
        failure_codes = entry.get("failure_codes")
        if (
            not isinstance(arguments, list)
            or len(arguments) > 20
            or any(not isinstance(value, str) for value in arguments)
        ):
            raise ValueError(f"trusted recovery probe {probe_id} has invalid arguments")
        if (
            not isinstance(failure_codes, list)
            or not failure_codes
            or any(
                not isinstance(value, str) or not FAILURE_CODE_RE.fullmatch(value)
                for value in failure_codes
            )
        ):
            raise ValueError(f"trusted recovery probe {probe_id} has invalid failure codes")
    return probes


def authorize_recovery_probe(
    probe_id: str,
    argv: list[str],
    failure_code: str,
    executable_digest: str,
) -> str:
    if not isinstance(probe_id, str) or not PROBE_ID_RE.fullmatch(probe_id):
        raise ValueError("recovery_probe_id must be a stable lowercase ID")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(value, str) for value in argv)
    ):
        raise ValueError("recovery_probe argv is invalid")
    if not isinstance(failure_code, str) or not FAILURE_CODE_RE.fullmatch(failure_code):
        raise ValueError("recovery_probe failure code is invalid")
    if not isinstance(executable_digest, str) or not SHA256_RE.fullmatch(
        executable_digest
    ):
        raise ValueError("recovery_probe executable digest is invalid")
    entry = load_recovery_probe_registry().get(probe_id)
    if not entry:
        raise ValueError("recovery_probe_id is not in the trusted registry")
    if not hmac.compare_digest(entry["executable_sha256"], executable_digest):
        raise ValueError("recovery_probe executable is not trusted for this probe ID")
    if argv[1:] != entry["arguments"]:
        raise ValueError("recovery_probe arguments do not match the trusted registry")
    if failure_code not in entry["failure_codes"]:
        raise ValueError("recovery_probe is not trusted for this failure code")
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{probe_id}:{canonical}".encode()).hexdigest()


@contextmanager
def verified_probe_snapshot(
    executable: str, expected_hash: str
) -> Iterator[Path]:
    """Stage the bytes read from one opened executable and run only that copy."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(executable, flags)
    except OSError as error:
        raise ValueError("recovery_probe executable cannot be opened") from error
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ValueError("recovery_probe executable must be a regular file")
        with tempfile.TemporaryDirectory(prefix="slack-review-probe-") as directory:
            snapshot = Path(directory) / "probe"
            digest = hashlib.sha256()
            with (
                os.fdopen(os.dup(source_fd), "rb") as source,
                snapshot.open("xb") as target,
            ):
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if digest.hexdigest() != expected_hash:
                raise ValueError("probe_executable_changed")
            snapshot.chmod(stat.S_IRUSR | stat.S_IXUSR)
            yield snapshot
    finally:
        os.close(source_fd)


def condition_hash(condition: str) -> str:
    return hashlib.sha256(condition.encode()).hexdigest()


def run_recovery_probe(record: dict[str, Any]) -> dict[str, Any]:
    try:
        trust_hash = authorize_recovery_probe(
            record["recovery_probe_id"],
            record["recovery_probe"],
            record["failure_code"],
            record["recovery_probe_executable_hash"],
        )
    except (KeyError, ValueError) as error:
        return {"succeeded": False, "reason": str(error)}
    if not hmac.compare_digest(
        trust_hash, str(record.get("recovery_probe_trust_hash") or "")
    ):
        return {"succeeded": False, "reason": "recovery_probe_trust_changed"}
    environment = {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "SLACK_REVIEW_RECOVERY_REQUEST_IDENTITY": record["request_identity"],
        "SLACK_REVIEW_RECOVERY_FAILURE_CODE": record["failure_code"],
        "SLACK_REVIEW_RECOVERY_CONDITION_HASH": record["recovery_condition_hash"],
    }
    try:
        with verified_probe_snapshot(
            record["recovery_probe"][0],
            record["recovery_probe_executable_hash"],
        ) as executable:
            process = subprocess.run(
                [str(executable), *record["recovery_probe"][1:]],
                check=False,
                text=True,
                capture_output=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                env=environment,
            )
    except ValueError as error:
        return {"succeeded": False, "reason": str(error)}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"succeeded": False, "reason": type(error).__name__}
    combined = f"{process.stdout}\n{process.stderr}"[:1000]
    if combined and contains_secret_like(combined):
        return {"succeeded": False, "reason": "probe_output_contains_secret_like_material"}
    if process.returncode != 0:
        return {"succeeded": False, "exit_code": process.returncode}
    try:
        attestation = json.loads(process.stdout)
    except json.JSONDecodeError:
        return {"succeeded": False, "reason": "probe_output_is_not_json"}
    if not isinstance(attestation, dict):
        return {"succeeded": False, "reason": "probe_output_is_not_object"}
    evidence = attestation.get("evidence")
    try:
        evidence = validate_safe_text("recovery_evidence", evidence)
    except ValueError as error:
        return {"succeeded": False, "reason": str(error)}
    bound = (
        attestation.get("succeeded") is True
        and attestation.get("request_identity") == record["request_identity"]
        and attestation.get("failure_code") == record["failure_code"]
        and attestation.get("condition_hash") == record["recovery_condition_hash"]
    )
    if not bound:
        return {"succeeded": False, "reason": "probe_attestation_mismatch"}
    return {
        "succeeded": True,
        "exit_code": 0,
        "evidence": evidence,
    }


def build_message(record: dict[str, Any]) -> str:
    artifact = slack_escape(f"{record['repo']}#{record['artifact']}")
    if record["artifact"].isdigit() and REPO_RE.fullmatch(record["repo"]):
        artifact = f"<{record['artifact_url']}|{artifact}>"
    return "\n".join((
        f"<@{record['host_slack_user_id']}> *Review-bot host recovery needed*",
        "",
        f"*BOT:* `{slack_escape(record['reviewer'])}`",
        f"*ARTIFACT:* {artifact}",
        f"*HEAD:* `{record['head']}`",
        f"*FAILURE CODE:* `{slack_escape(record['failure_code'])}`",
        f"*HOST RECOVERY ID:* `[host-recovery:{record['failure_fingerprint']}]`",
        f"*SANITIZED EVIDENCE:* {slack_escape(record['evidence'])}",
        f"*HOST ACTION:* {slack_escape(record['recovery_action'])}",
        f"*RECOVERY CONDITION:* {slack_escape(record['recovery_condition'])}",
        "",
        "Only this bot-dependent gate is `waiting_on_host`; independent task work continues. "
        "This recovery acknowledgement is not a review approval and does not change the result ledger or exact-head contract.",
    ))


def load(file: Any) -> dict[str, Any]:
    file.seek(0)
    text = file.read()
    payload = json.loads(text) if text.strip() else {"version": 1, "escalations": {}}
    if not isinstance(payload, dict):
        raise ValueError("host escalation state envelope is invalid")
    if not isinstance(payload.get("escalations"), dict):
        raise ValueError("host escalation state collection is invalid")
    return payload


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
    parser.add_argument("action", choices=["assess", "register", "set-state", "show"])
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--classification", choices=sorted(CLASSIFICATIONS))
    parser.add_argument("--failure-code")
    parser.add_argument("--host-environment-confirmed", action="store_true")
    parser.add_argument("--host-owns-recovery", action="store_true")
    parser.add_argument("--self-recovery-unavailable", action="store_true")
    parser.add_argument("--reconcile-thread", action="store_true")
    parser.add_argument("--evidence")
    parser.add_argument("--recovery-action")
    parser.add_argument("--recovery-condition")
    parser.add_argument("--recovery-evidence")
    parser.add_argument("--recovery-probe-id")
    parser.add_argument("--recovery-probe")
    parser.add_argument("--recovery-probe-arg", action="append", default=[])
    parser.add_argument("--expected-ledger", choices=["github_review", "slack", "either"])
    parser.add_argument("--channel")
    parser.add_argument("--thread-ts")
    parser.add_argument("--result-url")
    parser.add_argument("--state", choices=sorted(ESCALATION_STATES - {"waiting_on_host"}))
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not REVIEWER_RE.fullmatch(args.reviewer):
        parser.error("--reviewer must be a stable reviewer name")
    if not REPO_RE.fullmatch(args.repo):
        parser.error("--repo must be owner/name")
    if not ARTIFACT_RE.fullmatch(args.artifact):
        parser.error("--artifact must be a stable PR or diff identifier")
    if not HEAD_RE.fullmatch(args.head):
        parser.error("--head must be a full hexadecimal revision")
    if args.action in {"assess", "register"} and not args.classification:
        parser.error(f"{args.action} requires --classification")
    if args.action in {"register", "set-state", "show"}:
        if not args.failure_code or not FAILURE_CODE_RE.fullmatch(args.failure_code):
            parser.error(
                "register, set-state, and show require a stable lowercase --failure-code"
            )
    if args.action == "set-state" and not args.state:
        parser.error("set-state requires --state")
    return args


def predicates(args: argparse.Namespace) -> dict[str, bool]:
    return {field: bool(getattr(args, field)) for field in REQUIRED_OWNERSHIP_PREDICATES}


def main() -> int:
    args = parse_args()
    assessment = classify(args.classification, predicates(args)) if args.classification else None
    if args.action == "assess":
        print(json.dumps(assessment, sort_keys=True))
        return 0 if assessment["eligible"] else 2

    request_key = request_identity(args.reviewer, args.repo, args.artifact, args.head)
    key = escalation_identity(request_key, args.failure_code)
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_file.with_suffix(args.state_file.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if args.state_file.exists():
                with args.state_file.open() as source:
                    payload = load(source)
            else:
                payload = {"version": 1, "escalations": {}}
            escalations = payload["escalations"]
            record = escalations.get(key)
            if key in escalations and not isinstance(record, dict):
                raise ValueError("host escalation state record is invalid")
        except ValueError as error:
            print(json.dumps({
                "allowed": False,
                "reason": "invalid_host_escalation_state",
                "error": str(error),
                "identity": key,
            }, sort_keys=True))
            return 2
        payload["version"] = 1
        now = int(time.time())

        if args.action == "show":
            print(json.dumps(record or {}, sort_keys=True))
            return 0

        if args.action == "register":
            if assessment["classification"] != "host_action_required":
                print(json.dumps({
                    "allowed": False,
                    "reason": "classification_does_not_route_to_host",
                    "assessment": assessment,
                }, sort_keys=True))
                return 3
            if not assessment["eligible"]:
                print(json.dumps({
                    "allowed": False,
                    "reason": "host_escalation_requirements_missing",
                    "assessment": assessment,
                }, sort_keys=True))
                return 2
            if record and not args.force:
                if record.get("notification_state") == "pending_send":
                    if int(record.get("send_lease_expires_at", 0)) > now:
                        print(json.dumps({
                            "allowed": False,
                            "reason": "send_in_progress",
                            "identity": key,
                            "record": record,
                        }, sort_keys=True))
                        return 4
                    if not args.reconcile_thread:
                        print(json.dumps({
                            "allowed": False,
                            "reason": "unsent_requires_live_thread_reconciliation",
                            "identity": key,
                            "record": record,
                        }, sort_keys=True))
                        return 5
                    existing_permalink = reconcile_slack_notification(record)
                    if existing_permalink:
                        record["notification_state"] = "confirmed"
                        record["state"] = "notified"
                        record["result_url"] = existing_permalink
                        record.pop("send_lease_expires_at", None)
                        record["updated_at"] = now
                        save(args.state_file, payload)
                        print(json.dumps({
                            "allowed": False,
                            "reason": "existing_notification_reconciled",
                            "identity": key,
                            "record": record,
                        }, sort_keys=True))
                        return 0
                    refreshed_host = resolve_host(args.reviewer)
                    if (
                        refreshed_host["slack_user_id"]
                        != record.get("host_slack_user_id")
                        or refreshed_host["role"] != record.get("host_role")
                    ):
                        raise ValueError(
                            "live host relationship changed; do not reuse the stored mention"
                        )
                    if resolve_slack_sender() != record.get("sender_slack_user_id"):
                        raise ValueError(
                            "authenticated Slack sender changed; do not reuse the stored message"
                        )
                    record["send_attempt"] = int(record.get("send_attempt", 1)) + 1
                    record["send_lease_expires_at"] = now + SEND_LEASE_SECONDS
                    record["updated_at"] = now
                    save(args.state_file, payload)
                    print(json.dumps({
                        "allowed": True,
                        "reason": "retry_after_thread_reconciliation",
                        "identity": key,
                        "record": record,
                        "slack_message": record["notification_message"],
                    }, sort_keys=True))
                    return 0
                print(json.dumps({
                    "allowed": False,
                    "reason": "duplicate_escalation",
                    "identity": key,
                    "record": record,
                }, sort_keys=True))
                return 4
            if not args.expected_ledger:
                raise ValueError("expected_ledger is required")
            if not args.channel or not args.thread_ts:
                raise ValueError("channel and thread_ts are required for same-thread escalation")
            if not SLACK_CHANNEL_RE.fullmatch(args.channel):
                raise ValueError("channel must be a Slack channel ID")
            if not SLACK_THREAD_RE.fullmatch(args.thread_ts):
                raise ValueError("thread_ts must be a Slack message timestamp")
            evidence = validate_safe_text("evidence", args.evidence)
            recovery_action = validate_safe_text("recovery_action", args.recovery_action)
            recovery_condition = validate_safe_text(
                "recovery_condition", args.recovery_condition
            )
            probe_options_present = bool(
                args.recovery_probe_id
                or args.recovery_probe
                or args.recovery_probe_arg
            )
            recovery_probe = None
            recovery_probe_executable_hash = None
            recovery_probe_trust_hash = None
            if probe_options_present:
                if not args.recovery_probe_id or not args.recovery_probe:
                    raise ValueError(
                        "trusted recovery requires recovery_probe_id and recovery_probe"
                    )
                recovery_probe = normalize_probe(
                    args.recovery_probe, args.recovery_probe_arg
                )
                recovery_probe_executable_hash = executable_hash(recovery_probe[0])
                recovery_probe_trust_hash = authorize_recovery_probe(
                    args.recovery_probe_id,
                    recovery_probe,
                    args.failure_code,
                    recovery_probe_executable_hash,
                )
            host = resolve_host(args.reviewer)
            sender_slack_user_id = resolve_slack_sender()
            fingerprint = failure_fingerprint(request_key, args.failure_code)
            record = {
                "reviewer": args.reviewer,
                "repo": args.repo,
                "artifact": args.artifact,
                "artifact_url": f"https://github.com/{args.repo}/pull/{args.artifact}",
                "head": args.head.lower(),
                "request_identity": request_key,
                "failure_code": args.failure_code,
                "failure_fingerprint": fingerprint,
                "classification": args.classification,
                "host_slack_user_id": host["slack_user_id"],
                "host_role": host["role"],
                "sender_slack_user_id": sender_slack_user_id,
                "evidence": evidence,
                "recovery_action": recovery_action,
                "recovery_condition": recovery_condition,
                "recovery_condition_hash": condition_hash(recovery_condition),
                "recovery_mode": (
                    "trusted_probe" if recovery_probe else "result_ledger_only"
                ),
                "expected_ledger": args.expected_ledger,
                "channel": args.channel,
                "thread_ts": args.thread_ts,
                "state": "waiting_on_host",
                "dependency_state": "waiting_on_host",
                "notification_state": "pending_send",
                "send_attempt": 1,
                "send_lease_expires_at": now + SEND_LEASE_SECONDS,
                "continue_independent_work": True,
                "completion_contract_unchanged": True,
                "updated_at": now,
            }
            if recovery_probe:
                record.update({
                    "recovery_probe_id": args.recovery_probe_id,
                    "recovery_probe": recovery_probe,
                    "recovery_probe_hash": probe_hash(recovery_probe),
                    "recovery_probe_executable_hash": recovery_probe_executable_hash,
                    "recovery_probe_trust_hash": recovery_probe_trust_hash,
                })
            notification_message = build_message(record)
            record["notification_message"] = notification_message
            record["notification_message_hash"] = message_hash(
                notification_message
            )
            escalations[key] = record
            save(args.state_file, payload)
            print(json.dumps({
                "allowed": True,
                "identity": key,
                "record": record,
                "slack_message": notification_message,
            }, sort_keys=True))
            return 0

        if not record:
            print(json.dumps({"updated": False, "reason": "unknown_escalation", "identity": key}))
            return 6
        if record.get("state") in TERMINAL_STATES and not args.force:
            print(json.dumps({
                "updated": False,
                "reason": "terminal_escalation_requires_force",
                "identity": key,
                "record": record,
            }, sort_keys=True))
            return 4
        verified_result_url = None
        if args.state == "notified":
            if not args.result_url:
                raise ValueError("notified state requires result_url")
            verified_result_url = confirm_slack_notification(record, args.result_url)
        elif args.result_url and not is_slack_permalink(args.result_url, record["channel"]):
            raise ValueError("result_url must be a Slack permalink for the registered channel")
        if args.state == "recovered":
            if record.get("state") != "notified":
                raise ValueError("recovered state requires a confirmed host notification")
            if record.get("recovery_mode") != "trusted_probe":
                print(json.dumps({
                    "updated": False,
                    "reason": "trusted_recovery_probe_unavailable",
                    "identity": key,
                    "record": record,
                }, sort_keys=True))
                return 7
            recovery_evidence = validate_safe_text(
                "recovery_evidence", args.recovery_evidence, required=False
            )
            probe = run_recovery_probe(record)
            if not probe.get("succeeded"):
                print(json.dumps({
                    "updated": False,
                    "reason": "recovery_probe_failed",
                    "identity": key,
                    "probe": probe,
                    "record": record,
                }, sort_keys=True))
                return 7
            record["recovery_evidence"] = recovery_evidence or probe["evidence"]
            record["recovery_attestation"] = {
                "attestation_id": secrets.token_hex(16),
                "succeeded": True,
                "observed_at": now,
                "observed_at_ns": time.time_ns(),
                "probe_exit_code": probe["exit_code"],
                "probe_hash": record["recovery_probe_hash"],
                "probe_executable_hash": record["recovery_probe_executable_hash"],
                "probe_trust_hash": record["recovery_probe_trust_hash"],
                "condition_hash": record["recovery_condition_hash"],
            }
        if verified_result_url:
            record["result_url"] = verified_result_url
        elif args.result_url:
            record["result_url"] = args.result_url
        if args.state == "notified":
            record["notification_state"] = "confirmed"
            record.pop("send_lease_expires_at", None)
        record["state"] = args.state
        record["dependency_state"] = (
            "ready_to_resume" if args.state == "recovered"
            else "recovery_failed" if args.state == "failed"
            else "waiting_on_host"
        )
        record["updated_at"] = now
        save(args.state_file, payload)
        print(json.dumps({"updated": True, "identity": key, "record": record}, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(json.dumps({"error": str(error), "allowed": False}, sort_keys=True))
        sys.exit(2)
