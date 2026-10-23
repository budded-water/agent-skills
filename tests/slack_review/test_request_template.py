from .helpers import SKILL


def test_complete_requests_keep_native_approval_authority_explicit():
    text = (SKILL / "SKILL.md").read_text()
    approval = text.split("For an approval-authorized request:", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]
    no_approval = text.split("For a request that does not authorize approval", 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]
    for request in (approval, no_approval):
        assert "<full-pr-url>" in request and "<full-sha>" in request
        assert "RESULT LEDGER:" in request and "IF FINDINGS:" in request
    assert "please approve this PR" in approval
    assert "does\nnot authorize native approval" in no_approval
