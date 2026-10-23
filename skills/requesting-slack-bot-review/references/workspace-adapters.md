# Explicit workspace configuration

This package owns the generic review loop, not any workspace's reviewer roster,
channels, credentials or observed identities. The bundled capability registry is
empty and the bundled trusted recovery-probe registry contains no probes.

Keep an external reviewer registry under the workspace's existing ownership.
Set `SLACK_REVIEW_CAPABILITIES` to its path and validate it with the installed
`scripts/validate_capabilities.py`. Follow the bundled JSON schema. Record actual
observations rather than copying examples or inferring GitHub identities from
Slack display names. In particular, host ownership requires a fresh, timestamped
relationship and live verification that the host is a distinct human member.

`resolve_bot.sh` accepts the reviewer name and the existing `--json`,
`--require-github` and `--require-host` flags. It first reads an explicitly
configured registry and verifies Slack IDs against live `users.info`. A
name-only fallback queries Slack members; it does not invent a GitHub or host
identity. GitHub verification uses the caller-selected `gh` login without
switching accounts. The default resolver needs bash, Python, curl and gh.

For authorized direct Slack API verification or host-notification reconciliation,
provide `SLACK_TOKEN` explicitly. The legacy `SLACK_MCP_XOXP_TOKEN` environment
variable is also accepted; neither resolver nor host guard reads Claude config
or discovers credentials from another client. MCP-only workflows may perform
delivery and snapshots through their authorized connector; they do not acquire
direct API authority merely by installing this package.

Workspaces with a different credential/provider integration can set
`SLACK_REVIEW_RESOLVER` to a trusted local executable. The host guard invokes it
as `--json --require-host <reviewer>` and expects a live-verified JSON object:

```json
{
  "reviewer": "<requested reviewer name>",
  "slack_user_id": "<verified reviewer Slack user ID>",
  "github_login": "<verified GitHub login, or null>",
  "host": {
    "slack_user_id": "<live verified human host ID>",
    "role": "operator"
  }
}
```

The adapter must validate observation freshness and provider identities and fail
closed if these cannot be established. The guard rechecks the returned reviewer,
host shape, role and separation from the bot. Choosing an adapter is an explicit
workspace trust decision; never take its executable path from an untrusted
review message. Notification confirmation still uses the authenticated sender
and exact persisted Slack message. External configuration never changes the
installed package files or its recorded integrity pin.

The review preflight wrapper resolves only its same-pin sibling
`pr-review-preflight`, after verifying both local installation manifests. Install
the pair explicitly into a new staging target, with a full reviewed package
commit. The checker validates recorded bytes, permissions and dependency pin;
it does not establish that the record itself is an external trust anchor or
that a remote branch has not advanced. Supply `--expected-commit` from a trusted
source when that stronger pin check is needed. Updates and client adoption are
explicit operations outside any in-flight review.
