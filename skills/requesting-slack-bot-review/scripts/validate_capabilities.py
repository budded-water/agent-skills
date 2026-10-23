#!/usr/bin/env python3
"""Validate the portable reviewer-capability cache without third-party deps."""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

LEDGERS = {"github_review", "github_comment", "slack"}
VERDICTS = {"approved", "with_findings", "no_verdict"}
STRUCTURED_OUTPUT = {"supported", "not_observed", "unknown"}
HOST_ROLES = {"operator", "owner"}
SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]+$")
SLACK_HUMAN_USER_RE = re.compile(r"^U[A-Z0-9]+$")
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def is_https_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def validate(payload):
    errors = []
    reviewers = payload.get("reviewers")
    if not isinstance(reviewers, dict):
        return ["reviewers must be an object"]
    for name, item in reviewers.items():
        if not isinstance(item, dict):
            errors.append(f"{name}: entry must be an object")
            continue
        for field in (
            "observed_at", "slack_user_id", "accepted_senders", "result_ledgers",
            "verdicts", "watches_new_pushes",
        ):
            if field not in item:
                errors.append(f"{name}: missing {field}")
        allowed_fields = {
            "observed_at", "slack_user_id", "github_login", "accepted_senders",
            "result_ledgers", "verdicts", "watches_new_pushes", "notes",
            "structured_output", "host",
        }
        unknown = set(item) - allowed_fields
        if unknown:
            errors.append(f"{name}: unknown fields: {', '.join(sorted(unknown))}")
        observed_at = str(item.get("observed_at", ""))
        try:
            parsed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            valid_observed_at = bool(RFC3339_RE.fullmatch(observed_at)) and parsed.utcoffset() is not None
        except ValueError:
            valid_observed_at = False
        if not valid_observed_at:
            errors.append(f"{name}: observed_at must be RFC 3339 date-time with an explicit offset")
        if not isinstance(item.get("slack_user_id"), str) or not SLACK_USER_RE.fullmatch(item.get("slack_user_id", "")):
            errors.append(f"{name}: slack_user_id must be a Slack member ID")
        if (
            not isinstance(item.get("accepted_senders"), list)
            or not item.get("accepted_senders")
            or not all(isinstance(value, str) and value for value in item["accepted_senders"])
        ):
            errors.append(f"{name}: accepted_senders must be a non-empty array")
        if not item.get("result_ledgers") or not set(item.get("result_ledgers") or []).issubset(LEDGERS):
            errors.append(f"{name}: invalid result ledger")
        if set(item.get("result_ledgers") or []) & {"github_review", "github_comment"}:
            github_login = item.get("github_login")
            if not isinstance(github_login, str) or not GITHUB_LOGIN_RE.fullmatch(github_login):
                errors.append(f"{name}: GitHub ledger requires a valid github_login")
        if not item.get("verdicts") or not set(item.get("verdicts") or []).issubset(VERDICTS):
            errors.append(f"{name}: invalid verdict")
        if not isinstance(item.get("watches_new_pushes"), bool):
            errors.append(f"{name}: watches_new_pushes must be boolean")
        if "structured_output" in item and item["structured_output"] not in STRUCTURED_OUTPUT:
            errors.append(f"{name}: invalid structured_output")
        host = item.get("host")
        if host is not None:
            if not isinstance(host, dict):
                errors.append(f"{name}: host must be an object")
                continue
            host_allowed = {"observed_at", "slack_user_id", "role", "evidence_url", "notes"}
            host_unknown = set(host) - host_allowed
            if host_unknown:
                errors.append(
                    f"{name}: unknown host fields: {', '.join(sorted(host_unknown))}"
                )
            for field in ("observed_at", "slack_user_id", "role", "evidence_url"):
                if field not in host:
                    errors.append(f"{name}: host missing {field}")
            host_observed_at = str(host.get("observed_at", ""))
            try:
                parsed = dt.datetime.fromisoformat(host_observed_at.replace("Z", "+00:00"))
                valid_host_observed_at = (
                    bool(RFC3339_RE.fullmatch(host_observed_at))
                    and parsed.utcoffset() is not None
                )
            except ValueError:
                valid_host_observed_at = False
            if not valid_host_observed_at:
                errors.append(
                    f"{name}: host observed_at must be RFC 3339 date-time with an explicit offset"
                )
            if (
                not isinstance(host.get("slack_user_id"), str)
                or not SLACK_HUMAN_USER_RE.fullmatch(host.get("slack_user_id", ""))
            ):
                errors.append(f"{name}: host slack_user_id must be a Slack user ID")
            if host.get("role") not in HOST_ROLES:
                errors.append(f"{name}: invalid host role")
            evidence_url = host.get("evidence_url")
            if not is_https_url(evidence_url):
                errors.append(f"{name}: host evidence_url must be an HTTPS URL")
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    errors = validate(json.loads(args.file.read_text()))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Reviewer capability cache is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
