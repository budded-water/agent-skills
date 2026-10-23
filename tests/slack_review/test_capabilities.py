import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "validate_capabilities.py"
SPEC = importlib.util.spec_from_file_location("validate_capabilities", MODULE_PATH)
validate_capabilities = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = validate_capabilities
SPEC.loader.exec_module(validate_capabilities)


class CapabilityValidationTest(unittest.TestCase):
    def test_valid_capability_entry(self):
        errors = validate_capabilities.validate({
            "reviewers": {
                "example-reviewer": {
                    "observed_at": "2026-08-21T10:00:00Z",
                    "slack_user_id": "U012ABC",
                    "github_login": "review-bot",
                    "accepted_senders": ["slack_mcp"],
                    "result_ledgers": ["github_review", "slack"],
                    "verdicts": ["approved", "with_findings"],
                    "watches_new_pushes": False,
                    "structured_output": "not_observed",
                    "host": {
                        "observed_at": "2026-08-26T08:18:44Z",
                        "slack_user_id": "U000HOST",
                        "role": "operator",
                        "evidence_url": "https://example.test/host-evidence",
                    },
                }
            }
        })
        self.assertEqual(errors, [])

    def test_missing_observation_is_rejected(self):
        errors = validate_capabilities.validate({
            "reviewers": {
                "bot": {
                    "slack_user_id": "U012ABC",
                    "accepted_senders": ["slack_mcp"],
                    "result_ledgers": ["slack"],
                    "verdicts": ["approved"],
                    "watches_new_pushes": False,
                    "structured_output": "unknown",
                }
            }
        })
        self.assertIn("bot: missing observed_at", errors)

    def test_empty_ledgers_and_unknown_fields_are_rejected(self):
        errors = validate_capabilities.validate({
            "reviewers": {
                "bot": {
                    "observed_at": "2026-08-21T10:00:00Z",
                    "slack_user_id": "U012ABC",
                    "accepted_senders": ["slack_mcp"],
                    "result_ledgers": [],
                    "verdicts": [],
                    "watches_new_pushes": False,
                    "structured_output": "unknown",
                    "unexpected": True,
                }
            }
        })
        self.assertIn("bot: invalid result ledger", errors)
        self.assertIn("bot: invalid verdict", errors)
        self.assertIn("bot: unknown fields: unexpected", errors)

    def test_observed_at_requires_time_and_explicit_offset(self):
        for observed_at in ("2026-08-21", "2026-08-21T10:00:00"):
            errors = validate_capabilities.validate({
                "reviewers": {"bot": {
                    "observed_at": observed_at,
                    "slack_user_id": "U012ABC",
                    "accepted_senders": ["slack_mcp"],
                    "result_ledgers": ["slack"],
                    "verdicts": ["approved"],
                    "watches_new_pushes": False,
                    "structured_output": "unknown",
                }}
            })
            self.assertTrue(any("RFC 3339" in error for error in errors))

    def test_github_ledger_requires_login(self):
        errors = validate_capabilities.validate({
            "reviewers": {"bot": {
                "observed_at": "2026-08-21T10:00:00Z",
                "slack_user_id": "U012ABC",
                "accepted_senders": ["slack_mcp"],
                "result_ledgers": ["github_review"],
                "verdicts": ["approved"],
                "watches_new_pushes": False,
                "structured_output": "unknown",
            }}
        })
        self.assertIn("bot: GitHub ledger requires a valid github_login", errors)

    def test_unknown_structured_output_value_is_rejected(self):
        errors = validate_capabilities.validate({
            "reviewers": {"bot": {
                "observed_at": "2026-08-21T10:00:00Z",
                "slack_user_id": "U012ABC",
                "accepted_senders": ["slack_mcp"],
                "result_ledgers": ["slack"],
                "verdicts": ["approved"],
                "watches_new_pushes": False,
                "structured_output": "sometimes",
            }}
        })
        self.assertIn("bot: invalid structured_output", errors)

    def test_legacy_entry_without_structured_output_is_treated_as_unknown(self):
        errors = validate_capabilities.validate({
            "reviewers": {"bot": {
                "observed_at": "2026-08-21T10:00:00Z",
                "slack_user_id": "U012ABC",
                "accepted_senders": ["slack_mcp"],
                "result_ledgers": ["slack"],
                "verdicts": ["approved"],
                "watches_new_pushes": False,
            }}
        })
        self.assertEqual(errors, [])

    def test_optional_host_requires_verified_observation_shape(self):
        base = {
            "observed_at": "2026-08-21T10:00:00Z",
            "slack_user_id": "U012ABC",
            "accepted_senders": ["slack_mcp"],
            "result_ledgers": ["slack"],
            "verdicts": ["approved"],
            "watches_new_pushes": False,
            "host": {
                "observed_at": "2026-08-26",
                "slack_user_id": "W08K7QGQ0BC",
                "role": "friend",
                "evidence_url": "http://example.test/untrusted",
                "unexpected": True,
            },
        }
        errors = validate_capabilities.validate({"reviewers": {"bot": base}})
        self.assertIn("bot: unknown host fields: unexpected", errors)
        self.assertTrue(any("host observed_at" in error for error in errors))
        self.assertIn("bot: host slack_user_id must be a Slack user ID", errors)
        self.assertIn("bot: invalid host role", errors)
        self.assertIn("bot: host evidence_url must be an HTTPS URL", errors)

    def test_host_is_optional_for_legacy_reviewer(self):
        errors = validate_capabilities.validate({
            "reviewers": {"bot": {
                "observed_at": "2026-08-21T10:00:00Z",
                "slack_user_id": "U012ABC",
                "accepted_senders": ["slack_mcp"],
                "result_ledgers": ["slack"],
                "verdicts": ["approved"],
                "watches_new_pushes": False,
            }}
        })
        self.assertEqual(errors, [])

    def test_host_evidence_url_requires_an_https_origin(self):
        errors = validate_capabilities.validate({
            "reviewers": {"bot": {
                "observed_at": "2026-08-21T10:00:00Z",
                "slack_user_id": "U012ABC",
                "accepted_senders": ["slack_mcp"],
                "result_ledgers": ["slack"],
                "verdicts": ["approved"],
                "watches_new_pushes": False,
                "host": {
                    "observed_at": "2026-08-26T08:18:44Z",
                    "slack_user_id": "U000HOST",
                    "role": "operator",
                    "evidence_url": "https://",
                },
            }}
        })
        self.assertIn("bot: host evidence_url must be an HTTPS URL", errors)

    def test_bundled_capability_observation_is_valid(self):
        payload = __import__("json").loads(
            ((Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "references" / "reviewer-capabilities.json").read_text()
        )
        self.assertEqual(validate_capabilities.validate(payload), [])


if __name__ == "__main__":
    unittest.main()
