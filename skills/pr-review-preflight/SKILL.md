---
name: pr-review-preflight
description: Use before asking anyone — a bot, a teammate, a review skill — to review a PR, to check its exact head is pushed, CI-green, conflict-free, free of unresolved review threads and not a draft; also use when base drift may affect the change, when a review's line references have gone stale, when a reviewer reported a red or missing build, or when deciding whether "unresolved comments" should block.
---

# PR Review Preflight

## Overview

Asking for a review is spending someone's attention. This gate refuses to spend
it on a PR that will waste it.

**Core principle: a review is valid for the exact PR head it read.** Unpushed
changes, a changed head, running CI, conflicts and unaddressed threads can make
that evidence unsafe. New commits on the base branch do not change the PR head
by themselves, so base drift is reported but is not a generic blocker.

## When to Use

- Before any review request (Slack bot, GitHub reviewer, `codex-review`, human)
- Before re-requesting after a fix
- Before claiming a PR is "ready"
- When review findings point at lines that have since moved

**Not for:** deciding *what* to review, or triaging findings — that belongs to
whichever review skill you are driving.

## Run it

```bash
"<installed-skill-directory>/preflight.sh" [--require-current-base '<reason>'] [--allow-no-checks '<reason>'] <pr-number> [owner/repo]
```

Replace `<installed-skill-directory>` with the directory containing this SKILL.md. Requires bash, git, gh and jq. Use the caller-selected gh authentication; this gate never switches accounts.

Exit 0 = safe to ask. Non-zero = fix first. Repo and reviewer agnostic; run it
from the repo whose PR it is.

Repositories that intentionally have no CI may use `--allow-no-checks` with a
specific reason. This only overrides the absence of a check suite; pending or
failing checks still block. Do not use it when checks are expected but missing.

Base drift is a warning by default. The script automatically upgrades it to a
block when the PR and base-only changes overlap on an exact path, or both sides
touch common high-risk files such as migrations, schemas, API contracts,
lockfiles, or generated artifacts. Use `--require-current-base '<reason>'` for
semantic dependencies that path analysis cannot see, or when repository policy
explicitly requires a current base. Do not enable strict mode merely because
another unrelated PR merged.

| Check | Why it blocks |
|---|---|
| conflicting/unknown GitHub mergeability, detected path overlap or two-sided high-risk drift, or behind base under `--require-current-base` | The candidate cannot be merged safely as observed, or latest-base validation is required |
| local ≠ PR head or tracked worktree dirty | The reviewer reads the remote, not your disk — also catches a wrong branch and local-only candidate edits |
| CI not `SUCCESS` | `PENDING` or failure is not a pass; no configured checks requires an explicit, reasoned override |
| unresolved review threads | Existing feedback outranks new feedback |
| draft | Publish it first |
| closed, merged, conflicting, or unknown mergeability | The artifact cannot safely enter a new pre-merge review round |

Untracked files are warnings rather than blockers because they may be unrelated
to the PR; disposition them explicitly. The gate also reports preview evidence
separately from aggregate CI. A Vercel status backed by an ignored or skipped
build is `SKIPPED/UNAVAILABLE`, even when GitHub colors that context green.

## Base drift decision

The generic gate cannot decide whether two changes are semantically related.
It therefore separates observable state from the caller's risk decision. Path
comparison disables rename folding so both the old and new path participate:

| Situation | Result |
|---|---|
| Behind base, GitHub says mergeable, no detected path overlap or two-sided high-risk drift | Warn; continue review of the exact head |
| GitHub says conflicting | Block |
| PR and base-only changes overlap on a path | Automatically block and update the branch |
| Both sides touch migrations, schemas, API contracts, lockfiles, generated artifacts, or other recognized high-risk files | Automatically block and update the branch |
| A cross-file or semantic dependency is known but not detectable from paths | Rerun with `--require-current-base '<reason>'` and update the branch |
| Repository or merge queue explicitly requires current base | Use strict mode and block until current |
| GitHub says mergeable but local fetch/comparison fails | Warn that enhanced drift analysis was unavailable; strict mode still blocks |

Advancing the base does not move review line references. Updating or rebasing
the PR head does; after that change, request review again for the new head.

## The trap this exists to encode

**"Unresolved comments" has two sources and only one of them is resolvable.**

`gh pr view --json comments` returns **issue comments** — a bot's quota notice, a
status note, your own updates. They have no resolution state, so a gate built on
them blocks forever on noise.

Resolution lives only in GraphQL:

```
reviewThreads(first:100){nodes{isResolved isOutdated}}
```

Skip `isOutdated` threads: the code they referenced is gone.
The gate prints the reviewer, path, URL, and first-line title for each blocking
thread so the next action does not require a separate discovery query.

**And the opposite trap:** `reviewThreads` can be empty while real findings sit on
the PR, because a reviewer may post its findings as an ordinary issue comment
rather than a review. If your reviewer does that, this generic gate cannot see
them — the reviewer-specific layer must add that check (see
`requesting-slack-bot-review` for a worked example using a `## Review @ <sha>`
stamp).

## A merged PR cannot pass, by design

`local == PR head` is **unsatisfiable** once a PR is merged and its branch
deleted: there is nothing to check out. That reads like a gate bug and is not one
— it is the gate saying a merged PR does not belong in a review-request flow.
Post-merge you want a follow-up issue, not a review request. (Found by an agent
that tried to route a merged PR through this gate, 2026-08-20.)

## Extending it

Wrap, do not fork: run this script, then add reviewer-specific checks, then
combine exit codes. A copied gate drifts from this one silently.

```bash
"<installed-skill-directory>/preflight.sh" "$PR" || fail=1
my_extra_check || fail=1
exit "$fail"
```

## Common Mistakes

| Mistake | Consequence |
|---|---|
| Asking while CI is `PENDING` | Reviewer reports a red or absent build, or reviews code you are about to change |
| Treating every behind-base count as a conflict | Causes unnecessary rebases, CI reruns and approval churn for independent changes |
| Assuming automatic path checks understand every dependency | Misses cross-file semantic coupling; use strict mode with a recorded reason when known |
| Gating on issue comments | Blocks forever on a bot notice nobody can resolve |
| Trusting `reviewThreads` alone | Blind to reviewers that comment instead of reviewing |
| Skipping the gate "just this once" | The cheapest check in the loop, run after the expensive one has already failed |

## Red Flags

- About to post a review request without having run this
- Reporting a PR as "ready" from memory rather than from this output
- A review whose findings cite lines that no longer match the diff
