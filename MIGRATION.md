# Selected skill migration and compatibility plan

Status: the initial two skills are in main. The only additional requested skill
is `requesting-slack-bot-review`, packaged with its generic helpers and regression
tests. User installation adoption and upstream compatibility changes remain
separate. Source versions and the unavailable live upstream check are recorded
per migration in provenance.json. See the
[caller inventory and dependency contract](docs/freed-skills-integration.md)
for the prepared upstream integration design.

1. Retain the first-batch redistribution authorization and the later explicit
   selection in NOTICE.md. Before proposing an upstream source transition,
   verify an accessible upstream head and reconcile changes after each recorded
   provenance revision. The review workflow uses the clean locally installed
   upstream merge snapshot; current API/SSH upstream access was unavailable.
2. Deliver each package change through its topic branch, checks and PR. Review
   and validate before any separately authorized merge. No additional upstream
   skills are included in this migration.
3. After a reviewed version is available, propose an OpenSpec change and PR in
   freed_skills if upstream adoption is requested. Resolve the exact target
   commit; never pin an unmerged candidate or a moving main branch. Retain
   Freed-specific configuration in freed_skills.
4. An upstream adoption must update both Freed installers and their refresh
   logic coherently. The external review workflow requires its same-pin
   preflight sibling. Its checker is local and read-only, not a replacement for
   the old Freed source-refresh mechanism. Keep workspace-specific identity
   configuration and any compatibility entry point upstream; do not leave an
   update command pointing to a removed implementation.
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
