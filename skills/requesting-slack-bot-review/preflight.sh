#!/usr/bin/env bash
# Optional repeat-review coverage layer over the same-pin generic gate.
#   usage: preflight.sh [--allow-covered <reason>] [--allow-no-checks <reason>] <pr-number> [owner/repo]
#
# WRAPS `pr-review-preflight`, never copies it: two gates drift, and the one
# that drifts is the copy. Everything here is the part that is only true for
# reviewers that use stamped issue comments or native GitHub review objects.
set -uo pipefail
# `--allow-covered <reason>`: the stamp check below cannot read a verdict, so a
# CLEAN re-review sitting on the current head blocks exactly like an unaddressed
# one. That conservatism is deliberate, but it must have a door — and the door
# is a flag with a stated reason, not an edit to this script. Editing the gate to
# get past it is how a gate becomes decoration.
ALLOW_COVERED=""
ALLOW_NO_CHECKS=""
while [ "${1:-}" = "--allow-covered" ] || [ "${1:-}" = "--allow-no-checks" ]; do
  case "$1" in
    --allow-covered) ALLOW_COVERED="${2:?--allow-covered needs a reason}" ;;
    --allow-no-checks) ALLOW_NO_CHECKS="${2:?--allow-no-checks needs a reason}" ;;
  esac
  shift 2
done
PR="${1:?pr number required}"
REPO="${2:-$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)}"
# An unresolved repo used to become the string "/" and surface as a confusing
# GraphQL "could not resolve Repository" error. Fail with the actual cause.
case "$REPO" in
  */?*) ;;
  *) echo "cannot determine owner/repo (gh repo view failed here?) — pass it as arg 2" >&2; exit 2 ;;
esac
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$SCRIPT_DIR/check_installation.py" --skill-dir "$SCRIPT_DIR" || exit 2
GENERIC="$SCRIPT_DIR/../pr-review-preflight/preflight.sh"
[ -x "$GENERIC" ] || { echo "missing same-pin sibling pr-review-preflight; install both skills together" >&2; exit 2; }

fail=0
generic_args=()
[ -n "$ALLOW_NO_CHECKS" ] && generic_args+=(--allow-no-checks "$ALLOW_NO_CHECKS")
generic_status=0
if [ "${#generic_args[@]}" -gt 0 ]; then
  "$GENERIC" "${generic_args[@]}" "$PR" "$REPO" || generic_status=$?
else
  "$GENERIC" "$PR" "$REPO" || generic_status=$?
fi
[ "$generic_status" = "0" ] || fail=1

note() { printf '%-28s %s\n' "$1" "$2"; }
bad()  { printf '%-28s %s  <-- BLOCKS\n' "$1" "$2"; fail=1; }

# Exit 1 means the generic gate ran and blocked — it printed its own reasons and
# its terminal PASS/FAIL line. Any other non-zero means it never finished, so its
# checks were NOT evaluated. Both fail closed, which is right, but they look the
# same in the output apart from some missing lines: a reader takes "the gate did
# not run" for "the gate said no". Observed 2026-09-16 when a concurrent editor
# left the sub-gate half-saved and bash reported a syntax error.
case "$generic_status" in
  0|1) ;;
  *) bad "generic gate" "did not run (exit $generic_status) — its checks were NOT evaluated" ;;
esac

# A review already covering THIS EXACT head.
#
# TWO ledgers, because the bots changed channel mid-flight and this check was
# briefly reading only the dead one.
#
# Originally they posted an issue comment stamped `## Review @ <sha>` and opened
# no review objects at all (verified 2026-08-20: reviews=[], threads=0 while a
# four-finding review sat on the PR) — which is why the generic
# unresolved-threads check is structurally blind to them, and why this stamp
# scanner exists.
#
# The upstream reviewers also submit real reviews (`POST /pulls/{n}/reviews`),
# after being asked to: an issue comment can never satisfy REVIEW_REQUIRED, so
# its verdicts could not clear a merge gate. Consequence for THIS check: a review
# object bound to the head was invisible here, so the gate reported "no coverage"
# with an APPROVAL sitting on that exact head and would have waved through a
# redundant re-request. Both ledgers are read below. Expect the bots to keep
# using either.
#
# Rule: a stamp equal to the current head means either its findings are still
# unaddressed, or nothing changed since — both make a new request wrong. It
# cannot block forever; any push clears it.
if ! HEAD_OID=$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null); then
  bad "review ledger" "cannot read PR head"
  HEAD_OID=""
fi
if ! BODIES=$(gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" --jq '.[].body' 2>/dev/null); then
  bad "review ledger" "cannot read issue comments — coverage unknown"
  BODIES=""
fi
# Ledger 2: review objects, minus MY OWN. Replying to a review thread creates a
# review object bound to the current head, so counting those would let my own
# reply mark the head "reviewed" and block every request. Third instance of the
# same trap in one session (a watcher read my sync message as the bot's review;
# a recap read as a delivery) — never let your own output count as a result.
if ! ME=$(gh api user --jq .login 2>/dev/null); then
  bad "review identity" "cannot resolve current GitHub user — cannot exclude self-authored evidence"
  ME=""
fi
if ! REVIEWS=$(gh api --paginate --slurp "repos/$REPO/pulls/$PR/reviews?per_page=100" 2>/dev/null); then
  bad "review ledger" "cannot read GitHub reviews — coverage unknown"
  REVIEWS='[[]]'
fi
if ! printf '%s' "$REVIEWS" | jq -e '
  type == "array" and
  all(.[]; type == "array") and
  all(add[]; ((.commit_id | type) == "string") and
             ((.user.login | type) == "string") and
             ((.state | type) == "string"))' >/dev/null 2>&1; then
  bad "review ledger" "unexpected GitHub review schema — coverage unknown"
  REVIEW_ROWS=""
elif ! REVIEW_ROWS=$(printf '%s' "$REVIEWS" | jq -r --arg me "${ME:-__none__}" \
  'add[] | select(.user.login != $me) | [.commit_id,.user.login,.state,(.html_url // "-")] | @tsv' 2>/dev/null); then
  bad "review ledger" "cannot parse GitHub reviews — coverage unknown"
  REVIEW_ROWS=""
fi
REVIEW_SHAS=$(printf '%s\n' "$REVIEW_ROWS" | cut -f1)
# Two lessons are baked into these two lines; do not simplify either away.
#
# 1. `Re?-?review` looked right and silently matched "Re-review" while missing
#    "Review" — a silent miss inside the check written to close a silent miss.
#
# 2. These bots post their newlines DOUBLE-ESCAPED: the body carries literal
#    two-character `\n` sequences, not 0x0A (verified 2026-08-20: 1884 bytes,
#    one real newline, fifteen literal `\n`). So `^` anchors almost nowhere, and
#    a stamp that is not at byte 0 is invisible to an anchored pattern. Accept a
#    literal `\n` as a line start too.
STAMPED=$(printf '%s' "$BODIES" \
  | grep -oEi '(^|\\n)#+ *(re-)?review @ *[0-9a-f]{7,40}' | grep -oE '[0-9a-f]{7,40}' || true)
COUNTED=$(printf '%s' "$STAMPED" | grep -c . || true)
# The sentinel must NOT share the extractor's pattern. An earlier version reused
# it, so when the extractor went blind the sentinel went blind with it and
# reported agreement — a checker verified by its own parser is not verified.
# This one is deliberately looser and independent: any "review @" at all.
ANNOUNCED=$(printf '%s' "$BODIES" | grep -oiE 'review @ *[0-9a-f]{7,40}' | grep -c . || true)
[ "$ANNOUNCED" -le "$COUNTED" ] || \
  bad "review-stamp parse" "$ANNOUNCED loose vs $COUNTED parsed — format changed, fix the pattern"

covering=""
for sha in $STAMPED $REVIEW_SHAS; do
  case "$HEAD_OID" in "$sha"*) covering="$sha" ;; esac
  case "$sha" in "${HEAD_OID:0:${#sha}}") covering="$sha" ;; esac
done
if [ -z "$covering" ]; then
  note "review covers this head" "no ($COUNTED stamped, $(printf '%s' "$REVIEW_SHAS" | grep -c . || true) review object(s), head ${HEAD_OID:0:9})"
elif [ -n "$ALLOW_COVERED" ]; then
  note "review covers this head" "${covering:0:9} — OVERRIDDEN: $ALLOW_COVERED"
else
  bad "review covers this head" "${covering:0:9} — address it and push, or pass --allow-covered '<reason>'"
fi
while IFS=$'\t' read -r sha reviewer state url; do
  [ -n "$sha" ] || continue
  if [[ "$HEAD_OID" == "$sha"* || "$sha" == "${HEAD_OID:0:${#sha}}" ]]; then
    note "  covering review" "$reviewer $state ${sha:0:9} $url"
  fi
done <<<"$REVIEW_ROWS"

echo
[ "$fail" = "0" ] && echo "PREFLIGHT PASS — ok to request review" \
  || echo "PREFLIGHT FAIL — fix the above before posting to Slack"
exit "$fail"
