# First-batch migration and compatibility plan

Status: first-batch redistribution authorized under MIT; operational source
transition and upstream compatibility changes remain pending. See the
[caller inventory and dependency contract](docs/freed-skills-integration.md)
for the prepared upstream integration design.

1. First-batch redistribution under MIT was explicitly authorized by the owner
   on 2026-10-23; see NOTICE.md. Before proposing the operational source
   transition, verify the accessible upstream head and reconcile changes after
   the provenance revision. This candidate was prepared from the recorded local
   source; current remote upstream access was unavailable.
2. Create the authorized personal repository with a minimal default-branch
   baseline. Deliver the actual package on a topic branch through a PR. Review
   and validate it before any authorized merge; never use bootstrap to publish
   the feature directly to the default branch.
3. After a reviewed version is available, propose an OpenSpec change and PR in
   freed_skills. Resolve the exact target commit; never pin an unmerged candidate
   or a moving main branch. Retain Freed-specific configuration in freed_skills.
4. Update both Freed installers and their source-integrity/refresh logic as one
   coherent change. requesting-slack-bot-review currently expects its sibling
   preflight and its refresh mechanism reinstalls both review skills. Preserve
   that contract through a pinned compatibility wrapper/installer, with tests;
   do not simply delete the source directory or leave a dead update command.
5. The wrapper should resolve only a verified installed target and fail with an
   actionable installation message when absent. Do not fetch/execute a remote
   script at invocation time or modify global auth. Keep one implementation.
6. Exercise both clients' actual discovered paths, missing target, pin mismatch,
   modified copy, existing foreign symlink and rollback. Switch user installations
   only after the source/compatibility PRs are reviewed and the adoption is
   authorized. Keep the previously working version until verification succeeds.

The App durable-handoff proposal remains a separate open PR. Do not close it,
merge it or claim its procedure migrated in this first batch. A future generic
PR skill can own that workflow once its source and scope are settled.
