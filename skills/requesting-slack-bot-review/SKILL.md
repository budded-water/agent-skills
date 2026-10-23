---
name: requesting-slack-bot-review
description: Request and close the loop on an asynchronous PR or diff review from a Slack bot or human reviewer, including readiness checks, delivery verification, finding triage, fixes, re-review, and post-review workflow improvement.
---

# Requesting a Slack Review

A review request is complete only when a result for the intended revision has
arrived and its findings have been dispositioned. Posting a message or receiving
an acknowledgement is not completion.

## Review loop

Before starting a new review, run
`python3 <installed-skill-path>/check_installation.py`.
Install this skill with its sibling `pr-review-preflight` from the same reviewed
agent-skills commit. The checker verifies both copies against their recorded
installation manifests; it does not claim that a moving upstream branch is
current. Use `--expected-commit <full-reviewed-sha>` when the caller has a trusted
package pin. Updates are explicit: stage both skills from a clean pinned checkout
into a new target with `scripts/install.py`, verify them, and switch the client
entry only when authorized and no review is in flight. Never fetch code, refresh
another source checkout, or change GitHub authentication during invocation.

This skill documents a review workflow. Migrating, installing or inspecting it
does not authorize Slack messages, GitHub review submission, merging or deployment.
Use external actions only within the user's authorized review task.

1. Identify the exact artifact and revision to review. For a PR, capture its
   current head SHA and define the completion contract: result ledger and
   acceptable verdicts.
2. For a PR, run this skill's `preflight.sh <pr-number> [owner/repo]`, which
   verifies the same-pin installation pair before delegating to the sibling
   generic gate and checking repeat-review coverage. Require the pushed head,
   applicable base checks, green required CI, a non-draft PR and dispositioned
   existing actionable review threads.
3. Immediately before sending, re-read the remote head and require it to equal
   the preflighted head. Choose a delivery method that the reviewer can accept,
   post in the existing PR conversation when one exists, and state the
   completion contract explicitly.
4. Start monitoring Slack and the PR immediately. For a bot reviewer, require
   an acknowledgement within 60 seconds; if none arrives, reconcile Slack and
   the result ledger, then fail over to the next verified reviewer instead of
   retrying the same silent bot first. Do not stop at “sent.”
5. Bind any delivered result to the requested revision. Triage every finding as
   `confirmed`, `needs verification`, or `rejected` using the cited code.
6. Implement authorized fixes, verify them, push the new revision when
   authorized, synchronize the disposition into the same conversation, rerun
   readiness checks, and explicitly request re-review.
7. Before the final review request, freeze the candidate: update every
   PR-resident artifact that is already knowable, including specs, task
   checkboxes, generated files, version metadata, and validation summaries.
8. Finish only when the current revision has a delivered result with nothing
   actionable left, or report the precise blocker.
9. After the loop closes, run a short evidence-based process retrospective.
   Recommend whether the review skill should stay unchanged, be updated, or
   create a separate follow-up. After an accepted exact-head result, put
   authorized process improvements in a separate follow-up by default. Reopen
   the approved candidate only when the user explicitly asks; genuine findings
   delivered before acceptance remain fixes on the current PR.

Stay in one Slack thread per PR so the request, progress, disposition, fixes,
and re-review remain together. The thread is the conversation boundary, not a
substitute for an authorization boundary: when a reviewer accepts mutations
only from the final authenticated message, make that message self-contained.

Before every mutation of a candidate whose review loop is still open, classify
why the revision is being requested as `finding_driven` or
`infrastructure_only`. `finding_driven` has precedence whenever the revision
addresses an actionable finding on the current candidate, even when the changed
files implement review infrastructure; reset the consecutive infrastructure
counter to zero and keep that fix on the current PR. Otherwise increment the
counter before mutation. The second consecutive `infrastructure_only` round is
routed to `separate_follow_up` instead of changing the current candidate.
Use `scripts/review_round_budget.py` to make this decision auditable; classify
from the revision's cause and evidence, never from file paths.

## Freeze the final candidate

Do not make the final approval a prerequisite for a change to the revision that
must receive that approval. This creates a self-referential loop: approval is
received, the branch is edited to record it, and the approval immediately
becomes stale.

Before requesting the intended final review:

- finish all known code, documentation, spec, checklist, generated-file, and
  version updates that belong on the PR branch;
- run validation and record pre-review facts that are already true;
- make the worktree clean and push the complete candidate;
- treat that head as frozen while its review is in flight.

When synchronizing a PR description, pass Markdown as data rather than shell
source. Prefer the expected-head-aware helper:

```bash
scripts/update_pr_body.py --repo <owner/repo> --pr <number> \
  --expected-head <full-sha> --body-file <markdown-file-or->
```

Do not interpolate PR Markdown into a shell command. Backticks, substitutions,
and quotes in ordinary documentation must never execute locally.

Treat tracked or staged dirt as a hard blocker because it is invisible to the
remote reviewer. Untracked files may be unrelated; surface and disposition them
explicitly rather than silently treating them as candidate content. Before
running validation in a sibling checkout or restricted sandbox, run
`scripts/workspace_probe.py --path <repo>` so an environment write denial is not
misreported as a test failure.

After a final verdict, record the runtime outcome in surfaces that do not alter
the reviewed revision: the GitHub review ledger, the existing Slack thread, and
the request-state ledger. Do not push a commit whose only purpose is to say that
the preceding commit was approved.

Design PR checklists around author-controlled readiness, not a future reviewer
outcome. For example, “E2E request and re-review path exercised” can be checked
before the final request; “final HEAD approved” belongs in the external review
ledger. If a post-verdict branch edit is genuinely required, it creates a new
candidate and must be reviewed again; make that an explicit exception, not the
default completion step.

## Define the completion contract

Before sending, record:

```yaml
request_identity: <reviewer>:<repo>:<pr-or-diff>:<head-sha>
expected_revision: <full-head-sha>
result_ledger: github_review | slack | either
acceptable_verdicts: [approved, with_findings]
required_github_review_state: APPROVED  # optional; explicit native approval or verified gate
```

For a GitHub pull request, use `github_review` by default whenever the reviewer
has a verified GitHub identity and can create review objects. GitHub is then the
canonical review ledger; Slack is the control and notification surface. Use
`slack` or `either` only as an explicit capability fallback, never because it is
easier to observe a Slack reply.

Map the result to one GitHub review object:

- actionable findings: submit `COMMENTED` with the complete findings;
- clean and the authenticated request explicitly authorizes native approval:
  submit `APPROVED`, with the review summary in its body;
- clean without native-approval authorization: submit `COMMENTED` with the
  zero-finding conclusion.

Do not create both a separate COMMENTED review and an APPROVED review for the
same clean result. Notify the Slack thread with only the verdict, exact head,
and GitHub review URL; do not maintain a second detailed findings ledger there.
If the expected GitHub review object or URL is absent, the result is not
delivered. Say that the PR write failed or is missing instead of posting a
success-looking Slack-only recap.

When `github_review` can satisfy the contract, also resolve and record the
reviewer's GitHub login from a verified reviewer capability record. Never infer
it from the Slack display name, bot label, or a similarly named public GitHub
account. Missing identity must reject GitHub evidence rather than treating any
collaborator's review as the requested result. GitHub logins compare
case-insensitively, while the capability record preserves the provider spelling.

Keep the semantic verdict separate from a requested or required provider
action. Normally omit `required_github_review_state`: an
unambiguous zero-finding `COMMENTED` review can still be a delivered semantic
approval. Set it to `APPROVED` when the authenticated request explicitly asks
for native approval if clean, or after a live ruleset/readiness check proves
that GitHub requires an approving review action. Then register and monitor
`required_github_review_state: APPROVED`. In that mode a zero-finding
`COMMENTED` review remains non-terminal until the expected reviewer submits
`APPROVED`, while any actionable finding still terminates the watcher wait for
triage. Never infer approval authorization from thread history or from
`reviewDecision` alone.

The request identity is the idempotency key. While that identity is
acknowledged or in progress, do not send it again. Retry only after a defined
silence timeout, an explicit rejection/failure, proof that no job is active, or
an explicit user override. A new pushed head creates a new identity.

Bot delivery and review completion use separate clocks. The default
acknowledgement SLA is 60 seconds while the request is still `requested`. An
eyes reaction or a working/progress message moves it to `acknowledged` or
`in_progress` and starts a separate result SLA: normally 5–10 minutes for a
small documentation PR and 10–15 minutes for complex code. Human reviewers are
not subject to the one-minute bot acknowledgement default. Keep only one active
reviewer at a time.

If the bot acknowledgement SLA expires, re-read the Slack thread and provider
ledger. When both remain empty and the head is unchanged, mark that reviewer
expired for `acknowledgement_timeout` and register the next live-verified
reviewer as a new request identity in the same PR thread. Do not spend a second
full window retrying the same silent reviewer before eligible alternatives.
Stop after the verified
reviewer pool is exhausted and report the dependency; never round-robin
indefinitely.

A locally elapsed timeout is not proof of Slack silence. When the guard reports
`expired_requires_reconciliation`, re-read the thread and provider ledger before
using an explicit override. A delivered identity remains closed; do not
re-register the same reviewer/repo/artifact/head merely because time passed.

Finish composing the self-contained Slack message before registering the
request. Use `scripts/request_guard.py register` immediately before posting;
it compares the remote PR head with the expected head and atomically rejects
an active duplicate. After Slack confirms the post and its sender, call
`request_guard.py set-state --state requested --request-url <permalink>` to
start the 60-second acknowledgement clock from verified delivery rather than
from message preparation. This transition is one-shot and idempotent for the
same permalink; it cannot reset an already-started clock or revive an expired
request. Before delivery confirmation, the new identity has a separate
five-minute posting lease, not a bot acknowledgement deadline. If that lease
expires, reconcile the thread and provider ledger before attempting any retry;
do not call the bot silent. If posting fails, do not record delivery; reconcile
the thread before retrying. Move the identity through `acknowledged`,
`in_progress`, `delivered`, `failed`, `superseded`, or `artifact_closed` with
`set-state`; do not maintain this state only in prose.
Registration and active-state transitions reject a second active reviewer for
the same repository/artifact/head; complete the reconciliation and move the
first identity out of an active state before failover. A late acknowledgement
from that first reviewer cannot reactivate it after the successor is active.
The guard defaults to `--ack-timeout 60` for `requested` and
`--result-timeout 600` for `acknowledged`/`in_progress`; use a larger explicit
result timeout for complex code or humans. The deprecated `--silence-timeout`
remains only as a compatibility override for both clocks.
Persist the chosen GitHub identity, ledger, optional required GitHub review
state, Slack channel/thread, terminal reason, and result URL in the guard as
they become known. This gives later runs a durable reconciliation trail instead
of forcing them to reconstruct the request from process output.

`with_findings` is a delivered review, not a closed loop. It terminates the
current watcher wait so triage can begin, but only an accepted verdict with no
actionable finding completes the overall workflow. If the contract requires a
GitHub review, a Slack recap remains non-terminal even when it names the current
head.

## Choose the Slack delivery method

Slack MCP and direct Slack API access are both valid. Choose based on the
reviewer's actual identity and permission contract; do not impose a universal
sender requirement.

Use Slack MCP when:

- the reviewer accepts messages from the connected MCP identity;
- the integration provides the needed thread, mention, and read operations;
- no human attribution requirement has been stated.

Use a human/user token or another authenticated sender when:

- the reviewer allowlists human user IDs or rejects app/bot identities;
- the request must visibly come from a particular person;
- the MCP connection cannot expose or verify the sender identity required by
  the workflow.

After posting, verify observable delivery: inspect the returned sender/message
when possible and check for rejection reactions or error replies. A successful
API response proves that Slack stored a message, not that the reviewer accepted
the job.

Do not switch delivery methods merely because one mode is locally preferred.
Use evidence from the reviewer, workspace policy, or a failed attempt.

## Write an actionable request

Include:

- the PR or diff URL;
- the revision SHA when revision identity matters;
- an explicit `review` or `re-review` request;
- two or three high-risk areas worth examining;
- where the result should be recorded;
- material limitations the reviewer cannot verify, such as device-only UI or
  production behavior.

For reviewers that authorize GitHub writes from the final Slack event, put the
full PR URL, full head, `review` or `re-review` intent, result ledger, and any
native-approval request in the same final mention. Earlier thread messages may
provide context, but do not assume the reviewer will combine them into write
authority. If the reviewer asks for a missing field, resend the complete
canonical request; do not reply with only the URL, SHA, or the word `approve`.

For an approval-authorized request:

```text
<reviewer> re-review <full-pr-url> at exact HEAD <full-sha>.
RESULT LEDGER: GitHub PR review object.
IF FINDINGS: submit COMMENTED with the complete findings.
IF CLEAN: please approve this PR on GitHub with a substantive summary in the
review body. This authenticated final request authorizes native approval only
if the exact HEAD is clean.
Reply in this Slack thread only with verdict, HEAD, and the GitHub review URL.
```

Use the explicit `approve` verb in the authenticated final request when native
approval is authorized. Some reviewer bridges distinguish it from a provider
state label such as `APPROVED`; verify the workspace adapter's contract rather
than broadening authorization from quoted examples or old thread history.

For a request that does not authorize approval, resend the same complete target
and contract with an explicit non-authorization clause:

```text
<reviewer> re-review <full-pr-url> at exact HEAD <full-sha>.
RESULT LEDGER: GitHub PR review object.
IF FINDINGS: submit COMMENTED with the complete findings.
IF CLEAN: submit COMMENTED with the zero-finding conclusion. This request does
not authorize native approval.
Reply in this Slack thread only with verdict, HEAD, and the GitHub review URL.
```

Ask reviewers whose capability record says `structured_output: supported` to
end with:

```text
VERDICT: APPROVED
ACTIONABLE_FINDINGS: 0
HEAD: <full-sha>
```

Use `WITH_FINDINGS` and a positive count when findings exist. The structured
fields are authoritative. For `unknown`, one request may explicitly label the
template as a capability probe; until the reviewer actually returns those
fields, accept and classify its normal revision-bound provider verdict instead.
For `not_observed`, request a normal verdict unless fresh evidence justifies a
new probe. After a live result, update the observation from delivered evidence
rather than from what the request asked the reviewer to emit.

Prefer a compact, labeled request containing `PR`, `HEAD`, `MODE`, `RESULT
LEDGER`, `IF FINDINGS`, `IF CLEAN`, `SLACK NOTIFICATION`, `FOCUS`, and `OUT OF
SCOPE`. Keep historical diagnosis under `CONTEXT` so it cannot be confused with
the requested result.

For re-review, summarize each prior finding and its disposition. State what was
not changed and why. Never imply that local-only changes are visible to a remote
reviewer; request final diff review only after the reviewed revision is pushed.

## Monitor for a real result

Track transport and verdict separately.

Transport states:

- `acknowledged`: reaction or progress message;
- `delivered`: substantive result bound to the requested revision;
- `stale`: substantive-looking result bound to an older revision;
- `silent`: no qualifying response within the expected window.
- `watcher_error`: monitoring could not establish state.
- `superseded`: the remote head moved beyond the requested revision.
- `artifact_closed`: the PR merged or closed before an acceptable exact-head
  result could be established. This stops the monitor and requires
  reconciliation; it is not an approval.

Review verdicts:

- `approved`: no actionable code finding;
- `with_findings`: one or more actionable findings;
- `no_verdict`: substantive response without a usable outcome;
- `none`: no current-revision result.

GitHub `APPROVED` is a review action, not proof that its body contains no
findings. Parse and disposition review-body notes and current-head threads even
when the review state is approved. An approval containing actionable or
explicitly labeled findings is `with_findings` for this workflow.

After a PR merges or closes, recover an exact-head result only when provider
timestamps prove that the result existed before closure. Missing timestamps
or malformed present timestamp fields fail closed; a post-closure response must
not retroactively satisfy the request.
When an evidence record exposes multiple timestamps, use the latest meaningful
creation, submission, update, or edit time so an older field cannot hide a
post-closure mutation.
For Slack evidence, absence of `edited` means unedited; if `edited` is present,
it must be an object with a valid `ts`. Never treat a null or empty edit marker
as permission to fall back to the original message timestamp.

Poll both the Slack thread and PR comments/reviews when both can contain the
result. Exclude your own messages and progress updates. A prose recap of an old
review is stale, not a new review. Prefer a PR review/comment whose commit ID is
the requested head; for Slack-only results, require the response to identify
the revision or otherwise prove which diff it reviewed.

Monitoring must tolerate transient API failures and re-derive final state after
a watcher error. If the user asked to wait for the review, keep monitoring until
the watcher has an acceptable delivered verdict (including `with_findings`)
rather than reporting only that the request was posted. Findings then enter
triage; they do not complete the overall workflow.

Start exactly one persistent monitor immediately after sending. Do not manually
alternate sleeps, Slack reads, and GitHub reads. The monitor owns polling,
classification, supersession detection, provider-error handling, and terminal
exit for that request identity.

When the monitor returns a terminal GitHub result, persist `delivered` with its
review URL before calling `request_guard.py show` or `check`. Those read actions
can provisionally expire an unacknowledged request; an already-delivered
provider result must be reconciled first. A Slack-only recap is not a terminal
GitHub result.

If a rejected finding or other no-code disposition requires a fresh review on
the unchanged head, capture the current maximum GitHub review ID before sending
the correction request and pass it as `--after-review-id`. This provider
watermark prevents the persistent watcher from immediately re-consuming the
old COMMENTED review. Do not invent a new commit merely to make old evidence
stale, and do not use a watermark captured after the request was sent.

Use `scripts/watch_review.py` in normal live mode when direct Slack API access
is available. When the completion contract is `github_review` and Slack is
accessed only through MCP, use `--github-only`: Slack MCP handles delivery and
an optional one-time acknowledgement check, while the persistent watcher reads
the authoritative GitHub ledger without requiring a Slack token. With a
Slack-only or `either` contract over MCP, the orchestration layer must run one
recurring monitor that normalizes both providers with
`scripts/build_review_snapshot.py`; do not degrade to caller-driven manual
polling or invent a second set of delivery rules. Read `--help` for both
scripts. The watcher returns
non-zero after timeout or repeated provider errors, so watcher death cannot look
like continued monitoring.

For a bot, pass a watcher timeout that covers the acknowledgement clock plus the
selected result clock: `--timeout 660` for the default 60 + 600 second contract,
or `--timeout 960` for a 15-minute complex review result SLA. The watcher's
longer built-in default remains only a compatibility/safety ceiling; it does not
replace the request guard's state-specific deadlines. Human-review workflows
must set their explicitly chosen longer timeout.

When the completion contract includes
`required_github_review_state: APPROVED`, pass
`--required-github-review-state APPROVED` to the watcher and the same option to
`request_guard.py register`. This option is valid only with `github_review` or
`either`; it is not a substitute for checking the review body and unresolved
threads.

Watcher JSON also includes `verdict_evidence` — the phrase a prose-derived
`with_findings` hinged on. Every keyword-derived `with_findings` carries one, so
a null on that verdict means it came from structured fields or provider state;
a null on `approved` says nothing either way. See
[references/review-state-contract.md](references/review-state-contract.md).

Watcher JSON includes `monitoring_scope`. In `github_only` scope, `silent`
means only that no qualifying GitHub result is present; it is not evidence that
Slack ignored the request. Perform the allowed one-time Slack MCP acknowledgement
check at the 60-second bot acknowledgement boundary and immediately persist an
observed reaction/progress message as
`acknowledged` or `in_progress` with `request_guard.py set-state`. The GitHub-only
watcher remains the single persistent result monitor. If Slack and GitHub are
still empty at that boundary, stop the current watcher before starting the next
reviewer's watcher; never leave two reviewer monitors active for one PR.

One-shot and snapshot classification use exit 0 only for a terminal acceptable
verdict, exit 4 for supersession, exit 5 for a merged/closed artifact, and exit
1 for other non-terminal states. Read the emitted JSON for the precise
transport state.

Read [references/review-state-contract.md](references/review-state-contract.md)
when constructing an MCP snapshot or integrating watcher output.

Keep CI and preview evidence separate. A successful Vercel status whose build
was skipped or ignored does not prove a deploy exists; state preview validation
as unavailable and put browser or visual checks under `OUT OF SCOPE` unless a
live URL was actually opened.

## Recover host-owned bot failures

Classify who owns recovery before escalating a bot error:

- `agent_recoverable`: recover directly within existing scope and authority;
- `transient_retryable`: use bounded retry/backoff;
- `host_action_required`: notify the verified bot host;
- `user_decision_required`: ask the user for the decision or new authority;
- `external_provider_outage`: monitor or report the provider state.

Use `host_action_required` only when all four facts are established: evidence
locates the fault in the bot's host environment, the configured host owns a
concrete recovery action, neither the bot nor current agent can safely recover
it, and the guard's resolver verifies a fresh host relationship and live Slack
profile. Ordinary silence, an undiagnosed
watcher error, requestor-side tool approval, code/CI/base problems, or a global
provider outage do not establish host ownership.

Before sending, use `scripts/host_escalation_guard.py assess` and `register`.
Key the record by the existing request identity plus a stable failure code;
never derive the code from variable raw error prose. The guard invokes the
workspace resolver itself and returns a sanitized message plus a bounded send
lease. Post it once in the existing review thread, then persist the Slack
permalink with `set-state --state notified`. The caller-supplied URL is only a
message locator: the guard must re-read that exact message through the
authenticated provider and require the complete canonical message persisted at
registration, including its fingerprint, verified host mention, artifact,
exact head, evidence, host action, and recovery condition, plus the sender
captured by Slack `auth.test`, before confirming it. Retries reuse that persisted
message rather than rebuilding a potentially different body. If a
process fails before that confirmation, reconcile the thread after the lease
and use
`--reconcile-thread`; the guard performs the authenticated fingerprint lookup,
requires the message author to equal the sender identity captured by Slack
`auth.test`, persists an existing permalink, or returns the same message only
when no post exists. The actual sender of the Slack post must equal that
registered identity. Do not include tokens, credentials, raw secrets, or unrelated logs.
Credential-labelled text is safe only when its entire value is a canonical
redaction marker. If the capability record lacks a
fresh host relationship or the member cannot be verified live, fail closed
instead of guessing from a bot name. Use the guard's `--force` only for an
explicit user override.

Host escalation is a separate dependency lane. Only the failed bot gate becomes
`waiting_on_host`; continue all authorized work independent of it. A host reply
is recovery evidence, never a review verdict or approval, and does not alter the
result ledger or exact-head contract. Automatic recovery requires a safe,
idempotent probe from the checked-in trusted-probe registry. Registration and
execution both require an exact match on probe ID, executable content digest,
fixed arguments, and allowed failure code. The guard binds the canonical
registry entry's trust hash into the record and attestation, then revalidates it
immediately before execution. A caller-selected or self-attesting probe is not
recovery authority.

If no trusted probe exists for the failure, register the escalation without
probe options. It remains `result_ledger_only`: the host can still be notified,
but `set-state --state recovered` fails closed and the request identity cannot
auto-resume from that host ledger. Add a real probe to
`references/recovery-probes.json` only after its executable, fixed arguments,
and failure-code scope have been reviewed.

For a trusted probe, `set-state --state recovered` must run that exact probe
successfully and persist an attestation whose request identity, failure code,
condition hash, and trusted-registry hash match the registered values. Each
attestation has a one-time ID and nanosecond timestamp strictly newer than the
failed request. Probe output must decode to a JSON object; lists, scalars,
missing fields, and malformed container shapes in the request, host-escalation,
or recovery ledgers fail closed as structured CLI rejections instead of escaping
the guard. A present key with a JSON `null` value is malformed, not an absent
record, and must never be overwritten by registration. The probe process
receives only a fixed minimal environment:
`PATH`, `LANG`, `LC_ALL`, and the three `SLACK_REVIEW_RECOVERY_*` binding values;
it never inherits Slack, GitHub, or unrelated parent-process credentials. The
registered probe also binds normalized argv and the executable content digest.
At recovery, the guard opens the source once, hashes and stages those same bytes
in a private executable snapshot, then runs only that snapshot; replacement of
the original path before or during execution cannot change the verified bytes.
Recovery fails if the opened content or trusted registry entry differs from
registration. Legacy failed request records without trustworthy nanosecond
metadata cannot consume recovery attestations. Resume through
`request_guard.py register` with that host-recovery ledger and the same request
identity; caller-supplied prose cannot unlock the retry. Do not ask the user to
restart the workflow or claim completion, merge, or deploy through an
unsatisfied required gate.

## Triage findings independently

Verify every finding against the revision it names. One report can contain both
a real defect and an incorrect claim.

- `confirmed`: reproduce from code or behavior, then fix within scope.
- `needs verification`: obtain the missing runtime, product, or data evidence.
- `rejected`: provide reproducible evidence, ideally a command such as
  `git show <sha>:<path>` plus the relevant output, and invite correction.

Do not let a reviewer's passing claim replace a check you can run yourself.
For a regression test written from a finding, mutation-check it when practical:
make the defect observable, confirm the test fails, restore the fix, and confirm
it passes.

For security, permissions, payments, privacy, destructive operations, release
gates, and required status checks, failure-path or mutation validation is
mandatory unless it is impossible in the available environment. Record the
specific unverified path instead of silently downgrading it to optional.

## Reviewer capabilities

Treat reviewer capabilities as verified, revisable facts: accepted Slack sender
identities, GitHub review-object support, available verdicts, whether new pushes
are watched automatically, and concurrency behavior. Use a workspace adapter
or live evidence to select the request mode. Do not infer capability from a bot
name or from one successful Slack post.

Store and refresh shared observations in the external workspace registry selected
by `SLACK_REVIEW_CAPABILITIES`. Follow
[references/reviewer-capabilities.schema.json](references/reviewer-capabilities.schema.json)
and validate that external file with `scripts/validate_capabilities.py`. The
bundled `references/reviewer-capabilities.json` is an empty immutable default;
never write live observations into the installed package. Refresh stale external
observations before relying on them for sender identity or result-ledger choice.
Record `structured_output` as `supported`, `not_observed`, or `unknown`; a
missing field in a legacy capability record is `unknown`, while a
provider-native approval with an empty body is `not_observed`, not `supported`.
An empty-body exact-head `APPROVED` event is also non-terminal under the
canonical GitHub ledger contract: the review body must preserve the substantive
clean summary. Do not apply that requirement to historical or non-requested
review evidence that cannot complete the active request.

## Improve the review workflow

After the review loop reaches an accepted exact-head result with no actionable
finding, or a precise terminal blocker, run the retrospective in
[references/review-retrospective.md](references/review-retrospective.md).
Base changes on observed evidence, not reviewer preference alone. A material
skill improvement discovered after an accepted exact-head result belongs to a
separate follow-up branch or PR by default; do not rewrite the approved branch
and manufacture another approval round. Continue changing the current PR only
for a genuine actionable finding delivered before the loop closes, for the
first consecutive `infrastructure_only` proposal that the active-loop budget
routes to `current_candidate`, or when the user explicitly asks to reopen that
approved candidate. The budget exception applies only while the loop is still
open; it never reopens an accepted result.

If watcher heuristics disagree with an explicit provider approval, reconcile
the raw provider action, revision, body, finding count, and unresolved threads.
When that evidence proves an accepted zero-finding result, close the current
review loop and route the watcher defect to the follow-up. Do not use a
monitoring false positive as authority to invalidate an otherwise valid review.
The final response to the user must include the retrospective's observed skill
behavior, friction, recommendation, rationale, and disposition. Persisting it
only in Slack or a request ledger does not deliver it to the user, and a bare
`no_change` label is insufficient.

## Workspace adapters

Keep workspace-specific reviewers, channels, identity evidence and sender rules
outside the installed package. The bundled capability registry is empty. Supply
`SLACK_REVIEW_CAPABILITIES` with a verified workspace registry and use
`SLACK_TOKEN` (or the explicitly supplied legacy `SLACK_MCP_XOXP_TOKEN`) only when
an authorized direct Slack API verification/reconciliation is needed. The default
resolver and host guard never read Claude configuration files. A workspace may
set `SLACK_REVIEW_RESOLVER` to a trusted executable implementing the documented
live identity contract for host recovery. Keep that choice explicit.

Read [references/workspace-adapters.md](references/workspace-adapters.md) for the
configuration and resolver contract. Do not copy a business workspace's identity
cache into the generic package or infer authority from sample names.

## Completion check

Before reporting completion, confirm:

- the reviewer saw the intended pushed revision;
- the remote head still equals the preflighted/requested revision;
- the result is substantive and revision-bound;
- the result arrived in the completion contract's ledger;
- when the contract required a GitHub provider state, the accepted review
  object's state matches it;
- every finding has an explicit disposition;
- any result recovered after artifact closure is proven by its evidence
  timestamp to predate closure;
- authorized fixes are present on the PR head and verified;
- re-review was explicitly requested after fixes;
- every known PR-resident status update was committed before the final review;
- no post-verdict commit was created merely to record the verdict;
- the latest result leaves no actionable finding, or the remaining blocker is
  stated plainly.
- the review-process retrospective records its evidence, recommendation, and
  disposition and is summarized in the final user-facing response;
- any follow-up skill change produced by that retrospective has its own review
  boundary and does not invalidate the completed artifact's approval.
