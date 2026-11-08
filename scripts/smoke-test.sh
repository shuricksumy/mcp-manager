#!/bin/sh
# Smoke test for a built image: start it with one stdio server (mcp-server-fetch),
# then check over HTTP that
#   1. a request with an allowed, non-loopback Host header gets an MCP
#      `initialize` result (entrypoint -> --allowed-host plumbing works), and
#   2. a request with an unknown Host header is still rejected with HTTP 421
#      (DNS-rebinding protection is still on).
#
# Usage: scripts/smoke-test.sh <image> [host-port]
set -eu

image="$1"
port="${2:-18080}"
name="mcp-smoke-$$"
cfg="$(mktemp -d)"

cleanup() {
    docker logs "$name" > "$cfg/container.log" 2>&1 || true
    docker rm -f "$name" > /dev/null 2>&1 || true
    if [ "${failed:-1}" = 1 ]; then
        echo "--- container log ---"
        cat "$cfg/container.log" 2>/dev/null || true
    fi
    rm -rf "$cfg"
}
trap cleanup EXIT

cat > "$cfg/servers.json" <<'EOF'
{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}
EOF
chmod -R a+rX "$cfg"

docker run -d --name "$name" -p "127.0.0.1:$port:8000" \
    -e MCP_PROXY_ALLOWED_HOSTS='mcp-smoke:*' \
    -v "$cfg:/mcp:ro" "$image" > /dev/null

init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'

post() {
    curl -s -m 20 -o "$cfg/body" -w '%{http_code}' -X POST \
        "http://127.0.0.1:$port/servers/fetch/mcp" \
        -H "Host: $1" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d "$init" || true
}

# uvx downloads mcp-server-fetch on first start, so allow up to ~2 minutes.
code=000
i=0
while [ $i -lt 60 ]; do
    code="$(post 'mcp-smoke:8000')"
    if [ "$code" = 200 ] && grep -q '"serverInfo"' "$cfg/body"; then
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ "$code" != 200 ] || ! grep -q '"serverInfo"' "$cfg/body"; then
    echo "FAIL: initialize with allowed Host returned HTTP $code"
    cat "$cfg/body" 2>/dev/null || true
    exit 1
fi
echo "ok: initialize via allowed Host -> $(grep -o '"serverInfo":{[^}]*}' "$cfg/body")"

code="$(post 'evil.example:8000')"
if [ "$code" != 421 ]; then
    echo "FAIL: unknown Host returned HTTP $code, expected 421"
    exit 1
fi
echo "ok: unknown Host rejected with 421"

docker logs "$name" 2>&1 | grep '^entrypoint:' || true
failed=0
