#!/usr/bin/env bash
# Rain-Outsource post-launch watch. Read-only. One run = one check; NOT scheduled.
# Checks prod /health, two odds endpoints (/v1/rain, /v1/status), and the deployed function count (<= 12,
# Vercel Hobby limit) via `vercel inspect` on the current production deployment.
# Never deploys, promotes or pulls env. The internal token is read from its file into a 0600 temp header file
# (curl -H @file), so it never appears in argv, output or the log, and these requests count as internal.
# Appends one line per run (Central time) to the log, plus one FAIL line per problem. Exit 0 = OK, 1 = FAIL.
set -u
export TZ=America/Chicago
BASE="${RAINOUT_WATCH_BASE:-https://rainout-agent-source.vercel.app}"
LOG="${RAINOUT_WATCH_LOG:-/workspace/shared/rain-outsource/prod-watch.log}"
TOKEN_FILE="${RAINOUT_INTERNAL_TOKEN_FILE:-$HOME/.config/rainout/internal_token}"
SCOPE="${RAINOUT_VERCEL_SCOPE:-bizwax-s-projects}"
MAX_FUNCS=12
UA="rainout-prod-watch/1.0"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
fails=()
HDR=$(mktemp); chmod 600 "$HDR"; trap 'rm -f "$HDR" "${BODY:-}"' EXIT
if [ -r "$TOKEN_FILE" ]; then
  printf 'X-Rainout-Internal: %s\n' "$(cat "$TOKEN_FILE")" > "$HDR"
else
  : > "$HDR"; fails+=("internal token file not readable ($TOKEN_FILE); requests sent without the internal header")
fi
BODY=$(mktemp)

get() {  # get <path> -> sets CODE, writes body to $BODY
  CODE=$(curl -sS -m 20 -A "$UA" -H @"$HDR" -o "$BODY" -w '%{http_code}' "$BASE$1" 2>/dev/null) || CODE=000
}

# 1. health
get /health
HEALTH=$CODE
if [ "$HEALTH" != 200 ]; then fails+=("/health returned $HEALTH")
elif ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("status")=="ok" else 1)' "$BODY" 2>/dev/null; then
  fails+=("/health body is not {\"status\": \"ok\"}"); fi

# 2. odds: rain chance for Austin
get "/v1/rain?q=Austin,%20TX"
RAIN=$CODE
if [ "$RAIN" != 200 ]; then fails+=("/v1/rain?q=Austin, TX returned $RAIN")
elif ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$BODY" 2>/dev/null; then fails+=("/v1/rain body is not JSON"); fi

# 3. odds: game status for a sample field, game 8 h from now
GT=$(python3 -c "from datetime import datetime,timedelta;import urllib.parse;from zoneinfo import ZoneInfo;print(urllib.parse.quote((datetime.now(ZoneInfo('America/Chicago'))+timedelta(hours=8)).replace(minute=0,second=0,microsecond=0).isoformat()))")
get "/v1/status?field_id=Krieg&game_time=$GT"
STATUS=$CODE
if [ "$STATUS" != 200 ]; then fails+=("/v1/status (Krieg) returned $STATUS")
elif ! python3 -c '
import json,sys; d=json.load(open(sys.argv[1]))
p=d.get("game_time_play_probability_percent")
sys.exit(0 if "official_status" in d and isinstance(p,(int,float)) and 0<=p<=100 else 1)' "$BODY" 2>/dev/null; then
  fails+=("/v1/status (Krieg) is missing official_status or a 0-100 play probability"); fi

# 4. deployed function count on the current production deployment (read-only inspect)
FUNCS="?"; DPL="?"
if command -v vercel >/dev/null 2>&1; then
  INFO=$(timeout 90 vercel inspect "$BASE" --scope "$SCOPE" --json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
n=sum(1 for b in d.get("builds",[]) for o in (b.get("output") or []) if str(o.get("type","")).lower()=="lambda")
print(n, d.get("id","?"), d.get("readyState","?"), d.get("target","?"))' 2>/dev/null)
  if [ -n "$INFO" ]; then
    read -r FUNCS DPL STATE TARGET <<< "$INFO"
    [ "$FUNCS" -le "$MAX_FUNCS" ] 2>/dev/null || fails+=("production deployment $DPL has $FUNCS functions (limit $MAX_FUNCS)")
    [ "$STATE" = READY ] || fails+=("production deployment $DPL state is $STATE")
    [ "$TARGET" = production ] || fails+=("$BASE points at a $TARGET deployment, not production")
  else
    fails+=("vercel inspect failed (CLI not logged in, or network)")
  fi
else
  fails+=("vercel CLI not installed; function count not checked")
fi

LINE="$(ts) health=$HEALTH rain=$RAIN status=$STATUS functions=$FUNCS deployment=$DPL"
if [ ${#fails[@]} -eq 0 ]; then
  echo "$LINE OK" | tee -a "$LOG"; exit 0
fi
echo "$LINE FAIL (${#fails[@]})" | tee -a "$LOG"
for f in "${fails[@]}"; do echo "$(ts) FAIL $f" | tee -a "$LOG"; done
exit 1
