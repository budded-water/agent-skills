#!/usr/bin/env bash
# Generic PR-review readiness gate. Repo-agnostic, reviewer-agnostic.
#   usage: preflight.sh [--require-current-base <reason>] [--allow-no-checks <reason>] <pr-number> [owner/repo]
# Exit 0 only when the PR is worth a reviewer's time.
set -uo pipefail
ALLOW_NO_CHECKS=""
REQUIRE_CURRENT_BASE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-no-checks)
      ALLOW_NO_CHECKS="${2:?--allow-no-checks needs a reason}"; shift 2 ;;
    --require-current-base)
      REQUIRE_CURRENT_BASE="${2:?--require-current-base needs a reason}"; shift 2 ;;
    *) break ;;
  esac
done
PR="${1:?pr number required}"
REPO="${2:-$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)}"
# An unresolved repo used to become the string "/" and surface as a confusing
# GraphQL "could not resolve Repository" error. Fail with the actual cause.
case "$REPO" in
  */?*) ;;
  *) echo "cannot determine owner/repo (gh repo view failed here?) — pass it as arg 2" >&2; exit 2 ;;
esac
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"
# Use the caller-selected gh credential. Never switch the global account.
fail=0
note() { printf '%-28s %s\n' "$1" "$2"; }
bad()  { printf '%-28s %s  <-- BLOCKS\n' "$1" "$2"; fail=1; }

# The git checks below — base freshness, local == PR head, worktree cleanliness
# — read whichever repository the current directory belongs to. When owner/repo
# is passed explicitly that can be a DIFFERENT repository than the one under
# review, and then every one of those answers describes the wrong tree. Erring
# safe is not guaranteed: a clean unrelated checkout that happens to sit on a
# matching SHA would pass all three.
if [ -n "${2:-}" ]; then
  CWD_REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
  if [ -z "$CWD_REPO" ]; then
    # Skipping on an unresolved identity would be a checker that silently opts
    # out exactly when it cannot tell — and the git checks below would still
    # describe an unknown tree, which passes if it happens to share the SHA.
    bad "checkout" "cannot resolve the repository at $PWD — cannot tell whether the git checks below describe $REPO"
  elif [ "$CWD_REPO" != "$REPO" ]; then
    bad "checkout" "cwd is $CWD_REPO, target is $REPO — run this from the target's clone"
  fi
fi

q=$(gh api graphql --paginate --slurp -f query='
query($owner:String!,$repo:String!,$num:Int!,$endCursor:String){repository(owner:$owner,name:$repo){pullRequest(number:$num){
  baseRefName headRefOid mergeable isDraft state merged
  reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{
    isResolved isOutdated comments(first:1){nodes{author{login} path url body}}
  }}
  commits(last:1){nodes{commit{statusCheckRollup{state contexts(first:100){nodes{
    __typename
    ... on CheckRun{name status conclusion detailsUrl}
    ... on StatusContext{context state description targetUrl}
  }}}}}}}}}' \
  -F owner="$OWNER" -F repo="$NAME" -F num="$PR") || { echo "graphql failed"; exit 2; }
jq_() { jq -r "$1" <<<"$q"; }

BASE=$(jq_ '.[0].data.repository.pullRequest.baseRefName')
CI=$(jq_ '.[0].data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.state // "NONE"')
HEAD_OID=$(jq_ '.[0].data.repository.pullRequest.headRefOid')
DRAFT=$(jq_ '.[0].data.repository.pullRequest.isDraft')
PR_STATE=$(jq_ '.[0].data.repository.pullRequest.state')
MERGED=$(jq_ '.[0].data.repository.pullRequest.merged')
MERGEABLE=$(jq_ '.[0].data.repository.pullRequest.mergeable')
UNRESOLVED=$(jq_ '[.[].data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved==false and .isOutdated==false)] | length')
UNRESOLVED_ROWS=$(jq -r '.[].data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved==false and .isOutdated==false)
  | (.comments.nodes[0] // {}) as $comment
  | [($comment.author.login // "unknown"), ($comment.path // "-"),
     ($comment.url // "-"), (($comment.body // "") | split("\n")[0] | .[0:120])]
  | @tsv' <<<"$q")
CHECKS=$(jq -c '.[0].data.repository.pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes // []' <<<"$q")

note "base" "$BASE"
[ "$PR_STATE" = "OPEN" ] && [ "$MERGED" = "false" ] \
  || bad "PR state" "$PR_STATE (merged=$MERGED) — only open PRs can enter review"
[ "$DRAFT" = "false" ] || bad "draft" "yes — publish before asking"
if [ "$PR_STATE" = "OPEN" ] && [ "$MERGED" = "false" ]; then
  case "$MERGEABLE" in
    MERGEABLE) note "mergeable" "yes" ;;
    CONFLICTING) bad "mergeable" "CONFLICTING — resolve merge conflicts first" ;;
    *) bad "mergeable" "$MERGEABLE — GitHub has not established mergeability; retry" ;;
  esac
fi

# 1. base drift. GitHub mergeability is the primary conflict signal. Local git
#    adds semantic risk evidence: exact path overlap and high-risk files changed
#    on both sides automatically require the current base. Callers can also opt
#    into strict mode for dependencies that path analysis cannot see.
#
#    Measured against the PR's OWN head, not whatever is checked out: with the
#    wrong branch in the tree this used to print a number for an unrelated
#    branch, which reads like a real answer. Check 2 blocks either way, but a
#    misleading line next to it is how a gate teaches people to skim it.
HIGH_RISK_PATH='(^|/)(migrations?|schemas?|generated|api-contracts?|contracts?)(/|$)|(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|[^/]*\.lock|openapi[^/]*\.(ya?ml|json)|[^/]*\.proto)$|\.generated\.'
summarize_paths() {
  sed '/^$/d' | head -5 | paste -sd ', ' -
}
drift_analysis_unavailable() {
  if [ -n "$REQUIRE_CURRENT_BASE" ]; then
    bad "drift analysis" "$1 — current base required: $REQUIRE_CURRENT_BASE"
  else
    note "drift analysis" "$1 — warning; GitHub still reports MERGEABLE"
  fi
}

FETCH_OK=1
git fetch origin -q 2>/dev/null || FETCH_OK=0
if [ "$FETCH_OK" = "1" ] && git cat-file -e "$HEAD_OID" 2>/dev/null; then
  BEHIND=$(git rev-list --count "$HEAD_OID..origin/$BASE" 2>/dev/null || echo "?")
  if [ "$BEHIND" = "0" ]; then
    note "behind base" "0"
  elif [ "$BEHIND" = "?" ]; then
    drift_analysis_unavailable "cannot measure against origin/$BASE"
  else
    MERGE_BASE=$(git merge-base "$HEAD_OID" "origin/$BASE" 2>/dev/null || echo "")
    PR_PATHS=""; BASE_PATHS=""; PATHS_OK=1
    [ -n "$MERGE_BASE" ] || PATHS_OK=0
    if [ "$PATHS_OK" = "1" ]; then
      # Disable rename folding so both the source deletion and destination add
      # participate in overlap checks. Otherwise PR old.py -> new.py and a base
      # edit to old.py look unrelated even though they share the source path.
      PR_PATHS=$(git diff --no-renames --name-only "$MERGE_BASE" "$HEAD_OID" 2>/dev/null) || PATHS_OK=0
      BASE_PATHS=$(git diff --no-renames --name-only "$MERGE_BASE" "origin/$BASE" 2>/dev/null) || PATHS_OK=0
    fi
    if [ "$PATHS_OK" != "1" ]; then
      drift_analysis_unavailable "cannot compare changed paths"
    else
      OVERLAP=$(comm -12 \
        <(printf '%s\n' "$PR_PATHS" | sed '/^$/d' | sort -u) \
        <(printf '%s\n' "$BASE_PATHS" | sed '/^$/d' | sort -u))
      PR_HIGH_RISK=$(printf '%s\n' "$PR_PATHS" | grep -E "$HIGH_RISK_PATH" || true)
      BASE_HIGH_RISK=$(printf '%s\n' "$BASE_PATHS" | grep -E "$HIGH_RISK_PATH" || true)
      if [ -n "$OVERLAP" ]; then
        bad "overlapping paths" "$(printf '%s\n' "$OVERLAP" | summarize_paths) — update from origin/$BASE"
      elif [ -n "$PR_HIGH_RISK" ] && [ -n "$BASE_HIGH_RISK" ]; then
        bad "high-risk drift" "PR: $(printf '%s\n' "$PR_HIGH_RISK" | summarize_paths); base: $(printf '%s\n' "$BASE_HIGH_RISK" | summarize_paths)"
      elif [ -n "$REQUIRE_CURRENT_BASE" ]; then
        bad "behind base" "$BEHIND commit(s) — current base required: $REQUIRE_CURRENT_BASE"
      else
        note "behind base" "$BEHIND commit(s) — warning; no overlapping or two-sided high-risk paths detected"
      fi
    fi
  fi
elif [ "$FETCH_OK" = "1" ]; then
  drift_analysis_unavailable "PR head ${HEAD_OID:0:9} is not available locally"
else
  drift_analysis_unavailable "git fetch origin failed"
fi

# 2. pushed? The reviewer reads the remote, not your disk. This also catches
#    "wrong branch checked out", which would make check 1 measure nothing.
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
[ "$LOCAL" = "$HEAD_OID" ] && note "local == PR head" "yes" \
  || bad "local == PR head" "no (local ${LOCAL:0:9}, PR ${HEAD_OID:0:9}) — push, or check out this PR's branch"

# A matching commit does not prove that the candidate on disk is what will be
# reviewed. Tracked/staged edits are invisible to the remote reviewer and block.
# Untracked files may be unrelated, so surface them as a warning for explicit
# disposition rather than silently treating them as candidate content.
TRACKED_DIRTY=$(git status --porcelain --untracked-files=no 2>/dev/null || echo "__status_failed__")
if [ "$TRACKED_DIRTY" = "__status_failed__" ]; then
  bad "tracked worktree" "cannot inspect"
elif [ -n "$TRACKED_DIRTY" ]; then
  bad "tracked worktree" "dirty — commit/revert candidate changes before review"
else
  note "tracked worktree" "clean"
fi
UNTRACKED_COUNT=$(git status --porcelain --untracked-files=normal 2>/dev/null | grep -c '^?? ' || true)
[ "$UNTRACKED_COUNT" = "0" ] || note "untracked files" "$UNTRACKED_COUNT warning — confirm they are unrelated to the PR"

# 3. CI. PENDING is not a pass: you would be asking over a running build.
case "$CI" in
  SUCCESS) note "CI" "SUCCESS" ;;
  EXPECTED|PENDING) bad "CI" "$CI — still running, wait" ;;
  NONE)
    if [ -n "$ALLOW_NO_CHECKS" ]; then
      note "CI" "no checks reported — OVERRIDDEN: $ALLOW_NO_CHECKS"
    else
      bad "CI" "no checks reported — configure CI, or pass --allow-no-checks '<reason>'"
    fi
    ;;
  *) bad "CI" "$CI" ;;
esac

# CI success and a usable preview are separate ledgers. Vercel may report an
# ignored build as success; that is not evidence that visual review was possible.
PREVIEW_COUNT=$(jq '[.[] | select(((.name // .context // "") | test("vercel|preview"; "i")))] | length' <<<"$CHECKS")
PREVIEW_SKIPPED=$(jq '[.[] | select(
  ((.name // .context // "") | test("vercel|preview"; "i")) and
  (((.description // "") | test("ignored build|skipped|cancel(?:ed|led)"; "i")) or
   ((.conclusion // "") | test("skipped|cancel(?:ed|led)"; "i")))
)] | length' <<<"$CHECKS")
if [ "$PREVIEW_SKIPPED" -gt 0 ]; then
  note "preview deployment" "SKIPPED/UNAVAILABLE — do not claim preview validation"
elif [ "$PREVIEW_COUNT" -gt 0 ]; then
  note "preview deployment" "reported by checks — verify URL before claiming validation"
else
  note "preview deployment" "not reported"
fi

# 4. unresolved review threads. Issue comments (bot quota notices, status notes)
#    are NOT threads: they can never be resolved, so gating on them would block
#    forever. Resolution state exists only here. Outdated threads are skipped —
#    the code they point at is gone.
[ "$UNRESOLVED" = "0" ] && note "unresolved threads" "0" \
  || bad "unresolved threads" "$UNRESOLVED — resolve or answer them first"
if [ "$UNRESOLVED" != "0" ]; then
  while IFS=$'\t' read -r reviewer path url title; do
    [ -n "$reviewer" ] || continue
    note "  unresolved" "$reviewer $path $url ${title:-review finding}"
  done <<<"$UNRESOLVED_ROWS"
fi

echo
[ "$fail" = "0" ] && echo "PREFLIGHT(generic) PASS" || echo "PREFLIGHT(generic) FAIL"
exit "$fail"
