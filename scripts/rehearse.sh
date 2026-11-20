#!/bin/sh
# Run an unpack-order rehearsal against a running repository.
#
# Usage:
#   ./scripts/rehearse.sh <release> <installed.json> <pkg.deb> <pkg.deb> [pkg.deb ...]
#
# <installed.json> is a JSON array of
#   {"name": "...", "version": "...", "files": ["/abs/path", ...]}
# (use [] for an empty machine).  The .deb arguments are listed in the order
# they would be unpacked; their SHA-256 values are computed here.  Pass 2 to 8
# packages, at most one per package name.
#
# Requires: python3, curl.  Set REPO_URL to override http://127.0.0.1:8080.
# Exit status: 0 when every step passes, 1 on a conflict or a rejected
# rehearsal (so the script can gate a rollout).
set -eu

REPO_URL="${REPO_URL:-http://127.0.0.1:8080}"
if [ "$#" -lt 3 ]; then
    sed -n '2,14p' "$0" >&2
    exit 2
fi
RELEASE="$1"; shift
INSTALLED="$1"; shift

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

python3 - "$RELEASE" "$INSTALLED" "$WORK/request.json" "$@" <<'PY'
import hashlib, json, sys

release, installed_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
debs = sys.argv[4:]
digests = []
for path in debs:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    digests.append(digest.hexdigest())
with open(installed_path, encoding="utf-8") as handle:
    installed = json.load(handle)
body = {"release": int(release), "packages": digests, "installed": installed}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(body, handle)
PY

STATUS=$(curl -sS -o "$WORK/response.json" -w '%{http_code}' \
    -X POST "$REPO_URL/api/rehearse" \
    -H 'content-type: application/json' --data-binary @"$WORK/request.json")
echo "HTTP $STATUS"
python3 -m json.tool "$WORK/response.json"

if [ "$STATUS" != "200" ]; then
    exit 1
fi
python3 -c 'import json,sys; sys.exit(1 if json.load(open(sys.argv[1]))["result"] == "conflict" else 0)' \
    "$WORK/response.json"
