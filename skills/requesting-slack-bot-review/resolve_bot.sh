#!/usr/bin/env bash
# Resolve a configured review bot to verified Slack/GitHub identities.
#   resolve_bot.sh <bot>                         -> Slack member ID
#   resolve_bot.sh --json <bot>                  -> canonical identity object
#   resolve_bot.sh --json --require-github <bot> -> fail unless both identities verify live
#   resolve_bot.sh --json --require-host <bot>   -> also verify its configured human host live
set -uo pipefail

HOST_OBSERVATION_MAX_AGE_DAYS=30

OUTPUT=text
REQUIRE_GITHUB=0
REQUIRE_HOST=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --json) OUTPUT=json; shift ;;
    --require-github) REQUIRE_GITHUB=1; shift ;;
    --require-host) REQUIRE_HOST=1; shift ;;
    --help|-h)
      echo "usage: resolve_bot.sh [--json] [--require-github] [--require-host] <bot-name>"
      exit 0
      ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done

if [ "$REQUIRE_HOST" -eq 1 ] && [ "$OUTPUT" != json ]; then
  echo "--require-host requires --json" >&2
  exit 2
fi

RAW_NAME=${1:?bot name required}
[ "$#" -eq 1 ] || { echo "expected exactly one bot name" >&2; exit 2; }
WANT=$(python3 -c '
import re,sys
w=re.sub(r"[-_\x27 ]", "", sys.argv[1].lower())
if not w: sys.exit("empty bot name after normalising")
print(w)
' "$RAW_NAME") || exit 2

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CAPABILITIES="${SLACK_REVIEW_CAPABILITIES:-$SCRIPT_DIR/references/reviewer-capabilities.json}"
CAP_ROW=$(python3 -c '
import json,re,sys
path,want=sys.argv[1:]
try: reviewers=json.load(open(path, encoding="utf-8")).get("reviewers", {})
except FileNotFoundError: reviewers={}
norm=lambda value: re.sub(r"[-_\x27 ]", "", value.lower())
for name,item in reviewers.items():
    if norm(name)==want:
        host=item.get("host") or {}
        print("\x1f".join((
            item.get("slack_user_id", ""),
            item.get("github_login", ""),
            host.get("slack_user_id", ""),
            host.get("role", ""),
            host.get("observed_at", ""),
        )))
        break
' "$CAPABILITIES" "$WANT")
IFS=$'\x1f' read -r CACHED GITHUB_LOGIN HOST_CACHED HOST_ROLE HOST_OBSERVED_AT <<< "$CAP_ROW"

TOKEN="${SLACK_TOKEN:-${SLACK_MCP_XOXP_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "set SLACK_TOKEN (or SLACK_MCP_XOXP_TOKEN) explicitly for live identity verification" >&2; exit 2; }

verify_slack() {
  curl -sS -H "Authorization: Bearer $TOKEN" "https://slack.com/api/users.info?user=$1" 2>/dev/null \
  | python3 -c '
import json,sys
kind=sys.argv[1]
try: d=json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    print("Slack users.info returned empty or invalid JSON", file=sys.stderr); sys.exit(7)
u=d.get("user") or {}
if d.get("ok") and not u.get("deleted"):
    if kind=="host" and (u.get("is_bot") or u.get("id", "").startswith("B")):
        print("configured host is a bot, not a human member", file=sys.stderr); sys.exit(8)
    print(u["id"])
' "$2"
}

RESOLVED=""
if [ -n "$CACHED" ]; then
  RESOLVED=$(verify_slack "$CACHED" reviewer)
  [ -n "$RESOLVED" ] || echo "cached id $CACHED for '$WANT' could not be verified; falling back to users.list" >&2
fi

if [ -z "$RESOLVED" ]; then
  RESOLVED=$(curl -sS -H "Authorization: Bearer $TOKEN" "https://slack.com/api/users.list?limit=1000" 2>/dev/null \
  | python3 -c '
import json,re,sys
want=sys.argv[1]
try: d=json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    print("Slack users.list returned empty or invalid JSON", file=sys.stderr); sys.exit(7)
if not d.get("ok"):
    print("users.list failed: "+str(d.get("error")), file=sys.stderr); sys.exit(3)
def norm(value): return re.sub(r"[-_\x27 ]", "", (value or "").lower())
for member in d["members"]:
    if member.get("deleted"): continue
    profile=member.get("profile") or {}
    if want in (norm(member.get("name")), norm(member.get("real_name")), norm(profile.get("display_name"))):
        print(member["id"]); sys.exit(0)
print("no live member matches "+repr(want), file=sys.stderr); sys.exit(4)
' "$WANT") || exit $?
fi

HOST_RESOLVED=""
if [ "$REQUIRE_HOST" -eq 1 ]; then
  [ -n "${HOST_CACHED:-}" ] || {
    echo "no verified host identity is recorded for '$RAW_NAME'" >&2
    exit 8
  }
  python3 -c '
import datetime as dt,sys
value,max_age=sys.argv[1:]
try:
    observed=dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
except ValueError:
    sys.exit("configured host observation timestamp is invalid")
if observed.utcoffset() is None:
    sys.exit("configured host observation timestamp has no offset")
age=dt.datetime.now(dt.timezone.utc)-observed.astimezone(dt.timezone.utc)
if age.total_seconds() < -300:
    sys.exit("configured host observation timestamp is in the future")
if age > dt.timedelta(days=int(max_age)):
    sys.exit("configured host relationship observation is stale")
' "$HOST_OBSERVED_AT" "$HOST_OBSERVATION_MAX_AGE_DAYS" || exit 8
  HOST_RESOLVED=$(verify_slack "$HOST_CACHED" host) || exit $?
  [ -n "$HOST_RESOLVED" ] || {
    echo "host '$HOST_CACHED' for '$RAW_NAME' could not be verified live" >&2
    exit 8
  }
  [ "$HOST_RESOLVED" != "$RESOLVED" ] || {
    echo "configured host must differ from the review bot" >&2
    exit 8
  }
fi

if [ "$REQUIRE_GITHUB" -eq 1 ] && [ -z "${GITHUB_LOGIN:-}" ]; then
  echo "no verified GitHub identity is recorded for '$RAW_NAME'" >&2
  exit 5
fi

if { [ "$OUTPUT" = json ] || [ "$REQUIRE_GITHUB" -eq 1 ]; } && [ -n "${GITHUB_LOGIN:-}" ]; then
  LIVE_GITHUB=$(gh api "users/$GITHUB_LOGIN" --jq .login 2>/dev/null) || {
    echo "GitHub identity '$GITHUB_LOGIN' could not be verified live" >&2
    exit 6
  }
  python3 -c '
import sys
if sys.argv[1].casefold()!=sys.argv[2].casefold():
    sys.exit("GitHub identity verification returned a different login")
' "$LIVE_GITHUB" "$GITHUB_LOGIN" || exit 6
  GITHUB_LOGIN=$LIVE_GITHUB
fi

if [ "$OUTPUT" = json ]; then
  python3 -c '
import json,sys
name,slack,github,host,host_role=sys.argv[1:]
payload={"reviewer": name, "slack_user_id": slack, "github_login": github or None}
if host:
    payload["host"]={"slack_user_id": host, "role": host_role}
print(json.dumps(payload, sort_keys=True))
' "$RAW_NAME" "$RESOLVED" "${GITHUB_LOGIN:-}" "$HOST_RESOLVED" "${HOST_ROLE:-}"
else
  echo "$RESOLVED"
fi
