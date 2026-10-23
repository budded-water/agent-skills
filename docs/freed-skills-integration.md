# Freed compatibility integration

This is a design prepared against the source revision in provenance.json, not
an applied upstream change. Revalidate these callers against upstream before
implementation. Current remote access was unavailable during preparation.
Do not use the PR head as an operational dependency before an authorized merge.

## Dependencies that must move together

| Upstream consumer | Current coupling | Required transition |
| --- | --- | --- |
| install.sh | Links top-level Freed skill directories into the Claude skill root | Preserve the existing entry through a thin compatibility wrapper; resolve the pinned generic implementation |
| scripts/install-codex-skills.js | Copies pr-review-preflight explicitly and discovers the Codex context skill under codex/skills | Install the verified external skills instead of copying duplicate implementations; retain unrelated skill selection and foreign-symlink refusal |
| requesting-slack-bot-review/preflight.sh | Calls the generic gate through a fixed Claude installation path | Resolve the verified generic gate for the active installation, including Codex-only installations |
| requesting-slack-bot-review/check_installation.py | Refreshes the Freed source and reinstalls both review skills | Validate the Freed revision and external package pin separately; refresh neither implicitly from an invocation |
| docs/ai-agent-sync.md and README.md | Describe Freed as source for the currently shipped generic skills | Describe separate canonical owners, explicit update procedure and recovery |

## Proposed dependency contract

Keep one machine-readable dependency manifest in freed_skills containing the
agent-skills repository identity, a reviewed merged full commit, and the two
selected skill names. Installer and refresh code consume that manifest; docs
refer to it rather than repeating the commit. A missing or mismatched pin must
fail before installation changes. Updating the dependency is an explicit PR.

Keep a clean pinned checkout separately from user skill discovery roots. Stage
installation into an isolated directory using scripts/install.py and verify the
package marker, commit and every installed content hash. Do not place a mutable
development checkout directly on an executable skill symlink. A compatibility
wrapper verifies the expected installed package and delegates to its local
preflight.sh with unchanged arguments and exit status. It must fail with a clear
installation instruction when missing or mismatched, without downloading code
or changing global GitHub authentication during a review request.

Do not automatically overwrite the existing same-named Freed installations:
agent-skills intentionally refuses foreign or different-version targets. The
upstream migration needs a separately reviewed adoption procedure: identify old
ownership, preserve local edits and the old working targets, stage the new
version, then explicitly switch the relevant entry points. A failed adoption
must retain or restore the previous working installation. Never replace all
skills or rerun hook/MCP setup as a side effect of adopting these two skills.

## Validation required in the upstream PR

- Existing generic preflight and Freed reviewer-specific wrapper regressions.
- Claude links and Codex copies, including an installation without Claude skills.
- Missing pinned checkout, wrong package identity, commit mismatch and content
  modification, with no network execution during wrapper invocation.
- Existing copied installations, broken/foreign symlinks, local edits and
  interrupted adoption; unrelated skills, hooks, MCP and auth remain intact.
- Refresh respects both source owners, checks drift before replacing anything,
  and does not silently revive the removed duplicate implementations.

Implementation belongs in an upstream OpenSpec proposal and topic-branch PR
after a reviewed merged version is available. That PR owns changes to both
installers, the reviewer wrapper, refresh tooling, tests and upstream docs.
This plan does not assert those paths have already been changed or accepted.
