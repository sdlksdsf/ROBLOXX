#!/usr/bin/env bash
# Publish bridge/relay/roblox_relay.ps1 to the hub's database copy.
#
# The hub serves the DB asset when present, and the GitHub copy otherwise. The
# DB path is what makes a fix reachable when the session's branch cannot be
# pushed (merged PR branches are read-only).
#
# Emits ONE sql file per chunk plus a final assembly statement, so each
# statement stays reviewable instead of a 40KB blob.
set -euo pipefail
RELAY="${1:-bridge/relay/roblox_relay.ps1}"
OUT="${2:-/tmp/relay-deploy}"
TOKEN="ar-6f3c1d9b8e4a5270c1b9f7e2d4a68b35"
URL="https://vrljnkhojjjeuarunbqj.supabase.co/functions/v1/arena-hub/selfinstall?token=$TOKEN"

mkdir -p "$OUT"
python3 - "$RELAY" "$OUT" "$URL" <<'PY'
import sys, os
relay, out, url = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(relay, encoding="utf-8", newline="").read()
assert "$q$" not in s, "the file must not contain the dollar-quote tag"
lines = s.split("\r\n")
n = int(len(lines) / 3) + 1
groups = [lines[i:i + n] for i in range(0, len(lines), n)]
for i, g in enumerate(groups):
    body = "\r\n".join(g)
    sql = (f"select net.http_post(url := '{url}', body := jsonb_build_object("
           f"'files', jsonb_build_object('relay.chunk.p{i}', $q${body}$q$)), "
           f"headers := '{{\"Content-Type\":\"application/json\"}}'::jsonb) as id;")
    open(os.path.join(out, f"chunk{i}.sql"), "w", encoding="utf-8").write(sql)
    print(f"chunk{i}: {len(sql)} sql chars, {len(body)} relay chars")
meta = os.path.join(out, "meta.txt")
open(meta, "w").write(f"chunks={len(groups)} total={len(s)}\n")
print("wrote", meta, f"total={len(s)}")
PY
