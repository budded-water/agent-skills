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
This confirmation covers this first batch, not unrelated upstream skills.

Migration adaptations: remove automatic GitHub account switching, document
installation-relative helper lookup, remove the Freed-specific update command,
and relocate generic preflight regression cases while excluding Freed wrapper
tests. Fixture names are generalized, and regression coverage includes
authentication preservation, verified target-base snapshots and executable
permission integrity during installation.

The installer and its tests are new work in agent-skills. Existing operational
installations and the upstream repository have not been switched by this PR.
