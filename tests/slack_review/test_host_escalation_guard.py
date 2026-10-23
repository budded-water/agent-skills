import importlib.util
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


MODULE_PATH = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "host_escalation_guard.py"
SPEC = importlib.util.spec_from_file_location("host_escalation_guard", MODULE_PATH)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


HEAD = "a" * 40


def test_reconciliation_requires_explicit_credentials(monkeypatch):
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_MCP_XOXP_TOKEN", raising=False)
    with mock.patch.object(Path, "home", side_effect=AssertionError("private client config accessed")):
        with unittest.TestCase().assertRaisesRegex(ValueError, "explicitly"):
            guard.load_slack_token()
    monkeypatch.setenv("SLACK_TOKEN", "explicit-fixture")
    assert guard.load_slack_token() == "explicit-fixture"


def test_explicit_workspace_resolver_contract_is_used(monkeypatch):
    monkeypatch.setenv("SLACK_REVIEW_RESOLVER", "/trusted/workspace/resolve-reviewer")
    response = json.dumps({"reviewer": "example-reviewer", "slack_user_id": "U000BOT",
                           "host": {"slack_user_id": "U000HOST", "role": "operator"}})
    with mock.patch.object(guard.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=response)) as run:
        assert guard.resolve_host("example-reviewer") == {"slack_user_id": "U000HOST", "role": "operator"}
    assert run.call_args.args[0] == ["/trusted/workspace/resolve-reviewer", "--json", "--require-host", "example-reviewer"]


class HostEscalationGuardTest(unittest.TestCase):
    @staticmethod
    def write_probe(path, attestation, *, exit_code=0):
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' {shlex.quote(json.dumps(attestation))}\n"
            f"exit {exit_code}\n"
        )
        path.chmod(0o755)

    @staticmethod
    def reconciliation_record():
        record = {
            "reviewer": "example-reviewer",
            "repo": "owner/repo",
            "artifact": "5553",
            "artifact_url": "https://github.com/owner/repo/pull/5553",
            "head": HEAD,
            "failure_code": "github-review-write-401",
            "failure_fingerprint": "abc123fingerprint",
            "channel": "C000REVIEW",
            "thread_ts": "1787708124.013139",
            "host_slack_user_id": "U000HOST",
            "sender_slack_user_id": "U000SENDER",
            "evidence": "GitHub review submission returned HTTP 401.",
            "recovery_action": "Reconnect the bot GitHub integration.",
            "recovery_condition": "The bot can submit a GitHub review object.",
        }
        notification_message = guard.build_message(record)
        record["notification_message"] = notification_message
        record["notification_message_hash"] = guard.message_hash(
            notification_message
        )
        return record

    def run_guard(
        self,
        state_file,
        action,
        *extra,
        resolver_result=None,
        probe_result=None,
        reconciliation_result=None,
        sender_result="U000SENDER",
    ):
        argv = [
            "host_escalation_guard.py", action,
            "--reviewer", "example-reviewer",
            "--repo", "owner/repo",
            "--artifact", "5553",
            "--head", HEAD,
            "--state-file", str(state_file),
            *extra,
        ]
        output = io.StringIO()
        resolver_result = resolver_result or {
            "slack_user_id": "U000HOST",
            "role": "operator",
        }
        probe_result = probe_result or {
            "succeeded": True,
            "exit_code": 0,
            "evidence": "probe succeeded",
        }
        resolver_patch = mock.patch.object(
            guard,
            "resolve_host",
            side_effect=resolver_result if isinstance(resolver_result, Exception) else None,
            return_value=None if isinstance(resolver_result, Exception) else resolver_result,
        )
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(guard, "run_recovery_probe", return_value=probe_result),
            mock.patch.object(
                guard, "authorize_recovery_probe", return_value="f" * 64
            ),
            mock.patch.object(
                guard,
                "reconcile_slack_notification",
                return_value=reconciliation_result,
            ),
            mock.patch.object(
                guard, "resolve_slack_sender", return_value=sender_result
            ),
            resolver_patch,
            redirect_stdout(output),
        ):
            try:
                code = guard.main()
            except ValueError as error:
                print(json.dumps({"error": str(error), "allowed": False}))
                code = 2
        return code, json.loads(output.getvalue())

    @staticmethod
    def host_registration_args(failure_code="github-review-write-401"):
        return (
            "--classification", "host_action_required",
            "--failure-code", failure_code,
            "--host-environment-confirmed",
            "--host-owns-recovery",
            "--self-recovery-unavailable",
            "--evidence", "GitHub review submission returned HTTP 401.",
            "--recovery-action", "Reconnect the bot GitHub integration.",
            "--recovery-condition", "The bot can submit a GitHub review object.",
            "--recovery-probe-id", "trusted-test-probe",
            "--recovery-probe", "true",
            "--expected-ledger", "github_review",
            "--channel", "C000REVIEW",
            "--thread-ts", "1787708124.013139",
        )

    def test_fault_owner_routes_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            expected = {
                "agent_recoverable": "recover_directly",
                "transient_retryable": "retry_bounded",
                "user_decision_required": "ask_user",
                "external_provider_outage": "monitor_provider",
            }
            for classification, route in expected.items():
                code, payload = self.run_guard(
                    state, "assess", "--classification", classification
                )
                self.assertEqual(code, 0)
                self.assertEqual(payload["route"], route)
                self.assertNotEqual(payload["route"], "escalate_host")

    def test_host_route_requires_all_ownership_predicates(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            code, payload = self.run_guard(
                state, "assess",
                "--classification", "host_action_required",
                "--host-environment-confirmed",
            )
        self.assertEqual(code, 2)
        self.assertFalse(payload["eligible"])
        self.assertIn("--host-owns-recovery", payload["missing_requirements"])
        self.assertTrue(payload["requires_live_host_verification"])

    def test_register_is_deduplicated_and_keeps_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            first, payload = self.run_guard(
                state, "register", *self.host_registration_args()
            )
            second, duplicate = self.run_guard(
                state, "register", *self.host_registration_args()
            )
        self.assertEqual(first, 0)
        self.assertEqual(second, 4)
        self.assertEqual(duplicate["reason"], "send_in_progress")
        self.assertEqual(payload["record"]["dependency_state"], "waiting_on_host")
        self.assertTrue(payload["record"]["continue_independent_work"])
        self.assertTrue(payload["record"]["completion_contract_unchanged"])
        self.assertEqual(payload["record"]["expected_ledger"], "github_review")
        self.assertEqual(
            payload["record"]["notification_message"], payload["slack_message"]
        )
        self.assertEqual(
            payload["record"]["notification_message_hash"],
            guard.message_hash(payload["slack_message"]),
        )
        self.assertEqual(payload["slack_message"].count("<@U000HOST>"), 1)

    def test_unsent_registration_can_retry_after_thread_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(guard.time, "time", return_value=100):
                first, original = self.run_guard(
                    state, "register", *self.host_registration_args()
                )
            with mock.patch.object(guard.time, "time", return_value=200):
                blocked, reconciliation = self.run_guard(
                    state, "register", *self.host_registration_args()
                )
                retried, retry = self.run_guard(
                    state,
                    "register",
                    *self.host_registration_args(),
                    "--reconcile-thread",
                )
        self.assertEqual((first, blocked, retried), (0, 5, 0))
        self.assertEqual(
            reconciliation["reason"],
            "unsent_requires_live_thread_reconciliation",
        )
        self.assertEqual(retry["reason"], "retry_after_thread_reconciliation")
        self.assertEqual(retry["slack_message"], original["slack_message"])
        self.assertEqual(retry["record"]["send_attempt"], 2)

    def test_unsent_retry_reverifies_the_same_host_relationship(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(guard.time, "time", return_value=100):
                self.run_guard(state, "register", *self.host_registration_args())
            with mock.patch.object(guard.time, "time", return_value=200):
                code, payload = self.run_guard(
                    state,
                    "register",
                    *self.host_registration_args(),
                    "--reconcile-thread",
                    resolver_result={
                        "slack_user_id": "U0999999999",
                        "role": "operator",
                    },
                )
        self.assertEqual(code, 2)
        self.assertIn("relationship changed", payload["error"])

    def test_thread_reconciliation_persists_existing_notification(self):
        permalink = (
            "https://example.slack.com/archives/"
            "C000REVIEW/p1787708124013139"
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with mock.patch.object(guard.time, "time", return_value=100):
                self.run_guard(state, "register", *self.host_registration_args())
            with mock.patch.object(guard.time, "time", return_value=200):
                code, payload = self.run_guard(
                    state,
                    "register",
                    *self.host_registration_args(),
                    "--reconcile-thread",
                    reconciliation_result=permalink,
                )
        self.assertEqual(code, 0)
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["reason"], "existing_notification_reconciled")
        self.assertEqual(payload["record"]["state"], "notified")
        self.assertEqual(payload["record"]["result_url"], permalink)
        self.assertNotIn("slack_message", payload)

    def test_notified_state_confirms_only_the_authenticated_exact_message(self):
        permalink = (
            "https://example.slack.com/archives/"
            "C000REVIEW/p1787708124013139"
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.run_guard(state, "register", *self.host_registration_args())
            code, payload = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "notified",
                "--result-url", permalink,
                reconciliation_result=permalink,
            )
        self.assertEqual(code, 0)
        self.assertEqual(payload["record"]["notification_state"], "confirmed")
        self.assertEqual(payload["record"]["result_url"], permalink)

    def test_notified_state_rejects_an_unrelated_permalink(self):
        actual_permalink = (
            "https://example.slack.com/archives/"
            "C000REVIEW/p1787708124013139"
        )
        unrelated_permalink = (
            "https://example.slack.com/archives/"
            "C000REVIEW/p1787708124999999"
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.run_guard(state, "register", *self.host_registration_args())
            code, payload = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "notified",
                "--result-url", unrelated_permalink,
                reconciliation_result=actual_permalink,
            )
            persisted = json.loads(state.read_text())
        self.assertEqual(code, 2)
        self.assertIn("different notification permalink", payload["error"])
        record = next(iter(persisted["escalations"].values()))
        self.assertEqual(record["notification_state"], "pending_send")
        self.assertNotIn("result_url", record)

    def test_thread_lookup_finds_the_original_fingerprinted_message(self):
        record = self.reconciliation_record()
        permalink = (
            "https://example.slack.com/archives/"
            "C000REVIEW/p1787708124013139"
        )
        replies = {
            "ok": True,
            "messages": [{
                "ts": "1787708124.013139",
                "user": "U000SENDER",
                "text": record["notification_message"],
            }],
            "response_metadata": {"next_cursor": ""},
        }
        with mock.patch.object(
            guard,
            "slack_api",
            side_effect=[replies, {"ok": True, "permalink": permalink}],
        ) as slack_api:
            result = guard.reconcile_slack_notification(record)
        self.assertEqual(result, permalink)
        self.assertEqual(slack_api.call_count, 2)

    def test_thread_lookup_ignores_forged_matching_message(self):
        record = self.reconciliation_record()
        replies = {
            "ok": True,
            "messages": [{
                "ts": "1787708124.013139",
                "user": "U0999999999",
                "text": record["notification_message"],
            }],
            "response_metadata": {"next_cursor": ""},
        }
        with mock.patch.object(
            guard, "slack_api", return_value=replies
        ) as slack_api:
            result = guard.reconcile_slack_notification(record)
        self.assertIsNone(result)
        self.assertEqual(slack_api.call_count, 1)

    def test_thread_lookup_ignores_same_sender_with_altered_body(self):
        record = self.reconciliation_record()
        altered_message = record["notification_message"].replace(
            "Reconnect the bot GitHub integration.",
            "No host action supplied.",
        )
        replies = {
            "ok": True,
            "messages": [{
                "ts": "1787708124.013139",
                "user": "U000SENDER",
                "text": altered_message,
            }],
            "response_metadata": {"next_cursor": ""},
        }
        with mock.patch.object(
            guard, "slack_api", return_value=replies
        ) as slack_api:
            result = guard.reconcile_slack_notification(record)
        self.assertIsNone(result)
        self.assertEqual(slack_api.call_count, 1)

    def test_thread_lookup_rejects_a_legacy_unbound_record(self):
        record = self.reconciliation_record()
        record.pop("notification_message")
        record.pop("notification_message_hash")
        with (
            mock.patch.object(guard, "slack_api") as slack_api,
            self.assertRaisesRegex(ValueError, "lacks a persisted"),
        ):
            guard.reconcile_slack_notification(record)
        slack_api.assert_not_called()

    def test_distinct_stable_failure_code_creates_distinct_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            first, one = self.run_guard(
                state, "register", *self.host_registration_args()
            )
            second, two = self.run_guard(
                state, "register",
                *self.host_registration_args("local-review-daemon-stopped"),
            )
        self.assertEqual((first, second), (0, 0))
        self.assertNotEqual(
            one["record"]["failure_fingerprint"],
            two["record"]["failure_fingerprint"],
        )

    def test_non_host_classification_cannot_register_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            args = list(self.host_registration_args())
            args[1] = "transient_retryable"
            code, payload = self.run_guard(state, "register", *args)
        self.assertEqual(code, 3)
        self.assertEqual(payload["reason"], "classification_does_not_route_to_host")

    def test_escalation_without_trusted_probe_cannot_unlock_resume(self):
        permalink = (
            "https://example.slack.com/archives/"
            "C000REVIEW/p1787708124013139"
        )
        args = list(self.host_registration_args())
        for option in ("--recovery-probe-id", "--recovery-probe"):
            index = args.index(option)
            del args[index:index + 2]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            registered, registration = self.run_guard(
                state, "register", *args
            )
            self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "notified",
                "--result-url", permalink,
                reconciliation_result=permalink,
            )
            recovered, payload = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "recovered",
            )
        self.assertEqual((registered, recovered), (0, 7))
        self.assertEqual(
            registration["record"]["recovery_mode"], "result_ledger_only"
        )
        self.assertEqual(payload["reason"], "trusted_recovery_probe_unavailable")
        self.assertEqual(payload["record"]["dependency_state"], "waiting_on_host")

    def test_unregistered_env_echo_probe_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "env-echo"
            registry = Path(directory) / "registry.json"
            self.write_probe(probe, {"succeeded": True, "evidence": "echo"})
            registry.write_text(json.dumps({"version": 1, "probes": {}}))
            with (
                mock.patch.object(guard, "RECOVERY_PROBE_REGISTRY", registry),
                self.assertRaisesRegex(ValueError, "not in the trusted registry"),
            ):
                guard.authorize_recovery_probe(
                    "env-echo",
                    [str(probe)],
                    "github-review-write-401",
                    guard.executable_hash(str(probe)),
                )

    def test_trusted_probe_registry_binds_digest_arguments_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe"
            registry = Path(directory) / "registry.json"
            self.write_probe(probe, {"succeeded": True, "evidence": "trusted"})
            digest = guard.executable_hash(str(probe))
            registry.write_text(json.dumps({
                "version": 1,
                "probes": {
                    "github-review-access": {
                        "executable_sha256": digest,
                        "arguments": ["--check"],
                        "failure_codes": ["github-review-write-401"],
                    }
                },
            }))
            with mock.patch.object(guard, "RECOVERY_PROBE_REGISTRY", registry):
                trust_hash = guard.authorize_recovery_probe(
                    "github-review-access",
                    [str(probe), "--check"],
                    "github-review-write-401",
                    digest,
                )
                with self.assertRaisesRegex(ValueError, "arguments do not match"):
                    guard.authorize_recovery_probe(
                        "github-review-access",
                        [str(probe), "--echo"],
                        "github-review-write-401",
                        digest,
                    )
                with self.assertRaisesRegex(ValueError, "failure code"):
                    guard.authorize_recovery_probe(
                        "github-review-access",
                        [str(probe), "--check"],
                        "local-review-daemon-stopped",
                        digest,
                    )
        self.assertRegex(trust_hash, r"^[0-9a-f]{64}$")

    def test_checked_in_trusted_probe_registry_is_valid(self):
        self.assertEqual(guard.load_recovery_probe_registry(), {})

    def test_malformed_trusted_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "registry.json"
            registry.write_text(json.dumps([{"version": 1}]))
            with (
                mock.patch.object(guard, "RECOVERY_PROBE_REGISTRY", registry),
                self.assertRaisesRegex(ValueError, "invalid envelope"),
            ):
                guard.load_recovery_probe_registry()

    def test_malformed_host_state_containers_fail_closed_at_cli_entry(self):
        failure_code = "github-review-write-401"
        request_key = guard.request_identity("example-reviewer", "owner/repo", "5553", HEAD)
        key = guard.escalation_identity(request_key, failure_code)
        payloads = (
            [],
            True,
            {"escalations": []},
            {"escalations": {key: []}},
            {"escalations": {key: None}},
        )
        actions = (
            ("show", ("--failure-code", failure_code)),
            (
                "set-state",
                ("--failure-code", failure_code, "--state", "failed"),
            ),
            ("register", self.host_registration_args()),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            for payload in payloads:
                for action, arguments in actions:
                    with self.subTest(payload=payload, action=action):
                        state.write_text(json.dumps(payload))
                        original = state.read_bytes()
                        code, result = self.run_guard(
                            state, action, *arguments
                        )
                        self.assertEqual(code, 2)
                        self.assertFalse(result["allowed"])
                        self.assertEqual(
                            result["reason"], "invalid_host_escalation_state"
                        )
                        self.assertEqual(state.read_bytes(), original)

    def test_malformed_recovery_probe_record_fails_closed(self):
        record = {
            "request_identity": "example-reviewer:owner/repo:1:" + HEAD,
            "failure_code": "github-review-write-401",
            "recovery_condition_hash": "expected-condition",
            "recovery_probe": [],
            "recovery_probe_id": "trusted-test-probe",
            "recovery_probe_trust_hash": "f" * 64,
            "recovery_probe_executable_hash": "e" * 64,
        }
        result = guard.run_recovery_probe(record)
        self.assertFalse(result["succeeded"])
        self.assertEqual(result["reason"], "recovery_probe argv is invalid")

    def test_secret_like_evidence_fails_closed_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            args = list(self.host_registration_args())
            evidence_index = args.index("--evidence") + 1
            args[evidence_index] = "Authorization: Bearer github_pat_abcdefghijklmnopqrstuvwxyz"
            code, payload = self.run_guard(state, "register", *args)
            persisted = json.loads(state.read_text()) if state.exists() else {}
        self.assertEqual(code, 2)
        self.assertIn("secret-like material", payload["error"])
        self.assertFalse(persisted.get("escalations"))

    def test_redaction_marker_with_trailing_material_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            args = list(self.host_registration_args())
            evidence_index = args.index("--evidence") + 1
            args[evidence_index] = "Authorization: redacted leaked-value"
            code, payload = self.run_guard(state, "register", *args)
        self.assertEqual(code, 2)
        self.assertIn("secret-like material", payload["error"])

    def test_recovered_state_resumes_dependency_without_completing_review(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.run_guard(state, "register", *self.host_registration_args())
            notified, _ = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "notified",
                "--result-url", "https://example.slack.com/archives/C000REVIEW/p1787708124013139",
                reconciliation_result=(
                    "https://example.slack.com/archives/"
                    "C000REVIEW/p1787708124013139"
                ),
            )
            recovered, payload = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "recovered",
                "--recovery-evidence", "GitHub review submission probe succeeded.",
            )
        self.assertEqual((notified, recovered), (0, 0))
        self.assertEqual(payload["record"]["dependency_state"], "ready_to_resume")
        self.assertTrue(payload["record"]["completion_contract_unchanged"])
        self.assertEqual(payload["record"]["expected_ledger"], "github_review")
        self.assertTrue(payload["record"]["recovery_attestation"]["succeeded"])
        self.assertEqual(
            len(payload["record"]["recovery_attestation"]["attestation_id"]), 32
        )

    def test_recovery_text_cannot_bypass_a_failed_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.run_guard(state, "register", *self.host_registration_args())
            self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "notified",
                "--result-url", "https://example.slack.com/archives/C000REVIEW/p1787708124013139",
                reconciliation_result=(
                    "https://example.slack.com/archives/"
                    "C000REVIEW/p1787708124013139"
                ),
            )
            code, payload = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "recovered",
                "--recovery-evidence", "I think the permission is fixed.",
                probe_result={"succeeded": False, "exit_code": 1, "output": "401"},
            )
        self.assertEqual(code, 7)
        self.assertEqual(payload["reason"], "recovery_probe_failed")
        self.assertEqual(payload["record"]["dependency_state"], "waiting_on_host")

    def test_register_requires_live_resolver_result(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            code, payload = self.run_guard(
                state,
                "register",
                *self.host_registration_args(),
                resolver_result=ValueError("live host verification failed"),
            )
        self.assertEqual(code, 2)
        self.assertIn("live host verification failed", payload["error"])

    def test_probe_output_must_attest_the_recorded_condition(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe"
            record = {
                "request_identity": "example-reviewer:owner/repo:1:" + HEAD,
                "failure_code": "github-review-write-401",
                "recovery_condition_hash": "expected-condition",
                "recovery_probe": [str(probe)],
                "recovery_probe_id": "trusted-test-probe",
                "recovery_probe_trust_hash": "f" * 64,
            }
            self.write_probe(probe, {
                "succeeded": True,
                "request_identity": record["request_identity"],
                "failure_code": record["failure_code"],
                "condition_hash": "different-condition",
                "evidence": "Probe succeeded.",
            })
            record["recovery_probe_executable_hash"] = guard.executable_hash(
                str(probe)
            )
            with mock.patch.object(
                guard, "authorize_recovery_probe", return_value="f" * 64
            ):
                result = guard.run_recovery_probe(record)
        self.assertFalse(result["succeeded"])
        self.assertEqual(result["reason"], "probe_attestation_mismatch")

    def test_probe_output_must_be_a_json_object(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, output in enumerate(([], True, "recovered")):
                with self.subTest(output=output):
                    probe = Path(directory) / f"probe-{index}"
                    self.write_probe(probe, output)
                    record = {
                        "request_identity": "example-reviewer:owner/repo:1:" + HEAD,
                        "failure_code": "github-review-write-401",
                        "recovery_condition_hash": "expected-condition",
                        "recovery_probe": [str(probe)],
                        "recovery_probe_id": "trusted-test-probe",
                        "recovery_probe_trust_hash": "f" * 64,
                        "recovery_probe_executable_hash": guard.executable_hash(
                            str(probe)
                        ),
                    }
                    with mock.patch.object(
                        guard, "authorize_recovery_probe", return_value="f" * 64
                    ):
                        result = guard.run_recovery_probe(record)
                    self.assertFalse(result["succeeded"])
                    self.assertEqual(result["reason"], "probe_output_is_not_object")

    def test_recovery_rejects_replaced_probe_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe"
            self.write_probe(probe, {"succeeded": False, "evidence": "original"})
            record = {
                "request_identity": "example-reviewer:owner/repo:1:" + HEAD,
                "failure_code": "github-review-write-401",
                "recovery_condition_hash": "expected-condition",
                "recovery_probe": [str(probe)],
                "recovery_probe_id": "trusted-test-probe",
                "recovery_probe_trust_hash": "f" * 64,
                "recovery_probe_executable_hash": guard.executable_hash(
                    str(probe)
                ),
            }
            self.write_probe(probe, {"succeeded": True, "evidence": "replacement"})
            with (
                mock.patch.object(
                    guard, "authorize_recovery_probe", return_value="f" * 64
                ),
                mock.patch.object(guard.subprocess, "run") as run,
            ):
                result = guard.run_recovery_probe(record)
        self.assertFalse(result["succeeded"])
        self.assertEqual(result["reason"], "probe_executable_changed")
        run.assert_not_called()

    def test_recovery_executes_the_verified_snapshot_if_source_changes_later(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe"
            request_identity = "example-reviewer:owner/repo:1:" + HEAD
            failure_code = "github-review-write-401"
            condition = "expected-condition"
            trusted = {
                "succeeded": True,
                "request_identity": request_identity,
                "failure_code": failure_code,
                "condition_hash": condition,
                "evidence": "verified snapshot executed",
            }
            replacement = {
                **trusted,
                "evidence": "replacement path executed",
            }
            self.write_probe(probe, trusted)
            record = {
                "request_identity": request_identity,
                "failure_code": failure_code,
                "recovery_condition_hash": condition,
                "recovery_probe": [str(probe)],
                "recovery_probe_id": "trusted-test-probe",
                "recovery_probe_trust_hash": "f" * 64,
                "recovery_probe_executable_hash": guard.executable_hash(
                    str(probe)
                ),
            }
            original_run = guard.subprocess.run

            def replace_source_then_run(*args, **kwargs):
                self.write_probe(probe, replacement)
                return original_run(*args, **kwargs)

            with (
                mock.patch.object(
                    guard, "authorize_recovery_probe", return_value="f" * 64
                ),
                mock.patch.object(
                    guard.subprocess, "run", side_effect=replace_source_then_run
                ),
            ):
                result = guard.run_recovery_probe(record)
        self.assertTrue(result["succeeded"])
        self.assertEqual(result["evidence"], "verified snapshot executed")

    def test_recovery_probe_receives_only_minimal_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe"
            request_identity = "example-reviewer:owner/repo:1:" + HEAD
            failure_code = "github-review-write-401"
            condition = "expected-condition"
            probe.write_text(
                "#!/bin/sh\n"
                "evidence='secret absent'\n"
                "if [ \"${SLACK_TOKEN+x}\" = x ] || "
                "[ \"${SLACK_MCP_XOXP_TOKEN+x}\" = x ] || "
                "[ \"${GH_TOKEN+x}\" = x ] || "
                "[ \"${UNRELATED_SECRET+x}\" = x ]; then "
                "evidence='secret present'; fi\n"
                "printf '{\"succeeded\":true,\"request_identity\":\"%s\","
                "\"failure_code\":\"%s\",\"condition_hash\":\"%s\","
                "\"evidence\":\"%s\"}\\n' "
                '"$SLACK_REVIEW_RECOVERY_REQUEST_IDENTITY" '
                '"$SLACK_REVIEW_RECOVERY_FAILURE_CODE" '
                '"$SLACK_REVIEW_RECOVERY_CONDITION_HASH" "$evidence"\n'
            )
            probe.chmod(0o755)
            record = {
                "request_identity": request_identity,
                "failure_code": failure_code,
                "recovery_condition_hash": condition,
                "recovery_probe": [str(probe)],
                "recovery_probe_id": "trusted-test-probe",
                "recovery_probe_trust_hash": "f" * 64,
                "recovery_probe_executable_hash": guard.executable_hash(
                    str(probe)
                ),
            }
            with (
                mock.patch.object(
                    guard, "authorize_recovery_probe", return_value="f" * 64
                ),
                mock.patch.dict(os.environ, {
                    "SLACK_TOKEN": "explicit-secret-slack-token",
                    "SLACK_MCP_XOXP_TOKEN": "secret-slack-token",
                    "GH_TOKEN": "secret-github-token",
                    "UNRELATED_SECRET": "secret-unrelated-value",
                }),
            ):
                result = guard.run_recovery_probe(record)
        self.assertTrue(result["succeeded"])
        self.assertEqual(result["evidence"], "secret absent")

    def test_recovery_fails_if_trusted_registry_entry_changes(self):
        record = {
            "request_identity": "example-reviewer:owner/repo:1:" + HEAD,
            "failure_code": "github-review-write-401",
            "recovery_condition_hash": "expected-condition",
            "recovery_probe": ["/usr/bin/true"],
            "recovery_probe_id": "trusted-test-probe",
            "recovery_probe_trust_hash": "f" * 64,
            "recovery_probe_executable_hash": "e" * 64,
        }
        with (
            mock.patch.object(
                guard, "authorize_recovery_probe", return_value="a" * 64
            ),
            mock.patch.object(guard.subprocess, "run") as run,
        ):
            result = guard.run_recovery_probe(record)
        self.assertFalse(result["succeeded"])
        self.assertEqual(result["reason"], "recovery_probe_trust_changed")
        run.assert_not_called()

    def test_untrusted_mentions_are_escaped_in_message(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            args = list(self.host_registration_args())
            evidence_index = args.index("--evidence") + 1
            args[evidence_index] = "Connector failed near <@U999999> and <!channel>."
            code, payload = self.run_guard(state, "register", *args)
        self.assertEqual(code, 0)
        self.assertNotIn("<@U999999>", payload["slack_message"])
        self.assertNotIn("<!channel>", payload["slack_message"])
        self.assertIn("&lt;@U999999&gt;", payload["slack_message"])

    def test_invalid_thread_target_fails_closed_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            args = list(self.host_registration_args())
            thread_index = args.index("--thread-ts") + 1
            args[thread_index] = "not-a-thread"
            code, payload = self.run_guard(state, "register", *args)
            persisted = json.loads(state.read_text()) if state.exists() else {}
        self.assertEqual(code, 2)
        self.assertIn("Slack message timestamp", payload["error"])
        self.assertFalse(persisted.get("escalations"))

    def test_notified_state_requires_https_permalink(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.run_guard(state, "register", *self.host_registration_args())
            code, payload = self.run_guard(
                state, "set-state",
                "--failure-code", "github-review-write-401",
                "--state", "notified",
                "--result-url", "not-a-url",
            )
        self.assertEqual(code, 2)
        self.assertIn("Slack permalink", payload["error"])


if __name__ == "__main__":
    unittest.main()
