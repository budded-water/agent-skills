import json
import os
import subprocess

import pytest

from .helpers import PIN, stage_pair


def run_gate(tmp_path, *, mode="clean", generic_exit=0, covered_override=False, tamper=False):
    skill = stage_pair(tmp_path, generic=f"#!/bin/sh\nexit {generic_exit}\n")
    if tamper:
        (skill.parent / "pr-review-preflight/preflight.sh").write_text("#!/bin/sh\nexit 0\n# unreviewed\n")
    home = tmp_path / "home"
    home.mkdir()
    commands = tmp_path / "commands.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('''#!/usr/bin/env python3
import os,sys,json
with open(os.environ['COMMAND_LOG'],'a') as stream: stream.write(' '.join(sys.argv[1:])+'\\n')
args=sys.argv[1:]; mode=os.environ['PROVIDER_MODE']
if args[:2]==['pr','view']: print('a'*40)
elif args[:2]==['api','user']: print('request-author')
elif any('/issues/' in a for a in args): print('')
elif any('/pulls/' in a for a in args):
 if mode=='unavailable': sys.exit(1)
 if mode=='malformed': print('[{"unexpected":true}]')
 elif mode=='covered': print(json.dumps([[{'commit_id':'a'*40,'user':{'login':'reviewer'},'state':'COMMENTED','html_url':'https://example.test/review/1'}]]))
 else: print('[[]]')
else: sys.exit(98)
''')
    gh.chmod(0o755)
    args = [str(skill / "preflight.sh")]
    if covered_override: args += ["--allow-covered", "explicit re-review after reconciliation"]
    args += ["34", "example/project"]
    result = subprocess.run(args, env={**os.environ, "HOME": str(home),
                            "PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(commands),
                            "PROVIDER_MODE": mode}, capture_output=True, text=True)
    return result, commands.read_text() if commands.exists() else ""


def test_codex_only_installation_passes_without_switching_auth(tmp_path):
    result, calls = run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREFLIGHT PASS" in result.stdout
    assert "auth " not in calls


@pytest.mark.parametrize("mode,reason", [("unavailable", "cannot read GitHub reviews"), ("malformed", "unexpected GitHub review schema")])
def test_unavailable_or_invalid_review_evidence_fails_closed(tmp_path, mode, reason):
    result, _ = run_gate(tmp_path, mode=mode)
    assert result.returncode == 1
    assert reason in result.stdout


def test_existing_exact_head_review_requires_explicit_reconciliation_override(tmp_path):
    result, _ = run_gate(tmp_path, mode="covered")
    assert result.returncode == 1
    assert "review covers this head" in result.stdout


def test_reasoned_repeat_review_override_preserves_generic_gate(tmp_path):
    result, _ = run_gate(tmp_path, mode="covered", covered_override=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generic_gate_execution_error_is_distinct_from_a_gate_verdict(tmp_path):
    result, _ = run_gate(tmp_path, generic_exit=2)
    assert result.returncode == 1
    assert "did not run (exit 2)" in result.stdout


def test_modified_dependency_blocks_before_provider_queries(tmp_path):
    result, calls = run_gate(tmp_path, tamper=True)
    assert result.returncode == 2
    assert "verification refused" in result.stderr
    assert calls == ""
