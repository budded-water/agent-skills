import json
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review")
SCRIPT = ROOT / "scripts" / "build_review_snapshot.py"


def write(path: Path, value):
    path.write_text(json.dumps(value))
    return path


def test_builds_snapshot_accepted_by_watcher(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head,
        "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "acceptable_verdicts": ["approved", "with_findings"],
        "slack_reviewer": "U123",
        "requester_slack_id": "U456",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [{
        "user": "reviewer", "commit_id": head, "state": "APPROVED",
        "body": "No actionable findings.",
        "html_url": "https://example.test/reviews/1",
    }])
    snapshot = tmp_path / "snapshot.json"

    built = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews),
        "--output", str(snapshot),
    ], text=True, capture_output=True)
    assert built.returncode == 0, built.stderr

    classified = subprocess.run([
        str(ROOT / "scripts" / "watch_review.py"), "--snapshot", str(snapshot)
    ], text=True, capture_output=True)
    assert classified.returncode == 0
    assert json.loads(classified.stdout)["review_verdict"] == "approved"


def test_github_ledger_requires_reviewer_identity(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40,
        "remote_head": "a" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "slack_reviewer": "U123",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "github_reviewer" in result.stderr


def test_missing_ledger_defaults_to_github_review_without_slack_identity(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head,
        "remote_head": head,
        "artifact_state": "OPEN",
        "github_reviewer": "reviewer",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["request"]["expected_ledger"] == "github_review"


def test_missing_ledger_does_not_fall_back_to_slack(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head,
        "remote_head": head,
        "artifact_state": "OPEN",
        "slack_reviewer": "U123",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "github_reviewer" in result.stderr


def test_either_ledger_requires_github_reviewer_identity(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40,
        "remote_head": "a" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "either",
        "slack_reviewer": "U123",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "github_reviewer" in result.stderr


def test_remote_head_is_required_and_must_be_full_hex(tmp_path):
    base = {
        "head": "a" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "slack",
        "slack_reviewer": "U123",
    }
    missing = write(tmp_path / "missing.json", base)
    missing_result = subprocess.run(
        [str(SCRIPT), "--request", str(missing)], text=True, capture_output=True
    )
    assert missing_result.returncode == 2
    assert "remote_head" in missing_result.stderr

    invalid = write(tmp_path / "invalid.json", base | {"remote_head": "z" * 40})
    invalid_result = subprocess.run(
        [str(SCRIPT), "--request", str(invalid)], text=True, capture_output=True
    )
    assert invalid_result.returncode == 2
    assert "hexadecimal" in invalid_result.stderr


def test_artifact_state_is_required(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40,
        "remote_head": "a" * 40,
        "expected_ledger": "slack",
        "slack_reviewer": "U123",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "artifact_state" in result.stderr


def test_github_identity_filter_is_case_insensitive(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head,
        "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "slack_reviewer": "U123",
        "github_reviewer": "Reviewer-Login",
    })
    reviews = write(tmp_path / "reviews.json", [{
        "user": "reviewer-login", "commit_id": head, "state": "APPROVED",
        "body": "No actionable findings.",
        "html_url": "https://example.test/reviews/2",
    }])
    result = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews)
    ], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["github_reviews"]) == 1


def test_required_github_review_state_is_normalized(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head,
        "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "slack_reviewer": "U123",
        "github_reviewer": "reviewer",
        "required_github_review_state": "approved",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(result.stdout)["request"]["required_github_review_state"]
        == "APPROVED"
    )


def test_required_github_review_state_rejects_slack_only_ledger(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40,
        "remote_head": "a" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "slack",
        "slack_reviewer": "U123",
        "required_github_review_state": "APPROVED",
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "GitHub-capable ledger" in result.stderr


def test_moved_remote_head_classifies_as_superseded(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40,
        "remote_head": "b" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "acceptable_verdicts": ["approved"],
        "slack_reviewer": "U123",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [{
        "user": "reviewer", "commit_id": "a" * 40, "state": "APPROVED",
        "body": "No actionable findings.",
        "html_url": "https://example.test/reviews/3",
    }])
    snapshot = tmp_path / "snapshot.json"
    built = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews),
        "--output", str(snapshot),
    ], text=True, capture_output=True)
    assert built.returncode == 0, built.stderr

    classified = subprocess.run([
        str(ROOT / "scripts" / "watch_review.py"), "--snapshot", str(snapshot)
    ], text=True, capture_output=True)
    result = json.loads(classified.stdout)
    assert classified.returncode == 4
    assert result["transport_state"] == "superseded"
    assert result["terminal"] is False


def test_acknowledgement_snapshot_exits_nonterminal(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head, "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "slack", "slack_reviewer": "U123",
        "acceptable_verdicts": ["approved", "with_findings"],
    })
    slack = write(tmp_path / "slack.json", [{
        "user": "U123", "text": "Review started", "acknowledgement": True,
    }])
    snapshot = tmp_path / "snapshot.json"
    built = subprocess.run([
        str(SCRIPT), "--request", str(request), "--slack-messages", str(slack),
        "--output", str(snapshot),
    ], text=True, capture_output=True)
    assert built.returncode == 0, built.stderr

    classified = subprocess.run([
        str(ROOT / "scripts" / "watch_review.py"), "--snapshot", str(snapshot)
    ], text=True, capture_output=True)
    assert classified.returncode == 1
    result = json.loads(classified.stdout)
    assert result["transport_state"] == "acknowledged"
    assert result["terminal"] is False


def test_drops_unusable_historical_records_with_warnings(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head, "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review", "slack_reviewer": "U123",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [
        {"user": "reviewer", "commit_id": None, "state": "APPROVED"},
        {"user": "other", "commit_id": "short", "state": "APPROVED"},
        {
            "user": "reviewer", "commit_id": head, "state": "APPROVED",
            "body": "No actionable findings.",
            "html_url": "https://example.test/reviews/4",
        },
    ])
    slack = write(tmp_path / "slack.json", [
        {"bot_id": "B123", "text": "bot event"},
        {"user": "U123", "text": ""},
    ])
    result = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews),
        "--slack-messages", str(slack),
    ], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["github_reviews"] == [
        {
            "user": "reviewer", "commit_id": head, "state": "APPROVED",
            "body": "No actionable findings.",
            "html_url": "https://example.test/reviews/4",
        }
    ]
    assert snapshot["slack_messages"] == []
    assert len(snapshot["warnings"]) == 3

    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(result.stdout)
    classified = subprocess.run([
        str(ROOT / "scripts" / "watch_review.py"), "--snapshot", str(snapshot_path)
    ], text=True, capture_output=True)
    assert classified.returncode == 0, classified.stderr
    assert json.loads(classified.stdout)["review_verdict"] == "approved"


def test_rejects_invalid_state_on_attributed_structured_review(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head, "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review", "slack_reviewer": "U123",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [{
        "user": "reviewer", "commit_id": head, "state": "BOGUS"
    }])
    result = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews)
    ], text=True, capture_output=True)
    assert result.returncode == 2
    assert "state is invalid" in result.stderr


def test_rejects_attributed_github_review_without_url(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head, "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [{
        "user": "reviewer", "commit_id": head, "state": "APPROVED",
        "body": "No actionable findings.",
    }])
    result = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews)
    ], text=True, capture_output=True)
    assert result.returncode == 2
    assert "html_url" in result.stderr


def test_rejects_attributed_approved_review_without_body(tmp_path):
    head = "a" * 40
    request = write(tmp_path / "request.json", {
        "head": head, "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [{
        "user": "reviewer", "commit_id": head, "state": "APPROVED",
        "body": "   ",
        "html_url": "https://example.test/reviews/5",
    }])
    result = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews)
    ], text=True, capture_output=True)
    assert result.returncode == 2
    assert "body must be a non-empty string" in result.stderr


def test_historical_empty_approval_does_not_block_valid_current_review(tmp_path):
    head = "a" * 40
    old_head = "b" * 40
    request = write(tmp_path / "request.json", {
        "head": head, "remote_head": head,
        "artifact_state": "OPEN",
        "expected_ledger": "github_review",
        "github_reviewer": "reviewer",
    })
    reviews = write(tmp_path / "reviews.json", [
        {
            "user": "reviewer", "commit_id": old_head, "state": "APPROVED",
            "body": "", "html_url": "",
        },
        {
            "user": "reviewer", "commit_id": head, "state": "APPROVED",
            "body": "No actionable findings.",
            "html_url": "https://example.test/reviews/current",
        },
    ])
    snapshot = tmp_path / "snapshot.json"
    built = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-reviews", str(reviews),
        "--output", str(snapshot),
    ], text=True, capture_output=True)
    assert built.returncode == 0, built.stderr

    classified = subprocess.run([
        str(ROOT / "scripts" / "watch_review.py"), "--snapshot", str(snapshot)
    ], text=True, capture_output=True)
    assert classified.returncode == 0, classified.stderr
    assert json.loads(classified.stdout)["review_verdict"] == "approved"


def test_rejects_invalid_acceptable_verdict(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40, "remote_head": "a" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "slack", "slack_reviewer": "U123",
        "acceptable_verdicts": ["no_verdict"],
    })
    result = subprocess.run(
        [str(SCRIPT), "--request", str(request)], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "acceptable_verdicts" in result.stderr


def test_slack_ledger_drops_github_comments_without_reviewer_identity(tmp_path):
    request = write(tmp_path / "request.json", {
        "head": "a" * 40, "remote_head": "a" * 40,
        "artifact_state": "OPEN",
        "expected_ledger": "slack", "slack_reviewer": "U123",
    })
    comments = write(tmp_path / "comments.json", [{
        "user": "unrelated", "body": "Review @ bbbbbbb approved",
    }])
    result = subprocess.run([
        str(SCRIPT), "--request", str(request), "--github-comments", str(comments)
    ], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["github_comments"] == []
