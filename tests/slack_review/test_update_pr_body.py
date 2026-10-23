import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "update_pr_body.py"
SPEC = importlib.util.spec_from_file_location("update_pr_body", MODULE_PATH)
update_pr_body = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = update_pr_body
SPEC.loader.exec_module(update_pr_body)


class UpdatePrBodyTest(unittest.TestCase):
    def test_markdown_is_json_stdin_and_never_shell_source(self):
        head = "a" * 40
        body = "Use `STATUS_FILE`, $(do-not-run), 'quotes', and 中文."
        responses = [mock.Mock(stdout=head + "\n"), mock.Mock(stdout='{"html_url":"url"}')]
        with mock.patch.object(update_pr_body.subprocess, "run", side_effect=responses) as run:
            output = update_pr_body.update("owner/repo", 7, head, body)

        self.assertIn("html_url", output)
        patch = run.call_args_list[1]
        self.assertEqual(patch.args[0][-2:], ["--input", "-"])
        self.assertNotIn(body, patch.args[0])
        self.assertEqual(json.loads(patch.kwargs["input"]), {"body": body})

    def test_head_mismatch_fails_before_patch(self):
        with mock.patch.object(
            update_pr_body.subprocess, "run", return_value=mock.Mock(stdout="b" * 40)
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "remote head mismatch"):
                update_pr_body.update("owner/repo", 7, "a" * 40, "body")
        self.assertEqual(run.call_count, 1)

    def test_success_output_is_reduced_to_the_pr_url(self):
        output = json.dumps({
            "url": "https://api.github.test/pulls/7",
            "html_url": "https://github.test/owner/repo/pull/7",
            "body": "large body",
        })
        self.assertEqual(
            update_pr_body.display_output(output),
            "https://github.test/owner/repo/pull/7",
        )


if __name__ == "__main__":
    unittest.main()
