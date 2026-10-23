import datetime as dt
import json
import os
import subprocess

import pytest

from .helpers import SKILL


def resolver_env(tmp_path, *, github_fails=False, invalid_slack=False, host_is_bot=False, observed_at=None):
    home = tmp_path / "home"
    home.mkdir()
    # A misleading private-client token must never be discovered by this package.
    (home / ".claude.json").write_text('{"mcpServers":{"slack":{"args":["SLACK_MCP_XOXP_TOKEN=wrong-client-token"]}}}')
    registry = tmp_path / "reviewers.json"
    registry.write_text(json.dumps({"reviewers": {
        "example-reviewer": {"slack_user_id": "U000BOT", "github_login": "example-reviewer",
                             "host": {"slack_user_id": "U000HOST", "role": "operator",
                                      "observed_at": observed_at or dt.datetime.now(dt.timezone.utc).isoformat()}},
        "secondary-reviewer": {"slack_user_id": "U000ALT"},
    }}))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text('''#!/usr/bin/env python3
import json,os,sys
from urllib.parse import urlparse,parse_qs
if os.environ.get('INVALID_SLACK')=='1': print('not-json');sys.exit(0)
url=sys.argv[-1]; user=parse_qs(urlparse(url).query).get('user',[''])[0]
if user: print(json.dumps({'ok':True,'user':{'id':user,'deleted':False,'is_bot':user=='U000HOST' and os.environ.get('HOST_IS_BOT')=='1'}}))
else: print(json.dumps({'ok':True,'members':[]}))
''')
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\n[ \"$GITHUB_FAILS\" = 1 ] && exit 1\necho example-reviewer\n")
    curl.chmod(0o755)
    gh.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
           "SLACK_TOKEN": "fixture-token", "SLACK_MCP_XOXP_TOKEN": "", "SLACK_REVIEW_CAPABILITIES": str(registry),
           "GITHUB_FAILS": str(int(github_fails)), "INVALID_SLACK": str(int(invalid_slack)), "HOST_IS_BOT": str(int(host_is_bot))}
    return env


def resolve(env, *args):
    return subprocess.run([str(SKILL / "resolve_bot.sh"), *args], env=env, capture_output=True, text=True)


def test_external_registry_verifies_both_provider_identities(tmp_path):
    result = resolve(resolver_env(tmp_path), "--json", "--require-github", "example-reviewer")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"reviewer": "example-reviewer", "slack_user_id": "U000BOT", "github_login": "example-reviewer"}


def test_live_verified_host_is_required_for_recovery(tmp_path):
    result = resolve(resolver_env(tmp_path), "--json", "--require-host", "example-reviewer")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["host"] == {"slack_user_id": "U000HOST", "role": "operator"}


@pytest.mark.parametrize("options,args,error", [
    ({"github_fails": True}, ["--require-github", "example-reviewer"], "could not be verified live"),
    ({"host_is_bot": True}, ["--json", "--require-host", "example-reviewer"], "configured host is a bot"),
    ({"observed_at": "2000-01-01T00:00:00Z"}, ["--json", "--require-host", "example-reviewer"], "relationship observation is stale"),
    ({}, ["--json", "--require-host", "secondary-reviewer"], "no verified host identity"),
    ({}, ["--require-github", "secondary-reviewer"], "no verified GitHub identity"),
    ({"invalid_slack": True}, ["--json", "example-reviewer"], "invalid JSON"),
])
def test_unverified_identity_or_provider_failure_is_rejected(tmp_path, options, args, error):
    result = resolve(resolver_env(tmp_path, **options), *args)
    assert result.returncode != 0
    assert error in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_explicit_token_does_not_read_private_client_credentials(tmp_path):
    env = resolver_env(tmp_path)
    env.pop("SLACK_TOKEN")
    result = resolve(env, "--json", "example-reviewer")
    assert result.returncode == 2
    assert "explicitly" in result.stderr


def test_empty_bundled_registry_has_no_workspace_identity_defaults(tmp_path):
    env = resolver_env(tmp_path)
    env.pop("SLACK_REVIEW_CAPABILITIES")
    result = resolve(env, "--require-github", "example-reviewer")
    assert result.returncode != 0
    assert json.loads((SKILL / "references/reviewer-capabilities.json").read_text()) == {"reviewers": {}}
