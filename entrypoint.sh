#!/bin/sh
# Translate MCP_PROXY_* environment variables into mcp-proxy CLI flags.
# mcp-proxy itself does not read its options from the environment.
set -e

# Preserve args passed via compose `command:` before `set --` rebuilds "$@".
# Each arg is single-quoted so it survives a later `eval set --`.
quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}
user_args=""
for a in "$@"; do
    user_args="$user_args $(quote "$a")"
done

is_true() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

trim() {
    printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

MCP_PROXY_HOST="${MCP_PROXY_HOST:-0.0.0.0}"
MCP_PROXY_PORT="${MCP_PROXY_PORT:-8000}"
MCP_PROXY_TRANSPORT="${MCP_PROXY_TRANSPORT:-streamablehttp}"
MCP_PROXY_CONFIG="${MCP_PROXY_CONFIG:-/mcp/servers.json}"
MCP_PROXY_ALLOWED_HOSTS="${MCP_PROXY_ALLOWED_HOSTS:-}"
MCP_PROXY_ALLOW_ORIGINS="${MCP_PROXY_ALLOW_ORIGINS-*}"
MCP_PROXY_PASS_ENV="${MCP_PROXY_PASS_ENV:-false}"
MCP_PROXY_LOG_LEVEL="${MCP_PROXY_LOG_LEVEL:-}"
MCP_PROXY_DEBUG="${MCP_PROXY_DEBUG:-false}"
MCP_PROXY_EXTRA_ARGS="${MCP_PROXY_EXTRA_ARGS:-}"

# Per-server supervision (mcp-supervise): wrap the servers that opted in (all
# of them with MCP_SUPERVISE=true, or those with a "supervise" key) into a
# generated copy of the config. The original file is never modified.
export MCP_SUPERVISE_DIR="${MCP_SUPERVISE_DIR:-/run/mcp-supervise}"  # control sockets
if [ -f "$MCP_PROXY_CONFIG" ] && command -v mcp-supervise > /dev/null 2>&1; then
    supervised=/tmp/mcp-proxy-servers.supervised.json
    rc=0
    mcp-supervise wrap-config "$MCP_PROXY_CONFIG" "$supervised" || rc=$?
    case "$rc" in
        0) MCP_PROXY_CONFIG="$supervised" ;;
        2) ;; # nothing to supervise: use the config as is
        *) exit 1 ;;
    esac
fi

set -- mcp-proxy \
    --host "$MCP_PROXY_HOST" \
    --port "$MCP_PROXY_PORT" \
    --transport "$MCP_PROXY_TRANSPORT" \
    --named-server-config "$MCP_PROXY_CONFIG"

# Comma-separated list -> one "--flag=value" per non-empty, trimmed item.
# The "=" form matters for --allow-origin, which is nargs="+" and would
# otherwise swallow following arguments.
add_list() {
    flag="$1"
    list="$2"
    old_ifs="$IFS"
    IFS=','
    set -f
    # shellcheck disable=SC2086  # intentional word splitting on commas
    set -- $list
    set +f
    IFS="$old_ifs"
    for item in "$@"; do
        item="$(trim "$item")"
        [ -n "$item" ] && list_out="$list_out $(quote "$flag=$item")"
    done
    return 0
}

list_out=""
add_list --allowed-host "$MCP_PROXY_ALLOWED_HOSTS"
add_list --allow-origin "$MCP_PROXY_ALLOW_ORIGINS"
eval "set -- \"\$@\" $list_out"

if is_true "$MCP_PROXY_PASS_ENV"; then
    set -- "$@" --pass-environment
fi
if [ -n "$MCP_PROXY_LOG_LEVEL" ]; then
    set -- "$@" --log-level "$MCP_PROXY_LOG_LEVEL"
fi
if is_true "$MCP_PROXY_DEBUG"; then
    set -- "$@" --debug
fi

# Raw extra flags: split on whitespace (no quoting support - use compose
# `command:` for arguments containing spaces).
if [ -n "$MCP_PROXY_EXTRA_ARGS" ]; then
    set -f
    # shellcheck disable=SC2086  # intentional word splitting
    set -- "$@" $MCP_PROXY_EXTRA_ARGS
    set +f
fi

eval "set -- \"\$@\" $user_args"

if [ ! -f "$MCP_PROXY_CONFIG" ]; then
    echo "entrypoint: WARNING: named server config '$MCP_PROXY_CONFIG' not found" >&2
fi

# Versions recorded at build time by the Dockerfile smoke test.
if [ -f /etc/mcp-proxy.versions ]; then
    echo "entrypoint: $(cat /etc/mcp-proxy.versions)"
fi

# Log the command. Only flags are printed, never environment values, so
# secrets passed via env (API_ACCESS_TOKEN, server tokens) are not logged.
printf 'entrypoint: exec'
for a in "$@"; do printf ' %s' "$(quote "$a")"; done
printf '\n'

exec "$@"
