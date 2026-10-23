# Post-review workflow retrospective

Run this after the overall review loop closes, not after every polling cycle.
A `with_findings` round supplies evidence, but wait until its findings are
dispositioned and the current head is accepted before deciding how the workflow
itself should change. A precise terminal blocker may also trigger the review.

## Evaluate the process

Capture a compact record:

```yaml
outcome: approved | blocked
review_rounds: <integer>
round_causes: [finding_driven | infrastructure_only]
consecutive_infrastructure_rounds: <integer>
process_observations:
  - evidence: <command, ledger event, finding, or timestamp>
    impact: <delay, duplicate work, false state, or avoided failure>
    reusable: true | false
recommendation: no_change | update_skill | separate_follow_up
rationale: <why this is or is not a reusable workflow issue>
disposition: <what was changed, proposed, or intentionally left unchanged>
```

## Deliver it to the user

The retrospective is part of the final handoff, not internal bookkeeping. The
final user response MUST summarize:

- which review skills, gates, or helpers materially affected the outcome;
- the evidence and impact for each observed success or friction point;
- the recommendation, rationale, and actual disposition.

Do this even when the same record was posted to Slack or stored in the request
ledger. Do not reduce the user-facing analysis to `no_change`; explain why no
change is warranted. Keep the summary proportional to the evidence, but make it
independently understandable without opening the Slack thread.

Evaluate at least:

- whether identity, delivery, revision, lifecycle, and result-ledger checks
  classified the request correctly;
- which gate or test caught each problem, and which invariant was missing;
- manual recovery, duplicate work, confusing output, or provider-specific
  friction that could be automated or documented;
- drift between implementation, tests, skill instructions, specs, task lists,
  and PR text;
- whether the observation is reusable across reviews or a one-off artifact.

## Choose the disposition

Recommend `update_skill` only when evidence shows at least one of:

- a reproducible correctness or fail-closed gap;
- a missing invariant or propagation boundary;
- repeated/manual work with a stable automation path;
- instructions that caused a material ambiguity or recovery step.

Use `no_change` for one-off reviewer taste, already-covered behavior, or a
change whose maintenance cost exceeds its demonstrated impact. Use
`separate_follow_up` when the improvement is valid but belongs to another
system, needs product input, or materially expands the reviewed scope.

If the user already authorized skill evolution, implement `update_skill` in the
canonical skill source, synchronize tests/specs/docs, and open a separate
follow-up PR when the triggering review already reached an accepted exact-head
result. Update the current PR only while resolving a genuine actionable finding
from its still-open review loop, when the active-loop budget routes the first
consecutive `infrastructure_only` proposal to `current_candidate`, or when the
user explicitly reopens the approved candidate. The budgeted exception does not
apply after an accepted result.
Otherwise present the recommendation and request authority before external
mutation. Never silently edit an installed copy as the only source of truth.

## Preserve the review boundary

An accepted exact-head review closes that artifact's review boundary. Preserve
the approved head. Put retrospective-driven process improvements in a separate
follow-up branch or PR with their own preflight and review loop; link the two
artifacts for traceability instead of appending more historical approvals to the
completed PR.

If watcher output conflicts with an explicit provider approval, reconcile the
raw provider action, exact revision, body, structured count, and unresolved
threads before deciding the loop is still open. An accepted zero-finding result
closes the current loop even when the monitoring heuristic reports a false
positive; record and fix that heuristic defect in the follow-up.

Track every proposed revision before mutation with `review_rounds`,
`round_causes`, and `consecutive_infrastructure_rounds`. Classify the cause, not
the touched files. A round that addresses an actionable current-candidate
finding is `finding_driven`, takes precedence over infrastructure labels, stays
on the current PR, and resets the counter to zero. Any other review-process,
watcher, or retrospective revision is `infrastructure_only` and increments the
counter. The second consecutive infrastructure-only proposal is not committed
to the current branch; force `separate_follow_up` instead. Evaluate the next
round independently, so a later genuine finding remains finding-driven and
resets the counter.

Run `scripts/review_round_budget.py` before each proposed candidate mutation and
persist its cause, route, and resulting counter in the review ledger. This
budget does not suppress genuine reviewer findings on the current code.

Keep a no-change retrospective outside the reviewed revision. Do not create a
commit whose only purpose is to record that the preceding head was approved.
