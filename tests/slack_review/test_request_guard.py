import importlib.util
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


MODULE_PATH = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "request_guard.py"
SPEC = importlib.util.spec_from_file_location("request_guard", MODULE_PATH)
request_guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = request_guard
SPEC.loader.exec_module(request_guard)


class RequestGuardTest(unittest.TestCase):
    def run_guard(self, state_file, action, *extra):
        argv = [
            "request_guard.py", action,
            "--reviewer", "reviewer",
            "--repo", "owner/repo",
            "--artifact", "34",
            "--head", "abc1234def",
            "--state-file", str(state_file),
            *extra,
        ]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
            code = request_guard.main()
        return code, json.loads(output.getvalue())

    def test_duplicate_active_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                first, _ = self.run_guard(state, "register")
                check, check_payload = self.run_guard(state, "check")
                second, payload = self.run_guard(state, "register")
        self.assertEqual(first, 0)
        self.assertEqual(check, 4)
        self.assertEqual(check_payload["reason"], "duplicate_active")
        self.assertEqual(second, 4)
        self.assertFalse(payload["allowed"])

    def test_second_reviewer_requires_first_reviewer_to_leave_active_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                first, _ = self.run_guard(state, "register")
                blocked, blocked_payload = self.run_guard(
                    state, "register", "--reviewer", "other-reviewer"
                )
                self.run_guard(state, "set-state", "--state", "failed")
                second, second_payload = self.run_guard(
                    state, "register", "--reviewer", "other-reviewer"
                )
        self.assertEqual(first, 0)
        self.assertEqual(blocked, 4)
        self.assertEqual(blocked_payload["reason"], "another_reviewer_active")
        self.assertEqual(second, 0)
        self.assertEqual(second_payload["record"]["reviewer"], "other-reviewer")

    def test_second_reviewer_can_register_after_acknowledgement_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
                self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
            with mock.patch.object(request_guard.time, "time", return_value=160):
                expired, expired_payload = self.run_guard(state, "check")
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                second, second_payload = self.run_guard(
                    state, "register", "--reviewer", "other-reviewer"
                )
        self.assertEqual(expired, 4)
        self.assertEqual(
            expired_payload["record"]["expiration_reason"],
            "acknowledgement_timeout",
        )
        self.assertEqual(second, 0)
        self.assertEqual(second_payload["record"]["reviewer"], "other-reviewer")

    def test_late_acknowledgement_cannot_reactivate_failed_over_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
                self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
            with mock.patch.object(request_guard.time, "time", return_value=160):
                self.run_guard(state, "check")
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(state, "register", "--reviewer", "other-reviewer")
                blocked, blocked_payload = self.run_guard(
                    state, "set-state", "--state", "acknowledged"
                )
            requests = json.loads(state.read_text())["requests"]
        self.assertEqual(blocked, 4)
        self.assertEqual(blocked_payload["reason"], "another_reviewer_active")
        self.assertEqual(
            requests["reviewer:owner/repo:34:abc1234def"]["state"], "expired"
        )
        self.assertEqual(
            requests["other-reviewer:owner/repo:34:abc1234def"]["state"],
            "requested",
        )

    def test_remote_head_mismatch_blocks_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="fffffff000"):
                code, payload = self.run_guard(state, "register")
        self.assertEqual(code, 5)
        self.assertEqual(payload["reason"], "superseded")

    def test_remote_head_mismatch_persists_active_request_as_superseded(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(state, "register")
            with mock.patch.object(request_guard, "remote_head", return_value="fffffff000"):
                code, payload = self.run_guard(state, "register")
            persisted = json.loads(state.read_text())["requests"]
        self.assertEqual(code, 5)
        self.assertEqual(payload["record"]["state"], "superseded")
        self.assertEqual(next(iter(persisted.values()))["state"], "superseded")

    def test_delivered_identity_blocks_duplicate_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(state, "register")
                self.run_guard(state, "set-state", "--state", "delivered")
                code, payload = self.run_guard(state, "register")
        self.assertEqual(code, 4)
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["reason"], "already_delivered")

    def test_requested_identity_uses_one_minute_acknowledgement_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
                self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
            with mock.patch.object(request_guard.time, "time", return_value=159):
                active, active_payload = self.run_guard(state, "check")
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=160),
            ):
                code, payload = self.run_guard(state, "register")
        self.assertEqual(active, 4)
        self.assertEqual(active_payload["reason"], "duplicate_active")
        self.assertEqual(code, 4)
        self.assertEqual(payload["reason"], "expired_requires_reconciliation")
        self.assertEqual(
            payload["record"]["expiration_reason"], "acknowledgement_timeout"
        )
        self.assertEqual(payload["record"]["timeout_seconds"], 60)

    def test_pending_post_does_not_consume_acknowledgement_window(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
            with mock.patch.object(request_guard.time, "time", return_value=160):
                pending, pending_payload = self.run_guard(state, "check")
            with mock.patch.object(request_guard.time, "time", return_value=170):
                confirmed, confirmed_payload = self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
            with mock.patch.object(request_guard.time, "time", return_value=229):
                active, active_payload = self.run_guard(state, "check")
            with mock.patch.object(request_guard.time, "time", return_value=230):
                expired, expired_payload = self.run_guard(state, "check")
        self.assertEqual(pending, 4)
        self.assertEqual(pending_payload["reason"], "duplicate_active")
        self.assertEqual(pending_payload["record"]["state"], "requested")
        self.assertEqual(confirmed, 0)
        self.assertEqual(confirmed_payload["record"]["request_url"], "https://slack.test/posted-message")
        self.assertEqual(active, 4)
        self.assertEqual(active_payload["reason"], "duplicate_active")
        self.assertEqual(expired, 4)
        self.assertEqual(expired_payload["record"]["expiration_reason"], "acknowledgement_timeout")

    def test_unconfirmed_post_lease_expires_without_calling_bot_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
            with mock.patch.object(request_guard.time, "time", return_value=399):
                pending, pending_payload = self.run_guard(state, "check")
            with mock.patch.object(request_guard.time, "time", return_value=400):
                expired, expired_payload = self.run_guard(state, "check")
        self.assertEqual(pending, 4)
        self.assertEqual(pending_payload["record"]["state"], "requested")
        self.assertEqual(expired, 4)
        self.assertEqual(expired_payload["record"]["expiration_reason"], "posting_timeout")
        self.assertEqual(expired_payload["record"]["timeout_seconds"], 300)

    def test_legacy_requested_record_keeps_acknowledgement_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            key = "reviewer:owner/repo:34:abc1234def"
            state.write_text(json.dumps({
                "version": 2,
                "requests": {key: {
                    "reviewer": "reviewer",
                    "repo": "owner/repo",
                    "artifact": "34",
                    "head": "abc1234def",
                    "state": "requested",
                    "updated_at": 100,
                }},
            }))
            with mock.patch.object(request_guard.time, "time", return_value=160):
                expired, payload = self.run_guard(state, "check")
        self.assertEqual(expired, 4)
        self.assertEqual(payload["record"]["expiration_reason"], "acknowledgement_timeout")
        self.assertEqual(payload["record"]["timeout_seconds"], 60)

    def test_verified_post_restarts_acknowledgement_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
            with mock.patch.object(request_guard.time, "time", return_value=145):
                posted, payload = self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
            with mock.patch.object(request_guard.time, "time", return_value=160):
                pending, pending_payload = self.run_guard(state, "check")
            with mock.patch.object(request_guard.time, "time", return_value=205):
                expired, expired_payload = self.run_guard(state, "check")
        self.assertEqual(posted, 0)
        self.assertEqual(payload["record"]["request_url"], "https://slack.test/posted-message")
        self.assertNotIn("result_url", payload["record"])
        self.assertEqual(pending, 4)
        self.assertEqual(pending_payload["reason"], "duplicate_active")
        self.assertEqual(expired, 4)
        self.assertEqual(expired_payload["reason"], "expired_requires_reconciliation")
        self.assertEqual(expired_payload["record"]["expiration_reason"], "acknowledgement_timeout")

    def test_request_delivery_confirmation_is_one_shot(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
            missing, missing_payload = self.run_guard(
                state, "set-state", "--state", "requested"
            )
            with mock.patch.object(request_guard.time, "time", return_value=145):
                first, first_payload = self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
            with mock.patch.object(request_guard.time, "time", return_value=180):
                repeated, repeated_payload = self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/posted-message",
                )
                changed, changed_payload = self.run_guard(
                    state, "set-state", "--state", "requested",
                    "--request-url", "https://slack.test/another-message",
                )
            self.run_guard(state, "set-state", "--state", "delivered")
            late, late_payload = self.run_guard(
                state, "set-state", "--state", "requested",
                "--request-url", "https://slack.test/posted-message",
            )
        self.assertEqual(missing_payload["reason"], "verified_request_url_required")
        self.assertEqual(missing, 4)
        self.assertEqual(first, 0)
        self.assertEqual(repeated, 0)
        self.assertEqual(repeated_payload["reason"], "already_recorded")
        self.assertEqual(repeated_payload["record"]["updated_at"], first_payload["record"]["updated_at"])
        self.assertEqual(changed, 4)
        self.assertEqual(changed_payload["reason"], "request_delivery_already_recorded")
        self.assertEqual(late, 4)
        self.assertEqual(late_payload["reason"], "request_not_awaiting_delivery")

    def test_acknowledged_identity_uses_longer_result_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
                self.run_guard(state, "set-state", "--state", "acknowledged")
            with mock.patch.object(request_guard.time, "time", return_value=699):
                active, active_payload = self.run_guard(state, "check")
            with mock.patch.object(request_guard.time, "time", return_value=700):
                expired, expired_payload = self.run_guard(state, "check")
        self.assertEqual(active, 4)
        self.assertEqual(active_payload["reason"], "duplicate_active")
        self.assertEqual(expired, 4)
        self.assertEqual(
            expired_payload["record"]["expiration_reason"], "result_timeout"
        )
        self.assertEqual(expired_payload["record"]["timeout_seconds"], 600)

    def test_reconciled_acknowledgement_does_not_expire_before_set_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
            with mock.patch.object(request_guard.time, "time", return_value=161):
                code, payload = self.run_guard(
                    state, "set-state", "--state", "acknowledged"
                )
        self.assertEqual(code, 0)
        self.assertEqual(payload["record"]["state"], "acknowledged")
        self.assertNotIn("expiration_reason", payload["record"])
        self.assertNotIn("timeout_seconds", payload["record"])

    def test_reconciled_evidence_clears_a_provisional_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
            ):
                self.run_guard(state, "register")
            with mock.patch.object(request_guard.time, "time", return_value=160):
                self.run_guard(state, "check")
            with mock.patch.object(request_guard.time, "time", return_value=161):
                code, payload = self.run_guard(
                    state, "set-state", "--state", "in_progress"
                )
        self.assertEqual(code, 0)
        self.assertEqual(payload["record"]["state"], "in_progress")
        self.assertNotIn("expiration_reason", payload["record"])
        self.assertNotIn("timeout_seconds", payload["record"])

    def test_legacy_silence_timeout_remains_a_compatibility_override(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(state, "register")
                with mock.patch.object(request_guard.time, "time", return_value=9999999999):
                    code, payload = self.run_guard(
                        state, "register", "--silence-timeout", "1"
                    )
        self.assertEqual(code, 4)
        self.assertEqual(payload["reason"], "expired_requires_reconciliation")
        self.assertEqual(
            payload["record"]["expiration_reason"], "legacy_silence_timeout"
        )

    def test_timeouts_must_be_positive(self):
        for flag in ("--ack-timeout", "--result-timeout", "--silence-timeout"):
            with self.subTest(flag=flag):
                argv = [
                    "request_guard.py", "check",
                    "--reviewer", "reviewer",
                    "--repo", "owner/repo",
                    "--artifact", "34",
                    "--head", "abc1234def",
                    flag, "0",
                ]
                with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                    request_guard.parse_args()

    def test_artifact_closed_identity_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(state, "register")
                self.run_guard(
                    state, "set-state", "--state", "artifact_closed",
                    "--terminal-reason", "PR merged before a qualifying result",
                )
                check, check_payload = self.run_guard(state, "check")
                registered, register_payload = self.run_guard(state, "register")
        self.assertEqual((check, registered), (4, 4))
        self.assertEqual(
            check_payload["reason"], "artifact_closed_requires_reconciliation"
        )
        self.assertEqual(
            register_payload["reason"], "artifact_closed_requires_reconciliation"
        )

    def test_request_and_result_metadata_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                code, registered = self.run_guard(
                    state, "register",
                    "--github-reviewer", "example-reviewer",
                    "--expected-ledger", "github_review",
                    "--required-github-review-state", "APPROVED",
                    "--channel", "C000REVIEW",
                )
                delivered, result = self.run_guard(
                    state, "set-state", "--state", "delivered",
                    "--thread-ts", "1787373197.811679",
                    "--result-url", "https://example.test/review/1",
                    "--terminal-reason", "exact-head approval",
                )
        self.assertEqual((code, delivered), (0, 0))
        self.assertEqual(registered["record"]["github_reviewer"], "example-reviewer")
        self.assertEqual(
            registered["record"]["required_github_review_state"], "APPROVED"
        )
        self.assertEqual(result["record"]["thread_ts"], "1787373197.811679")
        self.assertEqual(result["record"]["result_url"], "https://example.test/review/1")
        self.assertEqual(result["record"]["terminal_reason"], "exact-head approval")

    def test_required_github_state_rejects_non_github_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            argv = [
                "request_guard.py", "register",
                "--reviewer", "reviewer",
                "--repo", "owner/repo",
                "--artifact", "34",
                "--head", "abc1234def",
                "--state-file", str(state),
                "--expected-ledger", "slack",
                "--required-github-review-state", "APPROVED",
            ]
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                request_guard.parse_args()

    def test_required_github_state_rejects_missing_reviewer_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            argv = [
                "request_guard.py", "register",
                "--reviewer", "reviewer",
                "--repo", "owner/repo",
                "--artifact", "34",
                "--head", "abc1234def",
                "--state-file", str(state),
                "--expected-ledger", "github_review",
                "--required-github-review-state", "APPROVED",
            ]
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                request_guard.parse_args()

    def test_verified_host_recovery_resumes_the_same_request_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "requests.json"
            recovery = Path(directory) / "recoveries.json"
            key = "reviewer:owner/repo:34:abc1234def"
            condition = "The bot can write a GitHub review object."
            condition_digest = hashlib.sha256(condition.encode()).hexdigest()
            recovery.write_text(json.dumps({
                "version": 1,
                "escalations": {
                    f"{key}:github-review-write-401": {
                        "request_identity": key,
                        "state": "recovered",
                        "dependency_state": "ready_to_resume",
                        "completion_contract_unchanged": True,
                        "expected_ledger": "github_review",
                        "recovery_condition": condition,
                        "recovery_condition_hash": condition_digest,
                        "recovery_mode": "trusted_probe",
                        "recovery_probe_id": "trusted-test-probe",
                        "recovery_probe_hash": "d" * 64,
                        "recovery_probe_executable_hash": "e" * 64,
                        "recovery_probe_trust_hash": "f" * 64,
                        "recovery_attestation": {
                            "attestation_id": "a" * 32,
                            "succeeded": True,
                            "probe_exit_code": 0,
                            "probe_hash": "d" * 64,
                            "probe_executable_hash": "e" * 64,
                            "probe_trust_hash": "f" * 64,
                            "condition_hash": condition_digest,
                            "observed_at": 9999999999,
                            "observed_at_ns": 9999999999999999999,
                        },
                    }
                },
            }))
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(
                    state, "register",
                    "--expected-ledger", "github_review",
                    "--required-github-review-state", "APPROVED",
                    "--github-reviewer", "review-bot-login",
                    "--channel", "C012ABC",
                )
                self.run_guard(state, "set-state", "--state", "failed")
                code, payload = self.run_guard(
                    state,
                    "register",
                    "--host-recovery-state-file", str(recovery),
                    "--host-failure-code", "github-review-write-401",
                )
        self.assertEqual(code, 0)
        self.assertEqual(payload["identity"], key)
        self.assertEqual(
            payload["record"]["host_recovery"]["failure_code"],
            "github-review-write-401",
        )
        self.assertEqual(payload["record"]["expected_ledger"], "github_review")
        self.assertEqual(
            payload["record"]["required_github_review_state"], "APPROVED"
        )
        self.assertEqual(payload["record"]["github_reviewer"], "review-bot-login")
        self.assertEqual(payload["record"]["channel"], "C012ABC")

    def test_unattested_host_recovery_cannot_resume_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "requests.json"
            recovery = Path(directory) / "recoveries.json"
            recovery.write_text(json.dumps({"version": 1, "escalations": {}}))
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                code, payload = self.run_guard(
                    state,
                    "register",
                    "--host-recovery-state-file", str(recovery),
                    "--host-failure-code", "github-review-write-401",
                )
        self.assertEqual(code, 7)
        self.assertEqual(payload["reason"], "invalid_host_recovery_attestation")

    def test_malformed_host_recovery_containers_fail_closed(self):
        key = "reviewer:owner/repo:34:abc1234def"
        failure_code = "github-review-write-401"
        cases = (
            ([], "envelope is invalid"),
            ({"escalations": []}, "escalations are invalid"),
            ({"escalations": {f"{key}:{failure_code}": []}}, "record is absent or invalid"),
            ({
                "escalations": {
                    f"{key}:{failure_code}": {"recovery_attestation": []}
                }
            }, "attestation is absent or invalid"),
        )
        with tempfile.TemporaryDirectory() as directory:
            recovery = Path(directory) / "recoveries.json"
            for payload, error in cases:
                with self.subTest(error=error):
                    recovery.write_text(json.dumps(payload))
                    with self.assertRaisesRegex(ValueError, error):
                        request_guard.consume_host_recovery(
                            recovery, key, failure_code
                        )

    def test_malformed_request_state_containers_fail_closed_at_cli_entry(self):
        key = "reviewer:owner/repo:34:abc1234def"
        payloads = (
            [],
            True,
            {"requests": []},
            {"requests": {key: []}},
            {"requests": {key: None}},
        )
        actions = (
            ("register", ()),
            ("set-state", ("--state", "failed")),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "requests.json"
            for payload in payloads:
                for action, arguments in actions:
                    with self.subTest(payload=payload, action=action):
                        state.write_text(json.dumps(payload))
                        original = state.read_bytes()
                        code, result = self.run_guard(state, action, *arguments)
                        self.assertEqual(code, 2)
                        self.assertFalse(result["allowed"])
                        self.assertEqual(result["reason"], "invalid_request_state")
                        self.assertEqual(state.read_bytes(), original)

    def test_old_recovery_attestation_cannot_unlock_a_later_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "requests.json"
            recovery = Path(directory) / "recoveries.json"
            key = "reviewer:owner/repo:34:abc1234def"
            condition = "The bot can write a GitHub review object."
            condition_digest = hashlib.sha256(condition.encode()).hexdigest()
            recovery.write_text(json.dumps({
                "version": 1,
                "escalations": {f"{key}:github-review-write-401": {
                    "request_identity": key,
                    "state": "recovered",
                    "dependency_state": "ready_to_resume",
                    "completion_contract_unchanged": True,
                    "expected_ledger": "github_review",
                    "recovery_condition": condition,
                    "recovery_condition_hash": condition_digest,
                    "recovery_mode": "trusted_probe",
                    "recovery_probe_id": "trusted-test-probe",
                    "recovery_probe_hash": "d" * 64,
                    "recovery_probe_executable_hash": "e" * 64,
                    "recovery_probe_trust_hash": "f" * 64,
                    "recovery_attestation": {
                        "attestation_id": "b" * 32,
                        "succeeded": True,
                        "probe_exit_code": 0,
                        "probe_hash": "d" * 64,
                        "probe_executable_hash": "e" * 64,
                        "probe_trust_hash": "f" * 64,
                        "condition_hash": condition_digest,
                        "observed_at": 1,
                        "observed_at_ns": 1,
                    },
                }},
            }))
            with mock.patch.object(request_guard, "remote_head", return_value="abc1234def"):
                self.run_guard(
                    state, "register", "--expected-ledger", "github_review"
                )
                self.run_guard(state, "set-state", "--state", "failed")
                code, payload = self.run_guard(
                    state,
                    "register",
                    "--host-recovery-state-file", str(recovery),
                    "--host-failure-code", "github-review-write-401",
                )
        self.assertEqual(code, 7)
        self.assertEqual(
            payload["reason"], "host_recovery_does_not_follow_failed_request"
        )

    def test_legacy_failed_request_without_nanoseconds_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "requests.json"
            recovery = Path(directory) / "recoveries.json"
            key = "reviewer:owner/repo:34:abc1234def"
            condition = "The bot can write a GitHub review object."
            condition_digest = hashlib.sha256(condition.encode()).hexdigest()
            state.write_text(json.dumps({
                "version": 2,
                "requests": {key: {
                    "reviewer": "reviewer",
                    "repo": "owner/repo",
                    "artifact": "34",
                    "head": "abc1234def",
                    "state": "failed",
                    "updated_at": 100,
                    "expected_ledger": "github_review",
                }},
            }))
            recovery.write_text(json.dumps({
                "version": 1,
                "escalations": {f"{key}:github-review-write-401": {
                    "request_identity": key,
                    "state": "recovered",
                    "dependency_state": "ready_to_resume",
                    "completion_contract_unchanged": True,
                    "expected_ledger": "github_review",
                    "recovery_condition": condition,
                    "recovery_condition_hash": condition_digest,
                    "recovery_mode": "trusted_probe",
                    "recovery_probe_id": "trusted-test-probe",
                    "recovery_probe_hash": "d" * 64,
                    "recovery_probe_executable_hash": "e" * 64,
                    "recovery_probe_trust_hash": "f" * 64,
                    "recovery_attestation": {
                        "attestation_id": "f" * 32,
                        "succeeded": True,
                        "probe_exit_code": 0,
                        "probe_hash": "d" * 64,
                        "probe_executable_hash": "e" * 64,
                        "probe_trust_hash": "f" * 64,
                        "condition_hash": condition_digest,
                        "observed_at": 9999999999,
                        "observed_at_ns": 9999999999999999999,
                    },
                }},
            }))
            with mock.patch.object(
                request_guard, "remote_head", return_value="abc1234def"
            ):
                code, payload = self.run_guard(
                    state,
                    "register",
                    "--host-recovery-state-file", str(recovery),
                    "--host-failure-code", "github-review-write-401",
                )
        self.assertEqual(code, 7)
        self.assertEqual(
            payload["reason"], "host_recovery_does_not_follow_failed_request"
        )

    def test_recovery_attestation_is_one_time_even_within_one_second(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "requests.json"
            recovery = Path(directory) / "recoveries.json"
            key = "reviewer:owner/repo:34:abc1234def"
            condition = "The bot can write a GitHub review object."
            condition_digest = hashlib.sha256(condition.encode()).hexdigest()
            recovery.write_text(json.dumps({
                "version": 1,
                "escalations": {f"{key}:github-review-write-401": {
                    "request_identity": key,
                    "state": "recovered",
                    "dependency_state": "ready_to_resume",
                    "completion_contract_unchanged": True,
                    "expected_ledger": "github_review",
                    "recovery_condition": condition,
                    "recovery_condition_hash": condition_digest,
                    "recovery_mode": "trusted_probe",
                    "recovery_probe_id": "trusted-test-probe",
                    "recovery_probe_hash": "d" * 64,
                    "recovery_probe_executable_hash": "e" * 64,
                    "recovery_probe_trust_hash": "f" * 64,
                    "recovery_attestation": {
                        "attestation_id": "c" * 32,
                        "succeeded": True,
                        "probe_exit_code": 0,
                        "probe_hash": "d" * 64,
                        "probe_executable_hash": "e" * 64,
                        "probe_trust_hash": "f" * 64,
                        "condition_hash": condition_digest,
                        "observed_at": 9999999999,
                        "observed_at_ns": 9999999999999999999,
                    },
                }},
            }))
            with (
                mock.patch.object(request_guard, "remote_head", return_value="abc1234def"),
                mock.patch.object(request_guard.time, "time", return_value=100),
                mock.patch.object(request_guard.time, "time_ns", return_value=100_000_000_000),
            ):
                self.run_guard(
                    state, "register", "--expected-ledger", "github_review"
                )
                self.run_guard(state, "set-state", "--state", "failed")
                first, _ = self.run_guard(
                    state,
                    "register",
                    "--host-recovery-state-file", str(recovery),
                    "--host-failure-code", "github-review-write-401",
                )
                self.run_guard(state, "set-state", "--state", "failed")
                second, payload = self.run_guard(
                    state,
                    "register",
                    "--host-recovery-state-file", str(recovery),
                    "--host-failure-code", "github-review-write-401",
                )
        self.assertEqual((first, second), (0, 7))
        self.assertEqual(
            payload["reason"], "host_recovery_does_not_follow_failed_request"
        )


if __name__ == "__main__":
    unittest.main()
