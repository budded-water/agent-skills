# Review state contract

For GitHub pull requests, `github_review` is the default expected ledger when
the reviewer has a verified GitHub identity. `slack` and `either` remain
explicit fallbacks for reviewers that cannot create GitHub review objects; they
must not be selected merely to accept an easier Slack result.

The canonical provider mapping is:

- findings -> one exact-head `COMMENTED` review containing the findings;
- clean plus explicit native-approval authorization -> one exact-head
  `APPROVED` review whose body contains the summary;
- clean without native-approval authorization -> one exact-head `COMMENTED`
  review containing the zero-finding conclusion.

Slack may acknowledge progress and notify the final verdict, head, and GitHub
review URL. It is not a second detailed findings ledger. Under a
`github_review` contract, a Slack-only result remains non-terminal and a
missing GitHub review URL must be reported as an undelivered write, not success.
An exact-head `APPROVED` review must also have a non-whitespace body containing
the clean summary; an empty native approval is not a complete canonical result.
Historical reviews and reviews by other identities cannot satisfy the request
and must not make an otherwise valid current snapshot fail this check.

Use this normalized snapshot when Slack is read through MCP or when testing the
classifier offline:

```json
{
  "request": {
    "head": "full commit SHA",
    "remote_head": "current remote head SHA",
    "artifact_state": "OPEN",
    "merged_at": null,
    "closed_at": null,
    "merge_commit_sha": null,
    "expected_ledger": "github_review",
    "acceptable_verdicts": ["approved", "with_findings"],
    "required_github_review_state": "APPROVED",
    "slack_reviewer": "U123",
    "requester_slack_id": "U456",
    "github_reviewer": "review-bot-login"
  },
  "slack_messages": [
    { "user": "U123", "text": "...", "ts": "1787373197.811679", "edited": { "ts": "1787373200.000000" }, "permalink": "..." }
  ],
  "github_reviews": [
    { "user": "review-bot-login", "commit_id": "...", "state": "APPROVED", "body": "...", "submitted_at": "2026-08-22T04:37:09Z", "html_url": "..." }
  ],
  "github_comments": [
    { "user": "review-bot-login", "body": "Review @ <sha> ...", "created_at": "...", "updated_at": "...", "html_url": "..." }
  ],
  "review_threads": [
    { "user": "review-bot-login", "commit_id": "...", "created_at": "...", "updated_at": "...", "is_resolved": false, "is_outdated": false }
  ]
}
```

Run:

```bash
./scripts/build_review_snapshot.py --request request.json \
  --slack-messages slack.json --github-reviews reviews.json \
  --github-comments comments.json --review-threads threads.json \
  --output snapshot.json
./scripts/watch_review.py --snapshot snapshot.json
```

Each input collection is a JSON array in the shape shown above. The builder
validates required request identities and field schemas, writes one
canonical snapshot, and fails closed when `github_review` is requested without
`github_reviewer`. The `either` ledger also requires that identity because a
GitHub review is eligible evidence. Both `head` and the freshly re-read
`remote_head` are required
full hexadecimal SHAs; omitting `remote_head` must never disable supersession
checking. `artifact_state` is also required and must be `OPEN`, `MERGED`, or
`CLOSED`; include `merged_at` and `merge_commit_sha` when the provider exposes
them. This avoids reimplementing snapshot assembly for each MCP
connector. Connector prose still needs to be reduced to normalized fields; do
not parse display text as identity when the provider exposes IDs.

When `expected_ledger` is omitted, the builder and classifier default it to
`github_review` and persist that normalized value. Callers that intentionally
need Slack completion must pass `slack` or `either` explicitly.

Snapshot producers created before this contract version must add
`artifact_state`; omission now fails closed instead of silently disabling PR
lifecycle termination. Existing reviewer capability caches must likewise add a
Slack member ID only for Slack-eligible ledgers and, for GitHub-capable ledgers,
a verified GitHub login before validation succeeds.

The classifier repeats security-critical boundary checks instead of assuming
every caller used the builder. Missing or unknown `artifact_state` yields
`watcher_error`. A `slack` or `either` ledger without `slack_reviewer` also
yields `watcher_error`; absence must never degrade the sender filter to “any
Slack user.” A GitHub-only contract does not require Slack identity because
Slack evidence cannot satisfy it.

When `artifact_state` is `MERGED` or `CLOSED`, qualifying evidence must carry a
provider timestamp. The classifier uses the latest meaningful evidence time
(`submitted_at`, `updated_at`, Slack edit time, or creation time) and accepts it
only when it is no later than `merged_at` or `closed_at`. Missing or malformed
timestamps fail closed, so a result posted or edited after closure cannot be
recovered as a pre-closure review. A present malformed timestamp is an input
error even when another timestamp is valid, and Slack `edited` must be an object;
when the `edited` field is present it must contain a valid non-empty `ts`.
Explicit null/empty edit metadata cannot fall back to the original message time.
The direct classifier returns structured `watcher_error` instead of traceback.

The builder filters records to the identities eligible under the request.
Malformed or unattributable historical records that cannot serve as
revision-bound evidence are dropped and reported in the snapshot's `warnings`
array; they do not prevent a valid current result from being classified.
Malformed fields on an attributable, structured evidence record still fail
closed when accepting them could change its meaning.

GitHub review and thread `commit_id` fields must also be full SHAs. Slack text
may name a 7–40 character SHA prefix because it is matched against the known
full request head. `acceptable_verdicts` is a non-empty subset of `approved`
and `with_findings`; `no_verdict` can never be made terminal by configuration.
`required_github_review_state` is optional and currently accepts only
`APPROVED`. It is valid only for `github_review` or `either`, must be persisted
in the request guard, and should be set only when a verified merge or release
ruleset requires the GitHub provider action.

The classifier emits one JSON object with `transport_state`, `review_verdict`,
`terminal`, `source`, `url`, `reason`, and — on live watcher output —
`monitoring_scope` and `verdict_evidence`.

`terminal` means the current watcher wait can stop because an acceptable result
arrived. A terminal `with_findings` still leaves the overall review loop open
for triage, fixes, and re-review.

For `--snapshot` or `--once`, exit 0 means a terminal acceptable result, exit 4
means superseded, exit 5 means the PR merged/closed without an acceptable
exact-head result, and exit 1 means another non-terminal classification.
Provider or input errors use exit 2.

An `approved` terminal result is evidence in an external ledger; it must not be
written back into the reviewed branch merely to record completion. Any branch
change after that result creates a different revision and invalidates its
coverage.

Transport states are `acknowledged`, `delivered`, `stale`, `silent`,
`superseded`, `artifact_closed`, and `watcher_error`. Verdicts are `approved`,
`with_findings`, `no_verdict`, and `none`. Existing qualifying exact-head
evidence is classified before lifecycle termination, so a review delivered
before merge remains discoverable after merge.

Live watcher output also carries `monitoring_scope`: `slack_and_github` or
`github_only`. In GitHub-only mode, `silent` is scoped to the GitHub ledger and
must not be promoted to a claim about Slack delivery. Slack MCP acknowledgement
is recorded separately in the request guard while the GitHub watcher continues.
For a correction request on an unchanged head, `--after-review-id` supplies a
provider watermark captured before the request. Live snapshot collection ignores
reviews at or below that ID so old exact-head findings cannot satisfy the new
wait. The watermark is live-collection metadata, not a substitute for reviewer,
head, URL, body, or provider-state validation on the newer review.

Live watcher output also carries `verdict_evidence`: the phrase a prose-derived
verdict hinged on, taken from the decision point itself rather than re-derived
over the whole body. It is populated **only** for a `with_findings` verdict that
came from keyword matching — when a reviewer supplied a structured `VERDICT` or
a non-zero `ACTIONABLE_FINDINGS`, the verdict is authoritative and there is
nothing to explain.

**What a null means depends on the verdict.** Every keyword-derived
`with_findings` carries evidence, so on a `with_findings` result null does mean
the verdict came from structured fields or provider state rather than keyword
matching — it is authoritative, not unexplained. On an `approved` result null
says nothing: approval may have come from structured fields, provider state, or
prose. When the field IS populated, treat it as the guess to check, not as a
finding.

The request guard uses state-specific active deadlines. A newly registered
`requested` record that has not yet confirmed Slack delivery has a separate
five-minute posting lease and expires with `expiration_reason: posting_timeout`;
that is not evidence of bot silence and requires thread/provider reconciliation.
The acknowledgement deadline starts only after the verified post is recorded
with `sent_at_ns` and its permalink. A delivered `requested` record then expires
after the acknowledgement timeout (60 seconds by default) with
`expiration_reason: acknowledgement_timeout`; an `acknowledged` or
`in_progress` record expires after the result timeout (600 seconds by default)
with `expiration_reason: result_timeout`. The persisted `timeout_seconds` binds
the evidence to the effective deadline. The deprecated `--silence-timeout`
compatibility override records `legacy_silence_timeout` instead. Expiry never
authorizes a retry by itself: Slack and the result ledger must still be
reconciled. The guard also rejects a second active reviewer for the same
repository, artifact, and head until the first identity leaves its active
state. Legacy records without the delivery-confirmation marker keep their
existing acknowledgement-timeout interpretation during migration.

`set-state` records already reconciled provider evidence and therefore does not
run local timeout expiry before applying that evidence. This matters when an
acknowledgement arrived within the SLA but the orchestration layer persisted it
after the local clock boundary. Moving a request to any non-expired state clears
provisional `expiration_reason` and `timeout_seconds` metadata so the ledger
cannot simultaneously claim active progress and timeout.
If failover already activated a successor reviewer for the exact candidate, a
late `set-state` transition from the prior reviewer back into `requested`,
`acknowledged`, or `in_progress` is rejected as `another_reviewer_active`.
Reconciled evidence remains in the provider ledger but cannot reactivate the
superseded review path.

`github_review` requires a review object. An issue comment or Slack recap may
prove acknowledgement but cannot satisfy that ledger. An unresolved,
non-outdated current-head thread turns a COMMENTED review into
`with_findings`. An unambiguous COMMENTED body can also provide the verdict;
generic completion prose remains `no_verdict`.

When `required_github_review_state` is `APPROVED`, a zero-finding COMMENTED
review remains `delivered` / `approved` evidence but is non-terminal with a
provider-state mismatch reason. A matching exact-head APPROVED action is
terminal only when its body contains a substantive clean summary and its
semantic verdict is acceptable. A COMMENTED or APPROVED review with actionable
findings remains terminal `with_findings`, because the provider-action gate
must never delay triage.

An `APPROVED` review whose body or unresolved current-head threads contain
findings is classified as `with_findings`; the GitHub action does not suppress
the review content. Mentions that explicitly say prior findings were fixed,
resolved, addressed, or are "not a finding" are not active findings; a new
finding elsewhere in the same body still wins. A canonical verdict sentence
such as `APPROVE - no actionable findings.` is authoritative over later
explanatory meta-text that merely discusses finding detection; a contradiction
inside that verdict sentence still yields `with_findings`. For an edited Slack progress message, a substantive
current-head verdict takes precedence over an older acknowledgement marker.
A current-head Slack response whose wording has no recognized verdict remains
visible as non-terminal `delivered` / `no_verdict` evidence for human triage,
unless it is still explicitly a progress or acknowledgement message; those
remain `acknowledged` even when they echo the current head.

Prefer explicit result fields when the reviewer supports them:
`VERDICT: APPROVED|WITH_FINDINGS` and `ACTIONABLE_FINDINGS: <integer>`.
A positive actionable-finding count always yields `with_findings`; otherwise
the structured verdict is authoritative over explanatory prose. When a result
contains only `ACTIONABLE_FINDINGS: 0`, that metadata line is not itself prose
evidence of a finding; another verdict signal is still required. A provider
approval plus that zero count is authoritative over later explanatory meta-text,
while an active finding stated in the approval sentence still wins. Normalize
negated or resolved phrases such as `no actionable findings` and `no blockers`
before deciding that the approval sentence contains an active finding.

GitHub evidence also requires `github_reviewer`. A GitHub-capable ledger without
that identity yields a structured `watcher_error`; a non-matching identity is
excluded. Absence never means "accept any reviewer".

## Host-recovery dependency state

Bot-host recovery is tracked outside this review transport snapshot. Do not add
`waiting_on_host` to `transport_state`: it is a dependency state in the separate
host-escalation ledger, while the request guard and watcher retain their normal
request and provider states.

The escalation identity is:

```text
<reviewer>:<repo>:<artifact>:<full-head>:<stable-failure-code>
```

The host ledger moves through `waiting_on_host`, `notified`, `recovered`, or
`failed`; its dependency state is `waiting_on_host`, `ready_to_resume`, or
`recovery_failed`. Registration preserves `expected_ledger` and records
`completion_contract_unchanged: true`. A host message or a `recovered` state is
not a review result: only evidence admitted by this review state contract can
satisfy the original ledger and exact-head requirement.

Before `notified`, the host ledger also keeps a bounded `pending_send` lease.
An expired lease requires a read of the original Slack thread. When that
authenticated reconciliation finds no notification with the stored recovery
fingerprint from the Slack sender captured at registration through `auth.test`,
the same message may be claimed again. A matching message from another sender is
not delivery evidence. When it finds the registered sender's message, the guard
persists its permalink and returns no sendable message. An active lease or
confirmed permalink remains deduplicated.

`recovered` requires `recovery_mode: trusted_probe` and an exact registry match
for probe ID, executable digest, fixed argv, and failure-code scope at both
registration and execution. The attestation binds the probe hash and canonical
registry trust hash to the recorded recovery-condition hash.
`request_guard.py register` consumes that attestation from the host ledger before
it reopens the same failed request identity. The attestation must have a
previously unconsumed one-time ID, be strictly newer than that request's failed
state at nanosecond resolution, match its expected ledger, and repeat the same
trusted-registry hash; the guard carries forward the original reviewer and
ledger metadata. Free-form recovery evidence cannot change request state.
The ledger envelope, escalation collection, selected escalation record, and
recovery attestation must each be JSON objects. Missing, list, or scalar shapes
are structured invalid-attestation failures, never permission to resume and
never uncaught attribute errors.
The request-state and host-escalation ledgers apply the same boundary to their
own envelopes, collections, and selected records before register, show, or
set-state reads or mutates them. Malformed persisted state produces a structured
CLI rejection and is never silently replaced with an empty ledger. Key absence
is distinct from a key whose value is JSON `null`; the latter is a present,
malformed record and registration must leave the state file unchanged.

An escalation registered without a trusted probe uses
`recovery_mode: result_ledger_only`. It may be notified and reconciled, but it
cannot enter `recovered` or unlock request-guard retry. This is the required
fail-closed behavior when no reviewed capability-specific probe exists.

Missing `updated_at_ns` on a legacy failed request is not freshness evidence and
fails closed. The probe attestation also binds the executable content digest in
addition to normalized argv; replacing the file at the same path invalidates
recovery. The probe receives a fixed minimal environment containing only
`PATH`, `LANG`, `LC_ALL`, and the request identity, failure code, and
condition-hash binding variables. Parent-process credentials and unrelated
environment variables are not inherited.

After verified recovery, resume the same request identity under the request
guard's retry and reconciliation rules. Do not manufacture a new head or review
identity merely because the infrastructure dependency was repaired.
