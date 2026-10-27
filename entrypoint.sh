#!/bin/sh
# Container entrypoint. Starts as root only long enough to read a token file that
# may be root-owned (mode 0600), then drops to uid/gid 10001 for good.
set -eu

# 1. Not root (e.g. `docker run --user …`): run unchanged. The server reads
#    MCP_TOKENS_FILE itself, so the file must be readable by that user.
if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

# 2. Root with a token file: read it into MCP_TOKENS. The server's parser does the
#    full validation (empty, malformed, duplicates, CHANGE-ME), so the rules live in
#    one place.
if [ -n "${MCP_TOKENS_FILE+x}" ]; then
    if [ -n "${MCP_TOKENS+x}" ]; then
        echo "usesend-mcp: refusing to start: both MCP_TOKENS and MCP_TOKENS_FILE are set; set exactly one" >&2
        exit 1
    fi
    if [ ! -f "$MCP_TOKENS_FILE" ] || [ ! -r "$MCP_TOKENS_FILE" ]; then
        echo "usesend-mcp: refusing to start: MCP_TOKENS_FILE $MCP_TOKENS_FILE is not a readable regular file" >&2
        exit 1
    fi
    MCP_TOKENS="$(cat "$MCP_TOKENS_FILE")"
    export MCP_TOKENS
    unset MCP_TOKENS_FILE
fi

# 3. Drop privileges for good.
exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
