---
name: claude-code-project-context
description: Use in any repository that contains Claude Code assets such as CLAUDE.md, .claude/rules, .claude/commands, .mcp.json, or OpenSpec files, when Codex should reuse that project knowledge without changing Claude Code configuration.
---

# Claude Code Project Context

Use this skill to let Codex consume Claude Code project context across repositories while leaving Claude Code untouched.

## Discovery

From the current working directory, check for these files and directories before editing code:

- `AGENTS.md` - Codex-native project entry point, if present.
- `CLAUDE.md` - Claude Code project entry point.
- `.claude/rules/*.md` - detailed project rules.
- `.claude/commands/**/*.md` - Claude slash-command workflows that may document repeatable project processes.
- `openspec/AGENTS.md` - OpenSpec workflow, if present.
- `.mcp.json` - project MCP reference, if present.

Use `rg --files` or direct file reads. Do not scan large Claude state directories such as `~/.claude/file-history`, `~/.claude/usage-data`, or `~/.claude/plans` unless the user explicitly asks.

## Priority

1. If `AGENTS.md` exists, read it first and treat it as the Codex entry point.
2. If `CLAUDE.md` exists, read it for shared or historical project guidance.
3. Read only the relevant `.claude/rules/*.md` files for the current task.
4. If the task mentions proposal, spec, plan, architecture, breaking changes, or ambiguous new capability work, read `openspec/AGENTS.md` before editing.
5. Treat `.claude/commands/**/*.md` as workflow references, not commands to execute blindly.
6. Treat `.mcp.json` as a reference for server names and required environment variables only. Configure Codex MCP separately in Codex's native config.

## Boundaries

- Do not modify `CLAUDE.md`, `.claude/`, Claude settings, or Claude MCP config unless the user explicitly asks.
- Do not symlink `CLAUDE.md` and `AGENTS.md` automatically.
- Do not copy secrets, tokens, local auth files, or personal MCP environment values into repository files or Codex skills.
- Prefer checked-in project docs over Claude private memory or generated usage data.
