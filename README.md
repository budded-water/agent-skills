# agent-skills

Personal cross-project skills for Claude Code and Codex. The initial migration contains
two skills adapted from BeFreed-ai/freed_skills under the MIT license.
See [NOTICE.md](NOTICE.md) for attribution and [LICENSE](LICENSE) for terms.

## Scope

This repository is intended to own generic workflow skills. Business skills stay
with their business owner. Product-specific skills stay with agent-followups,
UI-eval or release tooling. Third-party skills retain their upstream source.
Skills guide decisions; executable gates enforce only the checks they implement.
Installing a skill does not guarantee invocation or authorize external actions.

The first batch contains:

- `pr-review-preflight`: revision-bound readiness checks. Requires bash, git,
  GitHub CLI (`gh`), jq and caller-configured authentication. It never switches
  the caller's global GitHub account. It performs API reads and a local git fetch.
- `claude-code-project-context`: reads existing project knowledge without
  modifying Claude configuration. Instructions only; no runtime dependency.

## Installation

Requires Python 3.10+ and git. No package installation is required for the installer.
Use a clean checkout at a reviewed full commit. Select the target and each skill
explicitly; do not run competing installers against the same directory.

```sh
python3 scripts/install.py --target /absolute/path/to/isolated-skills \
  --skill pr-review-preflight --skill claude-code-project-context \
  --source-commit FULL_REVIEWED_COMMIT
# Add --apply only after inspecting the plan.
```

Start with an isolated target. This does not switch any existing installation.
For later activation, inspect the client's actual skill discovery configuration
and same-named installations first; do not assume a universal install directory.
The installer never edits hooks, MCP configuration, credentials or permissions.
It includes LICENSE and NOTICE.md in each installed copy, records commit and
file hashes, checks source bytes and executable flags against Git objects at the requested commit,
verifies the staged copy before activation, is idempotent for an identical owned copy,
and refuses foreign, modified, symlinked or different-version destinations.
Ignored extra files within a selected skill and edits hidden from git status
are rejected. Keep skill directories free of generated files before installation.
If a copy fails validation, newly installed skills from that batch are rolled back.
This first version deliberately has no in-place upgrade/uninstall command.
For an upgrade, stage to a new target, review it and explicitly replace the old
installation after preserving its source marker and any local edits.

## Source transition

`provenance.json` records the source checkout commit and original file hashes.
`NOTICE.md` records attribution and the owner-confirmed redistribution permission.
The existing freed_skills checkout remains the operational source until the
new package is published, reviewed and explicitly adopted. No live installation
or existing review workflow has been switched by preparing this candidate.
The proposed compatibility transition is described in `MIGRATION.md`.

## Validation

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
bash -n skills/pr-review-preflight/preflight.sh
```

Tests use pytest plus bash and jq; git/GitHub responses in preflight tests are
fixtures. They do not establish live GitHub acceptance. Keep review evidence
bound to the tested commit and disclose any unrun live tests.
