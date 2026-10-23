import json
import os
import subprocess

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "skills"
SCRIPT = ROOT / "pr-review-preflight" / "preflight.sh"
HEAD = "a" * 40


def run_preflight(
    tmp_path: Path,
    ci_state: str,
    *args: str,
    second_page_unresolved=False,
    tracked_dirty=False,
    untracked=False,
    preview_description=None,
    preview_conclusion=None,
    fetch_fails=False,
    behind_count=0,
    pr_changed_paths="",
    base_changed_paths="",
    pr_no_rename_paths=None,
    base_no_rename_paths=None,
    mergeable="MERGEABLE",
    pr_state="OPEN",
    cwd_repo="example/project",
    local_base_oid="b" * 40,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$GH_CALL_LOG\"\n"
        "case \"$1 $2\" in\n"
        "  'auth status') echo zw-befreed; exit 0 ;;\n"
        "  'api graphql') printf '%s\\n' \"$GH_GRAPHQL\" ;;\n"
        "  'repo view') [ -n \"$GH_CWD_REPO\" ] && printf '%s\\n' \"$GH_CWD_REPO\" ;;\n"
        "esac\n"
    )
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = status ]; then\n"
        "  case \"$*\" in\n"
        "    *--untracked-files=no) [ \"$GIT_TRACKED_DIRTY\" = 1 ] && echo ' M tracked.ts' ;;\n"
        "    *) [ \"$GIT_UNTRACKED\" = 1 ] && echo '?? local.tmp' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "case \"$1\" in\n"
        "  fetch) [ \"$GIT_FETCH_FAILS\" = 1 ] && exit 1 || exit 0 ;;\n"
        "  cat-file) exit 0 ;;\n"
        "  rev-list) echo \"$GIT_BEHIND_COUNT\" ;;\n"
        "  merge-base) echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;\n"
        "  diff)\n"
        "    case \"$*\" in\n"
        "      *--no-renames*\"$GH_HEAD\") printf '%s\\n' \"$GIT_PR_NO_RENAME_PATHS\" ;;\n"
        "      *--no-renames*origin/main) printf '%s\\n' \"$GIT_BASE_NO_RENAME_PATHS\" ;;\n"
        "      *\"$GH_HEAD\") printf '%s\\n' \"$GIT_PR_CHANGED_PATHS\" ;;\n"
        "      *origin/main) printf '%s\\n' \"$GIT_BASE_CHANGED_PATHS\" ;;\n"
        "    esac\n"
        "    ;;\n"
        "  rev-parse) [ \"$2\" = origin/main ] && echo \"$GIT_BASE_OID\" || echo \"$GH_HEAD\" ;;\n"
        "esac\n"
    )
    for executable in (bin_dir / "gh", bin_dir / "git"):
        executable.chmod(0o755)

    def page(nodes):
        contexts = []
        if preview_description is not None:
            contexts.append({
                "__typename": "StatusContext",
                "context": "Vercel",
                "state": "SUCCESS",
                "description": preview_description,
                "targetUrl": "https://vercel.example/preview",
            })
        if preview_conclusion is not None:
            contexts.append({
                "__typename": "CheckRun",
                "name": "Vercel Preview",
                "status": "COMPLETED",
                "conclusion": preview_conclusion,
                "detailsUrl": "https://vercel.example/preview",
            })
        return {"data": {"repository": {"pullRequest": {
            "baseRefName": "main", "baseRefOid": "b" * 40, "headRefOid": HEAD, "isDraft": False,
            "state": pr_state, "merged": pr_state == "MERGED", "mergeable": mergeable,
            "reviewThreads": {"nodes": nodes},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": None
                if ci_state == "NONE" else {"state": ci_state, "contexts": {"nodes": contexts}}}}]},
        }}}}
    pages = [page([])]
    if second_page_unresolved:
        pages.append(page([{
            "isResolved": False,
            "isOutdated": False,
            "comments": {"nodes": [{
                "author": {"login": "yu-bot"},
                "path": "scripts/watch_review.py",
                "url": "https://github.example/thread/1",
                "body": "P1: verdict can be masked",
            }]},
        }]))
    payload = json.dumps(pages)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_GRAPHQL": payload,
        "GH_CALL_LOG": str(tmp_path / "gh-calls.log"),
        "GH_HEAD": HEAD,
        "GIT_BASE_OID": local_base_oid,
        "GIT_TRACKED_DIRTY": "1" if tracked_dirty else "0",
        "GIT_UNTRACKED": "1" if untracked else "0",
        "GIT_FETCH_FAILS": "1" if fetch_fails else "0",
        "GIT_BEHIND_COUNT": str(behind_count),
        "GIT_PR_CHANGED_PATHS": pr_changed_paths,
        "GIT_BASE_CHANGED_PATHS": base_changed_paths,
        "GIT_PR_NO_RENAME_PATHS": pr_changed_paths
        if pr_no_rename_paths is None else pr_no_rename_paths,
        "GIT_BASE_NO_RENAME_PATHS": base_changed_paths
        if base_no_rename_paths is None else base_no_rename_paths,
        "GH_CWD_REPO": cwd_repo,
    }
    return subprocess.run(
        [str(SCRIPT), *args, "34", "example/project"],
        env=env,
        text=True,
        capture_output=True,
    )


def test_no_checks_requires_reasoned_override(tmp_path):
    blocked = run_preflight(tmp_path, "NONE")
    assert blocked.returncode == 1
    assert "no checks reported" in blocked.stdout

    allowed = run_preflight(
        tmp_path, "NONE", "--allow-no-checks", "repository has no CI workflows"
    )
    assert allowed.returncode == 0
    assert "OVERRIDDEN: repository has no CI workflows" in allowed.stdout


def test_override_does_not_allow_pending_checks(tmp_path):
    result = run_preflight(
        tmp_path, "PENDING", "--allow-no-checks", "repository has no CI workflows"
    )
    assert result.returncode == 1
    assert "still running, wait" in result.stdout


def test_unresolved_thread_on_later_page_blocks(tmp_path):
    result = run_preflight(tmp_path, "SUCCESS", second_page_unresolved=True)
    assert result.returncode == 1
    assert "unresolved threads           1" in result.stdout
    assert "scripts/watch_review.py" in result.stdout
    assert "https://github.example/thread/1" in result.stdout
    assert "P1: verdict can be masked" in result.stdout


def test_fetch_failure_warns_when_github_reports_mergeable(tmp_path):
    result = run_preflight(tmp_path, "SUCCESS", fetch_fails=True)
    assert result.returncode == 0
    assert "drift analysis" in result.stdout
    assert "warning" in result.stdout


def test_fetch_failure_blocks_when_current_base_is_explicitly_required(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        "--require-current-base",
        "shared schema changed on main",
        fetch_fails=True,
    )
    assert result.returncode == 1
    assert "drift analysis" in result.stdout
    assert "shared schema changed on main" in result.stdout


def test_behind_base_warns_without_blocking_by_default(tmp_path):
    result = run_preflight(tmp_path, "SUCCESS", behind_count=3)
    assert result.returncode == 0
    assert "behind base" in result.stdout
    assert "3 commit(s) — warning" in result.stdout
    assert "PREFLIGHT(generic) PASS" in result.stdout


def test_behind_base_blocks_when_current_base_is_explicitly_required(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        "--require-current-base",
        "shared schema changed on main",
        behind_count=3,
    )
    assert result.returncode == 1
    assert "behind base" in result.stdout
    assert "shared schema changed on main" in result.stdout
    assert "<-- BLOCKS" in result.stdout


def test_overlapping_changed_path_automatically_requires_current_base(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        behind_count=2,
        pr_changed_paths="friday/service.py\nfriday/tests/test_service.py",
        base_changed_paths="friday/service.py\nREADME.md",
    )
    assert result.returncode == 1
    assert "overlapping paths" in result.stdout
    assert "friday/service.py" in result.stdout


def test_pr_rename_overlaps_base_change_to_rename_source(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        behind_count=2,
        pr_changed_paths="new.py",
        pr_no_rename_paths="old.py\nnew.py",
        base_changed_paths="old.py",
    )
    assert result.returncode == 1
    assert "overlapping paths" in result.stdout
    assert "old.py" in result.stdout


def test_base_rename_overlaps_pr_change_to_rename_source(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        behind_count=2,
        pr_changed_paths="old.py",
        base_changed_paths="new.py",
        base_no_rename_paths="old.py\nnew.py",
    )
    assert result.returncode == 1
    assert "overlapping paths" in result.stdout
    assert "old.py" in result.stdout


def test_high_risk_changes_on_both_sides_automatically_require_current_base(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        behind_count=2,
        pr_changed_paths="api/openapi.yaml",
        base_changed_paths="db/migrations/20261006_add_index.sql",
    )
    assert result.returncode == 1
    assert "high-risk drift" in result.stdout
    assert "openapi.yaml" in result.stdout
    assert "20261006_add_index.sql" in result.stdout


def test_unrelated_drift_with_one_sided_high_risk_file_only_warns(tmp_path):
    result = run_preflight(
        tmp_path,
        "SUCCESS",
        behind_count=2,
        pr_changed_paths="friday/report_copy.py",
        base_changed_paths="package-lock.json",
    )
    assert result.returncode == 0
    assert "2 commit(s) — warning" in result.stdout


def test_conflicting_or_closed_pr_blocks(tmp_path):
    conflicting = run_preflight(tmp_path, "SUCCESS", mergeable="CONFLICTING")
    assert conflicting.returncode == 1
    assert "CONFLICTING" in conflicting.stdout

    closed = run_preflight(tmp_path, "SUCCESS", pr_state="CLOSED")
    assert closed.returncode == 1
    assert "only open PRs" in closed.stdout
    assert "GitHub has not established mergeability" not in closed.stdout


def test_tracked_dirty_blocks_but_untracked_only_warns(tmp_path):
    dirty = run_preflight(tmp_path, "SUCCESS", tracked_dirty=True)
    assert dirty.returncode == 1
    assert "tracked worktree" in dirty.stdout
    assert "dirty" in dirty.stdout

    untracked_result = run_preflight(tmp_path, "SUCCESS", untracked=True)
    assert untracked_result.returncode == 0
    assert "untracked files" in untracked_result.stdout
    assert "warning" in untracked_result.stdout


def test_ignored_preview_is_not_reported_as_deployed(tmp_path):
    result = run_preflight(
        tmp_path, "SUCCESS", preview_description="Canceled by Ignored Build Step"
    )
    assert result.returncode == 0
    assert "SKIPPED/UNAVAILABLE" in result.stdout


def test_skipped_or_cancelled_preview_check_run_is_unavailable(tmp_path):
    for conclusion in ("SKIPPED", "CANCELLED"):
        result = run_preflight(
            tmp_path, "SUCCESS", preview_conclusion=conclusion
        )
        assert result.returncode == 0
        assert "SKIPPED/UNAVAILABLE" in result.stdout














def test_explicit_repo_must_match_the_checkout_being_inspected(tmp_path):
    # 显式传了 owner/repo,但 cwd 是另一个仓库时,后面所有 git 检查
    # (behind base / local == PR head / worktree 是否干净)量的都是错的仓库。
    # 这次是往安全方向错(假阻塞);反过来——cwd 恰好干净且 SHA 碰巧相同——
    # 就是假通过。
    result = run_preflight(tmp_path, "SUCCESS", cwd_repo="example/other")

    assert result.returncode == 1, result.stdout
    assert "cwd is example/other" in result.stdout, result.stdout
    assert "<-- BLOCKS" in result.stdout, result.stdout


def test_matching_checkout_does_not_block(tmp_path):
    result = run_preflight(tmp_path, "SUCCESS", cwd_repo="example/project")

    assert "PREFLIGHT(generic) PASS" in result.stdout, result.stdout
    assert "cwd is" not in result.stdout, result.stdout


def test_unresolvable_checkout_blocks_instead_of_skipping_the_guard(tmp_path):
    # Codex P2 (#52 discussion_r4023877307):`|| true` 让解析失败变成空字符串,
    # 守卫就被静默跳过,而后面的 git 检查照样量着一个来历不明的 checkout ——
    # 如果它恰好干净且 SHA 相同,会一路假通过。无法判定必须阻塞,不能跳过。
    result = run_preflight(tmp_path, "SUCCESS", cwd_repo="")

    assert result.returncode == 1, result.stdout
    assert "cannot resolve" in result.stdout.lower(), result.stdout
    assert "<-- BLOCKS" in result.stdout, result.stdout




def test_does_not_switch_global_authentication(tmp_path):
    result = run_preflight(tmp_path, "SUCCESS")
    assert result.returncode == 0
    assert "auth switch" not in (tmp_path / "gh-calls.log").read_text()



def test_successful_fetch_of_wrong_base_cannot_pass(tmp_path):
    result = run_preflight(tmp_path, "SUCCESS", local_base_oid="c" * 40)
    assert result.returncode == 1
    assert "base snapshot" in result.stdout
    assert "differs from the target PR base" in result.stdout



@pytest.mark.parametrize("strict", [False, True])
def test_real_fork_base_cannot_masquerade_as_target_base(tmp_path, strict):
    # Only GitHub is a fixture here. All commits, remotes, fetches and drift
    # comparisons use real local Git repositories, with no network access.
    upstream, fork = tmp_path / "upstream.git", tmp_path / "fork.git"
    seed, checkout = tmp_path / "seed", tmp_path / "checkout"

    def git(cwd, *args):
        return subprocess.check_output([
            "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            "-C", str(cwd), *args,
        ], text=True, stderr=subprocess.PIPE).strip()

    for bare in (upstream, fork):
        git(tmp_path, "init", "--bare", "-q", str(bare))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "shared.txt").write_text("initial\n")
    git(seed, "add", "shared.txt")
    git(seed, "commit", "-qm", "initial base")
    git(seed, "push", str(upstream), "main")
    git(seed, "push", str(fork), "main")
    git(seed, "switch", "-c", "feature")
    (seed / "shared.txt").write_text("candidate\n")
    git(seed, "commit", "-qam", "candidate")
    candidate = git(seed, "rev-parse", "HEAD")
    git(seed, "push", str(upstream), "feature")
    git(seed, "switch", "main")
    (seed / "shared.txt").write_text("new target base\n")
    git(seed, "commit", "-qam", "overlapping target change")
    target_base = git(seed, "rev-parse", "HEAD")
    git(seed, "push", str(upstream), "main")
    git(tmp_path, "clone", "-q", "--branch", "main", str(fork), str(checkout))
    git(checkout, "fetch", str(upstream), "feature")
    git(checkout, "checkout", "--detach", "FETCH_HEAD")
    assert git(checkout, "rev-list", "--count", "HEAD..origin/main") == "0"

    payload = [{"data": {"repository": {"pullRequest": {
        "baseRefName": "main", "baseRefOid": target_base, "headRefOid": candidate,
        "isDraft": False, "state": "OPEN", "merged": False, "mergeable": "MERGEABLE",
        "reviewThreads": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {
            "state": "SUCCESS", "contexts": {"nodes": []},
        }}}]},
    }}}}]
    bin_dir = tmp_path / "github-fixture"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'repo view') echo example/upstream ;;\n"
        "  'api graphql') printf '%s\\n' \"$GH_GRAPHQL\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    arguments = ["--require-current-base", "target base is required"] if strict else []
    result = subprocess.run(
        [str(SCRIPT), *arguments, "1", "example/upstream"], cwd=checkout,
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}",
                          "GH_REPO": "example/upstream", "GH_GRAPHQL": json.dumps(payload)},
        text=True, capture_output=True,
    )
    assert result.returncode == 1, result.stdout
    assert "base snapshot" in result.stdout
    assert "PREFLIGHT(generic) PASS" not in result.stdout
