#!/bin/sh
# Entrypoint test, run in CI against the built image (needs Docker and sudo).
#   tests/entrypoint_test.sh <image>
#
# 1. As root with a root:root 0600 token file bind-mounted read-only: the container
#    starts, /healthz answers 200, the server runs as uid 10001, and the token is
#    not in the container config.
# 2. MCP_TOKENS and MCP_TOKENS_FILE both set: exit 1.
# 3. MCP_TOKENS_FILE missing: exit 1.
# 4. Not root (--user 10001): the entrypoint passes through; the server itself
#    refuses an unreadable file with exit 1.
set -eu

IMAGE="$1"
NET="mcp-entrypoint-test-$$"
PG="mcp-pg-$$"
APP="mcp-app-$$"
WORK="$(mktemp -d)"
FAILED=0

cleanup() {
    docker rm -f "$APP" "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    sudo rm -rf "$WORK"
}
trap cleanup EXIT

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILED=1; }

TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
printf 'alice:%s\n' "$TOKEN" >"$WORK/tokens"
sudo chown root:root "$WORK/tokens"
sudo chmod 0600 "$WORK/tokens"

docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_PASSWORD=test postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do
    if docker exec "$PG" pg_isready -U postgres >/dev/null 2>&1; then break; fi
    sleep 1
done

COMMON="--network $NET -e MCP_USESEND_API_KEY=us_test -e USESEND_BASE_URL=http://usesend.invalid:3000
 -e MCP_DATABASE_URL=postgresql://postgres:test@$PG/postgres -e MCP_ALLOWED_FROM_DOMAINS=example.com"

# ─── 1. root + root:root 0600 file ──────────────────────────────────────────
# shellcheck disable=SC2086
docker run -d --name "$APP" $COMMON -p 127.0.0.1::8080 \
    -v "$WORK/tokens:/run/secrets/mcp_tokens:ro" -e MCP_TOKENS_FILE=/run/secrets/mcp_tokens \
    "$IMAGE" >/dev/null
PORT="$(docker port "$APP" 8080/tcp | head -n1 | sed 's/.*://')"
STATUS=""
for _ in $(seq 1 60); do
    STATUS="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)"
    [ "$STATUS" = "200" ] && break
    sleep 1
done
if [ "$STATUS" = "200" ]; then pass "root:root 0600 token file: /healthz 200"; else
    fail "root:root 0600 token file: /healthz $STATUS"
    docker logs "$APP" 2>&1 | tail -n 20
fi

UIDS="$(docker top "$APP" -eo uid,cmd | tail -n +2)"
echo "$UIDS"
if echo "$UIDS" | grep -q 'usesend_mcp.server' && ! echo "$UIDS" | grep -v '^ *10001 ' | grep -q .; then
    pass "server runs as uid 10001"
else
    fail "server is not (only) uid 10001"
fi

STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/" || true)"
if [ "$STATUS" = "401" ]; then pass "POST / without token: 401"; else fail "POST / without token: $STATUS"; fi

if docker inspect "$APP" | grep -q "$TOKEN"; then fail "token visible in docker inspect"; else
    pass "token not in docker inspect"
fi
docker rm -f "$APP" >/dev/null

# ─── 2. both variables set ──────────────────────────────────────────────────
RC=0
# shellcheck disable=SC2086
OUT="$(docker run --rm $COMMON -v "$WORK/tokens:/run/secrets/mcp_tokens:ro" \
    -e MCP_TOKENS_FILE=/run/secrets/mcp_tokens -e "MCP_TOKENS=bob:$TOKEN" "$IMAGE" 2>&1)" || RC=$?
if [ "$RC" = "1" ] && echo "$OUT" | grep -q "both MCP_TOKENS and MCP_TOKENS_FILE"; then
    pass "both set: exit 1"
else
    fail "both set: exit $RC: $OUT"
fi

# ─── 3. file missing ────────────────────────────────────────────────────────
RC=0
# shellcheck disable=SC2086
OUT="$(docker run --rm $COMMON -e MCP_TOKENS_FILE=/run/secrets/missing "$IMAGE" 2>&1)" || RC=$?
if [ "$RC" = "1" ] && echo "$OUT" | grep -q "/run/secrets/missing"; then pass "missing file: exit 1"; else
    fail "missing file: exit $RC: $OUT"
fi

# ─── 4. not root: pass-through ──────────────────────────────────────────────
RC=0
# shellcheck disable=SC2086
OUT="$(docker run --rm --user 10001 $COMMON -v "$WORK/tokens:/run/secrets/mcp_tokens:ro" \
    -e MCP_TOKENS_FILE=/run/secrets/mcp_tokens "$IMAGE" 2>&1)" || RC=$?
if [ "$RC" = "1" ] && echo "$OUT" | grep -q "can't read MCP_TOKENS_FILE"; then
    pass "--user 10001 with a root-only file: the server refuses, exit 1"
else
    fail "--user 10001: exit $RC: $OUT"
fi

if echo "$OUT" | grep -q "$TOKEN"; then fail "token printed"; fi
exit "$FAILED"
