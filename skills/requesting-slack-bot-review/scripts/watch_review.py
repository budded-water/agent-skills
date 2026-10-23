#!/usr/bin/env python3
"""Classify or monitor a revision-bound Slack/GitHub review request."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROGRESS_RE = re.compile(r"hourglass|working|still working|in progress|处理中|正在处理", re.I)
SHA_RE = re.compile(r"\b(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)
FINDING_RE = re.compile(r"finding|blocker|\bP[0-3]\b|issue|bug|问题|发现|未通过", re.I)
APPROVE_RE = re.compile(r"\bapprove(?:d)?\b|审核通过|代码审查通过|未发现.*blocker", re.I)
NEGATIVE_APPROVE_RE = re.compile(r"not approved|not pass|未通过|没有通过|不能通过|不通过", re.I)
# Deliberately NOT line-anchored. A reviewer may put the whole trailer on one
# line — observed in an upstream review: "VERDICT: APPROVED
# ACTIONABLE_FINDINGS: 0 HEAD: <sha>". Anchored patterns missed it, the verdict
# fell back to prose, and FINDING_RE then matched `FINDING` inside the field
# name `ACTIONABLE_FINDINGS` — so a review that was APPROVED at the provider AND
# said APPROVED with zero findings in its own structured fields was reported as
# with_findings, citing its approval sentence as the evidence.
#
# The word boundary is what keeps this safe: the request template writes
# `VERDICT: <APPROVED|WITH_FINDINGS>`, whose `<` stops the value from matching,
# so a reviewer echoing the template back does not self-report a verdict.
STRUCTURED_VERDICT_RE = re.compile(
    r"\bVERDICT\s*:\s*(APPROVED|WITH_FINDINGS)\b", re.I
)
STRUCTURED_FINDINGS_RE = re.compile(
    r"\bACTIONABLE_FINDINGS\s*:\s*(\d+)\b", re.I
)
EXPLICIT_NO_ACTIONABLE_APPROVAL_RE = re.compile(
    r"\bapprove(?:d)?\s*[-—:]\s*no actionable (?:issues?|findings?|blockers?|bugs?)\b",
    re.I,
)
NO_FINDINGS_RE = re.compile(
    r"\bno (?:(?:new|code|blocking|actionable) )*"
    r"(?:blockers?|issues?|findings?|bugs?)\b|未发现.*(?:问题|blocker)", re.I
)
RESOLVED_FINDINGS_RE = re.compile(
    r"\b(?:all |previous |prior |earlier |reported )?"
    r"(?:issues?|findings?|blockers?|bugs?)\s+"
    r"(?:(?:is|are|was|were|have been|has been)\s+)?(?:fixed|resolved|addressed|closed)\b"
    r"|\b(?:fixed|resolved|addressed|closed)\s+"
    r"(?:all |the |previous |prior |earlier |reported )?"
    r"(?:issues?|findings?|blockers?|bugs?)\b"
    r"|\bno actionable (?:issues?|findings?|blockers?|bugs?)\b"
    r"|\bnot (?:an? )?(?:issues?|findings?|blockers?|bugs?)\b",
    re.I,
)
NUMBERED_RESOLVED_FINDING_RE = re.compile(
    r"\b(?:finding|issue|blocker|bug)\b"
    r"(?:\s+#?\s*|\s*#\s*)(?:\d+|(?-i:[A-Z]))\b"
    r"(?:\s*\([^().\n]{1,80}\))?"
    r"(?:\s*[-—:]\s*|\s+(?:is|are|was|were|has\s+been|have\s+been)\s+)"
    r"\b(?:fixed|resolved|addressed|closed)\b",
    re.I,
)
NEITHER_ACTIONABLE_FINDING_RE = re.compile(
    r"\bneither\s+an?\s+(?:(?:new|code|blocking)\s+)*actionable\s+"
    r"(?:blockers?|issues?|findings?|bugs?)\b(?=\s*(?:[.;]|$))",
    re.I,
)
INACTIVE_FINDING_PATTERNS = (
    NO_FINDINGS_RE,
    RESOLVED_FINDINGS_RE,
    NUMBERED_RESOLVED_FINDING_RE,
    NEITHER_ACTIONABLE_FINDING_RE,
)
REQUIRED_GITHUB_REVIEW_STATES = {"APPROVED"}


@dataclass
class Result:
    transport_state: str
    review_verdict: str
    terminal: bool
    source: str | None = None
    url: str | None = None
    reason: str | None = None
    monitoring_scope: str | None = None
    verdict_evidence: str | None = None


class SnapshotValidationError(ValueError):
    """Raised when direct snapshot evidence cannot be trusted safely."""


def matches_head(value: str | None, head: str) -> bool:
    if not value:
        return False
    value = value.lower()
    head = head.lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", value):
        return False
    if not re.fullmatch(r"[0-9a-f]{7,40}", head):
        return False
    return head.startswith(value) or value.startswith(head)


def text_names_head(text: str, head: str) -> bool:
    # Match candidates against the known head, so an all-numeric short SHA is
    # safe and cannot be confused with an unrelated timestamp.
    candidates = re.findall(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])", text, re.I)
    return any(matches_head(candidate, head) for candidate in candidates)


def same_identity(actual: str | None, expected: str | None) -> bool:
    return bool(actual and expected and actual.casefold() == expected.casefold())


def parse_timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def evidence_timestamp(item: dict[str, Any]) -> float | None:
    edited = {}
    if "edited" in item:
        edited = item["edited"]
        if not isinstance(edited, dict):
            raise SnapshotValidationError("evidence.edited must be an object when present")
        if edited.get("ts") in (None, ""):
            raise SnapshotValidationError("evidence.edited.ts is required when edited is present")
    fields = (
        ("submitted_at", item.get("submitted_at")),
        ("updated_at", item.get("updated_at")),
        ("created_at", item.get("created_at")),
        ("edited.ts", edited.get("ts")),
        ("ts", item.get("ts")),
    )
    observed = []
    for field, value in fields:
        if value in (None, ""):
            continue
        parsed = parse_timestamp(value)
        if parsed is None:
            raise SnapshotValidationError(f"evidence.{field} is not a valid timestamp")
        observed.append(parsed)
    return max(observed, default=None)


def evidence_precedes_closure(item: dict[str, Any], request: dict[str, Any]) -> bool:
    artifact_state = str(request.get("artifact_state") or "").upper()
    if artifact_state == "OPEN":
        return True
    closed_at = parse_timestamp(
        request.get("merged_at") if artifact_state == "MERGED" else request.get("closed_at")
    )
    observed_at = evidence_timestamp(item)
    return bool(closed_at is not None and observed_at is not None and observed_at <= closed_at)


def strip_inactive_findings(text: str) -> str:
    for pattern in INACTIVE_FINDING_PATTERNS:
        text = pattern.sub("", text)
    return text


def _excerpt(haystack: str, match: "re.Match[str]") -> str:
    return haystack[max(0, match.start() - 70):match.end() + 40].strip()


def classify_prose(text: str) -> tuple[str, str | None]:
    """判定,以及它真正据以判定的那段文字。

    证据必须在判定点上取,不能事后在全文里重新找一遍:下面四条返回路径看的
    文本范围各不相同(有两条只看批准语之后的同一句),重推一次必然和判定漂移
    ——要么给 approved 挂上一段读起来像"假触发报告"的文字,要么让关键词推出来的
    with_findings 反而是空的,而空值会被读成"这次没有猜"。(upstream reviewer observation。)

    只有关键词推出来的 with_findings 带证据。结构化字段是权威,没有可解释的
    东西;approved 带证据只会误导。
    """

    structured_verdict = STRUCTURED_VERDICT_RE.search(text)
    structured_findings = STRUCTURED_FINDINGS_RE.search(text)
    if structured_findings and int(structured_findings.group(1)) > 0:
        return "with_findings", None
    zero_structured_findings = bool(
        structured_findings and int(structured_findings.group(1)) == 0
    )
    if structured_findings:
        # The field name contains the word "findings"; once a zero count has
        # been parsed, remove that metadata before prose heuristics run.
        text = STRUCTURED_FINDINGS_RE.sub("", text)
    if structured_verdict:
        verdict = (
            "approved"
            if structured_verdict.group(1).upper() == "APPROVED"
            else "with_findings"
        )
        return verdict, None
    negated = NEGATIVE_APPROVE_RE.search(text)
    if negated:
        return "with_findings", _excerpt(text, negated)
    for pattern, gated in (
        (APPROVE_RE, zero_structured_findings),
        (EXPLICIT_NO_ACTIONABLE_APPROVAL_RE, True),
    ):
        if not gated:
            continue
        approval = pattern.search(text)
        if not approval:
            continue
        same_sentence_tail = re.split(r"[.\n]", text[approval.end():], maxsplit=1)[0]
        active_tail = strip_inactive_findings(same_sentence_tail)
        hit = FINDING_RE.search(active_tail)
        if hit:
            return "with_findings", _excerpt(active_tail, hit)
        return "approved", None
    no_findings = any(pattern.search(text) for pattern in INACTIVE_FINDING_PATTERNS)
    remaining = strip_inactive_findings(text)
    hit = FINDING_RE.search(remaining)
    if hit:
        return "with_findings", _excerpt(remaining, hit)
    if no_findings:
        return "approved", None
    if APPROVE_RE.search(text):
        return "approved", None
    return "no_verdict", None


def text_verdict(text: str) -> str:
    return classify_prose(text)[0]


def prose_verdict_evidence(text: str) -> str | None:
    return classify_prose(text)[1]


def _classify(snapshot: dict[str, Any]) -> Result:
    request = snapshot["request"]
    head = request["head"]
    ledger = request.get("expected_ledger", "github_review")
    acceptable = set(request.get("acceptable_verdicts", ["approved", "with_findings"]))
    required_github_state = request.get("required_github_review_state")
    github_reviewer = request.get("github_reviewer")
    slack_reviewer = request.get("slack_reviewer")
    requester = request.get("requester_slack_id")
    remote_head = request.get("remote_head")
    artifact_state = str(request.get("artifact_state") or "").upper()
    if artifact_state not in {"OPEN", "MERGED", "CLOSED"}:
        return Result(
            "watcher_error", "none", False,
            reason="invalid snapshot: request.artifact_state must be OPEN, MERGED, or CLOSED",
        )
    if ledger in {"slack", "either"} and not slack_reviewer:
        return Result(
            "watcher_error", "none", False,
            reason=f"invalid snapshot: {ledger} ledger requires request.slack_reviewer",
        )
    if ledger in {"github_review", "either"} and not github_reviewer:
        return Result(
            "watcher_error", "none", False,
            reason=f"invalid snapshot: {ledger} ledger requires request.github_reviewer",
        )
    if required_github_state is not None:
        if not isinstance(required_github_state, str):
            return Result(
                "watcher_error", "none", False,
                reason="invalid snapshot: request.required_github_review_state must be APPROVED",
            )
        required_github_state = required_github_state.upper()
        if required_github_state not in REQUIRED_GITHUB_REVIEW_STATES:
            return Result(
                "watcher_error", "none", False,
                reason="invalid snapshot: request.required_github_review_state must be APPROVED",
            )
        if ledger not in {"github_review", "either"}:
            return Result(
                "watcher_error", "none", False,
                reason=(
                    "invalid snapshot: request.required_github_review_state "
                    "requires a GitHub-capable ledger"
                ),
            )
    if remote_head and remote_head != head:
        return Result("superseded", "none", False, reason=f"remote head moved to {remote_head}")

    current_threads = [
        thread
        for thread in snapshot.get("review_threads", [])
        if not thread.get("is_resolved")
        and not thread.get("is_outdated")
        and matches_head(thread.get("commit_id"), head)
        and same_identity(thread.get("user"), github_reviewer)
        and evidence_precedes_closure(thread, request)
    ]

    candidates: list[Result] = []

    def result_is_terminal(
        verdict: str, source: str, provider_state: str | None = None
    ) -> bool:
        if verdict not in acceptable:
            return False
        if verdict == "with_findings":
            return True
        if required_github_state:
            return (
                source == "github_review"
                and provider_state == required_github_state
            )
        return True

    # GitHub evidence is usable only when it can be attributed to the reviewer
    # named by the request. Missing identity must fail closed, not mean "any
    # collaborator", or an unrelated approval can terminate another reviewer's
    # request.
    if ledger in {"github_review", "either"}:
        current_reviews = []
        for review in snapshot.get("github_reviews", []):
            if not same_identity(review.get("user"), github_reviewer):
                continue
            if not evidence_precedes_closure(review, request):
                continue
            if matches_head(review.get("commit_id"), head):
                current_reviews.append(review)
        if current_reviews:
            review = current_reviews[-1]
            review_url = review.get("html_url")
            if not isinstance(review_url, str) or not review_url.strip():
                return Result(
                    "watcher_error",
                    "none",
                    False,
                    reason=(
                        "invalid snapshot: exact-head GitHub review is missing "
                        "a non-empty html_url"
                    ),
                )
            state = str(review.get("state", "")).upper()
            body = str(review.get("body") or "")
            if state == "APPROVED" and not body.strip():
                return Result(
                    "watcher_error",
                    "none",
                    False,
                    reason=(
                        "invalid snapshot: exact-head APPROVED GitHub review is "
                        "missing a non-empty body"
                    ),
                )
            body_verdict, body_evidence = classify_prose(body)
            evidence = None
            if state == "CHANGES_REQUESTED" or review.get("has_findings") or current_threads:
                verdict = "with_findings"
            elif state == "APPROVED" and body_verdict == "with_findings":
                # 赌注最高的一条:用关键词推翻一个明确的 provider APPROVED。
                verdict = "with_findings"
                evidence = body_evidence
            elif state == "APPROVED":
                verdict = "approved"
            elif state == "COMMENTED":
                verdict = body_verdict
                evidence = body_evidence
            else:
                verdict = "no_verdict"
            provider_state_matches = (
                not required_github_state or state == required_github_state
            )
            reason = f"GitHub review {state or 'UNKNOWN'} targets requested head"
            if verdict == "approved" and not provider_state_matches:
                reason += f" but required provider state is {required_github_state}"
            candidates.append(Result(
                "delivered",
                verdict,
                result_is_terminal(verdict, "github_review", state),
                "github_review",
                review_url,
                reason,
                verdict_evidence=evidence,
            ))

    saw_stale = False
    wrong_ledger_result: Result | None = None
    for comment in snapshot.get("github_comments", []):
        if not same_identity(comment.get("user"), github_reviewer):
            continue
        if not evidence_precedes_closure(comment, request):
            continue
        text = str(comment.get("body") or "")
        if text_names_head(text, head):
            verdict, evidence = classify_prose(text)
            if ledger == "either":
                candidates.append(Result(
                    "delivered", verdict,
                    result_is_terminal(verdict, "github_comment"), "github_comment",
                    comment.get("html_url"), "GitHub issue comment targets requested head",
                    verdict_evidence=evidence,
                ))
            elif ledger == "github_review":
                wrong_ledger_result = Result(
                    "acknowledged", verdict, False, "github_comment", comment.get("html_url"),
                    "Current-head issue comment is not the required GitHub review object",
                    verdict_evidence=evidence,
                )
        if SHA_RE.search(text):
            saw_stale = True

    saw_ack = False
    for message in snapshot.get("slack_messages", []):
        if same_identity(message.get("user"), requester) or (
            slack_reviewer and not same_identity(message.get("user"), slack_reviewer)
        ):
            continue
        if not evidence_precedes_closure(message, request):
            continue
        text = str(message.get("text") or "")
        shas = SHA_RE.findall(text)
        if shas and not text_names_head(text, head):
            saw_stale = True
            continue
        if text_names_head(text, head) and text.strip():
            verdict, evidence = classify_prose(text)
            if verdict == "no_verdict" and (PROGRESS_RE.search(text) or message.get("acknowledgement")):
                saw_ack = True
                continue
            if ledger in {"slack", "either"}:
                candidates.append(Result(
                    "delivered", verdict, result_is_terminal(verdict, "slack"), "slack",
                    message.get("permalink"), "Slack result targets requested head",
                    verdict_evidence=evidence,
                ))
                continue
            wrong_ledger_result = Result(
                "acknowledged", verdict, False, "slack", message.get("permalink"),
                "Slack result names current head but completion contract requires a GitHub review",
                verdict_evidence=evidence,
            )
            continue
        if PROGRESS_RE.search(text) or message.get("acknowledgement"):
            saw_ack = True

    # A no-verdict or provider-state-mismatched result in one allowed ledger
    # must not mask a terminal result in another. Prefer terminal evidence,
    # then preserve the newest delivered evidence for diagnostics.
    for candidate in reversed(candidates):
        if candidate.terminal:
            return candidate
    if artifact_state in {"MERGED", "CLOSED"}:
        merge_commit = request.get("merge_commit_sha")
        suffix = f" at {merge_commit}" if merge_commit else ""
        return Result(
            "artifact_closed",
            "none",
            False,
            reason=f"PR is {artifact_state.lower()}{suffix}; no acceptable exact-head result remains to wait for",
        )
    if candidates:
        return candidates[-1]
    if wrong_ledger_result:
        return wrong_ledger_result

    if saw_stale:
        return Result("stale", "none", False, reason="substantive response targets another revision")
    if saw_ack:
        return Result("acknowledged", "none", False, reason="reviewer acknowledged or is working")
    return Result("silent", "none", False, reason="no qualifying reviewer response")


def classify(snapshot: dict[str, Any]) -> Result:
    try:
        return _classify(snapshot)
    except SnapshotValidationError as error:
        return Result("watcher_error", "none", False, reason=f"invalid snapshot: {error}")


def slack_replies(token: str, channel: str, thread_ts: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params = {"channel": channel, "ts": thread_ts, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        request = urllib.request.Request(
            f"https://slack.com/api/conversations.replies?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if not payload.get("ok"):
            raise RuntimeError(f"Slack conversations.replies failed: {payload.get('error', 'unknown_error')}")
        messages.extend(payload.get("messages", []))
        cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return messages


def gh_json(endpoint: str) -> Any:
    process = subprocess.run(["gh", "api", endpoint], check=True, text=True, capture_output=True)
    return json.loads(process.stdout)


def gh_json_paginated(endpoint: str) -> list[dict[str, Any]]:
    process = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        check=True,
        text=True,
        capture_output=True,
    )
    pages = json.loads(process.stdout)
    return [item for page in pages for item in page]


def gh_review_threads(repo: str, pr: int) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    query = """
      query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
        repository(owner:$owner, name:$name) {
          pullRequest(number:$number) {
            reviewThreads(first:100, after:$cursor) {
              pageInfo { hasNextPage endCursor }
              nodes { isResolved isOutdated comments(first:100) {
                nodes { author { login } body createdAt updatedAt pullRequestReview { commit { oid } } }
              } }
            }
          }
        }
      }
    """
    normalized = []
    cursor = ""
    while True:
        command = [
            "gh", "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={pr}",
        ]
        if cursor:
            command.extend(["-F", f"cursor={cursor}"])
        process = subprocess.run(command, check=True, text=True, capture_output=True)
        connection = json.loads(process.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            comments = (thread.get("comments") or {}).get("nodes") or []
            first = comments[0] if comments else {}
            normalized.append({
                "is_resolved": thread.get("isResolved"),
                "is_outdated": thread.get("isOutdated"),
                "user": (first.get("author") or {}).get("login"),
                "commit_id": (((first.get("pullRequestReview") or {}).get("commit") or {}).get("oid")),
                "created_at": first.get("createdAt"),
                "updated_at": first.get("updatedAt"),
            })
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return normalized
        cursor = page_info["endCursor"]


def live_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    slack_messages: list[dict[str, Any]] = []
    if not args.github_only:
        token = os.environ.get(args.slack_token_env)
        if not token:
            raise RuntimeError(f"missing Slack token environment variable: {args.slack_token_env}")
        slack_messages = slack_replies(token, args.channel, args.thread_ts)
    pull = gh_json(f"repos/{args.repo}/pulls/{args.pr}")
    reviews = gh_json_paginated(f"repos/{args.repo}/pulls/{args.pr}/reviews?per_page=100")
    after_review_id = getattr(args, "after_review_id", None)
    if after_review_id is not None:
        reviews = [
            review for review in reviews
            if isinstance(review.get("id"), int) and review["id"] > after_review_id
        ]
    comments = gh_json_paginated(f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100")
    threads = gh_review_threads(args.repo, args.pr)
    return {
        "request": {
            "head": args.head,
            "expected_ledger": args.expected_ledger,
            "acceptable_verdicts": args.acceptable_verdict,
            "required_github_review_state": getattr(
                args, "required_github_review_state", None
            ),
            "slack_reviewer": args.slack_reviewer,
            "requester_slack_id": args.requester_slack_id,
            "github_reviewer": args.github_reviewer,
            "remote_head": ((pull.get("head") or {}).get("sha")),
            "artifact_state": "MERGED" if pull.get("merged_at") else str(pull.get("state") or "open").upper(),
            "merged_at": pull.get("merged_at"),
            "closed_at": pull.get("closed_at"),
            "merge_commit_sha": pull.get("merge_commit_sha"),
        },
        "slack_messages": slack_messages,
        "github_reviews": [
            {
                "user": (review.get("user") or {}).get("login"),
                "commit_id": review.get("commit_id"),
                "state": review.get("state"),
                "body": review.get("body"),
                "html_url": review.get("html_url"),
                "submitted_at": review.get("submitted_at"),
            }
            for review in reviews
        ],
        "github_comments": [
            {
                "user": (comment.get("user") or {}).get("login"),
                "body": comment.get("body"),
                "html_url": comment.get("html_url"),
                "created_at": comment.get("created_at"),
                "updated_at": comment.get("updated_at"),
            }
            for comment in comments
        ],
        "review_threads": threads,
    }


def emit(result: Result) -> None:
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True), flush=True)


def terminal_exit_code(result: Result) -> int | None:
    if result.terminal:
        return 0
    if result.transport_state == "superseded":
        return 4
    if result.transport_state == "artifact_closed":
        return 5
    return None


def one_shot_exit_code(result: Result) -> int:
    exit_code = terminal_exit_code(result)
    return exit_code if exit_code is not None else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, help="classify a normalized JSON snapshot once")
    parser.add_argument("--repo")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head")
    parser.add_argument("--channel")
    parser.add_argument("--thread-ts")
    parser.add_argument("--slack-reviewer")
    parser.add_argument("--requester-slack-id")
    parser.add_argument("--github-reviewer")
    parser.add_argument(
        "--after-review-id",
        type=int,
        help=(
            "ignore GitHub reviews at or below this provider ID; use when a "
            "correction review is requested on an unchanged head"
        ),
    )
    parser.add_argument(
        "--expected-ledger",
        choices=["github_review", "slack", "either"],
        default="github_review",
        help="completion ledger; GitHub PR review objects are canonical by default",
    )
    parser.add_argument(
        "--github-only",
        action="store_true",
        help="persistently monitor a github_review ledger without reading Slack",
    )
    parser.add_argument("--acceptable-verdict", action="append", default=[])
    parser.add_argument(
        "--required-github-review-state",
        choices=["APPROVED"],
        help="require this GitHub provider action for an approved terminal result",
    )
    parser.add_argument("--slack-token-env", default="SLACK_TOKEN")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.after_review_id is not None and args.after_review_id < 0:
        parser.error("--after-review-id must be non-negative")
    if not args.acceptable_verdict:
        args.acceptable_verdict = ["approved", "with_findings"]
    if not args.snapshot:
        if args.github_only and args.expected_ledger != "github_review":
            parser.error("--github-only requires --expected-ledger github_review")
        if (
            args.required_github_review_state
            and args.expected_ledger not in {"github_review", "either"}
        ):
            parser.error(
                "--required-github-review-state requires --expected-ledger github_review or either"
            )
        required = ["repo", "pr", "head"]
        if not args.github_only:
            required.extend(["channel", "thread_ts", "slack_reviewer"])
        if args.expected_ledger in {"github_review", "either"}:
            required.append("github_reviewer")
        missing = [name for name in required if getattr(args, name) in (None, "")]
        if missing:
            parser.error("live mode requires: " + ", ".join(f"--{name.replace('_', '-')}" for name in missing))
    return args


def main() -> int:
    args = parse_args()
    if args.snapshot:
        result = classify(json.loads(args.snapshot.read_text()))
        emit(result)
        return one_shot_exit_code(result)

    deadline = time.monotonic() + args.timeout
    consecutive_errors = 0
    while True:
        try:
            result = classify(live_snapshot(args))
            result.monitoring_scope = (
                "github_only" if args.github_only else "slack_and_github"
            )
            consecutive_errors = 0
            emit(result)
            exit_code = terminal_exit_code(result)
            if args.once:
                return one_shot_exit_code(result)
            if exit_code is not None:
                return exit_code
        except Exception as error:  # every iteration owns its transient failure
            consecutive_errors += 1
            emit(Result(
                "watcher_error", "none", False, reason=str(error),
                monitoring_scope=(
                    "github_only" if args.github_only else "slack_and_github"
                ),
            ))
            if args.once or consecutive_errors >= args.max_consecutive_errors:
                return 2
        if time.monotonic() >= deadline:
            emit(Result(
                "silent", "none", False, reason="review watcher timed out",
                monitoring_scope=(
                    "github_only" if args.github_only else "slack_and_github"
                ),
            ))
            return 3
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
