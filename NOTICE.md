# Source and redistribution

The initial two skills and generic preflight regression cases were adapted from
BeFreed-ai/freed_skills. See provenance.json for source paths, revision and hashes.
Upstream Git history records contributions from zw and YuZhang71, including
Codex-assisted work. This repository preserves that attribution; its flattened
migration history is not a claim that all inherited content was created here.

No repository-level LICENSE was found in the inspected source checkout. On
2026-10-23 the repository owner explicitly confirmed the right to publicly
redistribute these two skills and their related tests, including other
contributors' changes, under the MIT license. This package relies on that
confirmation, rather than inferring permission from source access or authorship.
This confirmation covers that first batch. On the same date, the user explicitly
requested the additional migration of `requesting-slack-bot-review` into this
MIT package, and excluded other additional skills. Its source revision and file
hashes are recorded separately in provenance.json. No broader upstream migration
or redistribution scope is inferred.

Migration adaptations: remove automatic GitHub account switching, document
installation-relative helper lookup, remove the Freed-specific update command,
and relocate generic preflight regression cases while excluding Freed wrapper
tests. Fixture names are generalized, and regression coverage includes
authentication preservation, verified target-base snapshots and executable
permission integrity during installation.

The additional review-workflow migration preserves generic review-state,
delivery, watcher and host-recovery logic and related regression coverage. Its
adaptations replace implicit source refresh with recorded package integrity,
resolve a same-pin sibling gate without changing authentication, and replace
workspace identities/private-client credential discovery with explicit external
configuration. The business-specific workspace adapter and live reviewer roster
are not redistributed. Fixtures use illustrative workspace identities.

The installer and its tests are new work in agent-skills. Client adoption and
upstream compatibility are separate from packaging; this change does not switch
the currently discovered requesting-slack-bot-review installation.
