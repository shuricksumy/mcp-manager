# mcp-proxy-stack

Docker setup for [`mcp-proxy`](https://pypi.org/project/mcp-proxy/) (sparfenyuk) 0.14.0. It runs several stdio MCP servers and exposes each one over streamable HTTP (and SSE) at `/servers/<name>/mcp`. All mcp-proxy options are set through environment variables in `compose.yml` / `.env`. An entrypoint script turns them into CLI flags, because mcp-proxy does not read its options from the environment.

## Quick start

```sh
cp .env.example .env
cp data/mcp/servers.json.example data/mcp/servers.json   # then edit
docker compose up -d --build
docker logs -f mcp-proxy
```

Endpoints: `http://<host>:8005/servers/<name>/mcp` (streamable HTTP) and `.../servers/<name>/sse`.

`servers.json` uses the `mcpServers` format: each entry has `command`, `args` (a list), an optional `env` (object) and an optional `enabled` (default `true`). `env` values are literal. `${VAR}` is **not** expanded, so to keep secrets out of the file, set `MCP_PROXY_PASS_ENV=true` and pass them as container env vars.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `MCP_PROXY_PUBLISHED_PORT` | `8005` | Host port (compose only) |
| `MCP_PROXY_CONTAINER_NAME` | `mcp-proxy` | Container name (compose only) |
| `MCP_CONFIG_DIR` | `./data/mcp` | Host dir mounted at `/mcp` (compose only) |
| `MCP_PROXY_HOST` | `0.0.0.0` | `--host` |
| `MCP_PROXY_PORT` | `8000` | `--port` (container side) |
| `MCP_PROXY_TRANSPORT` | `streamablehttp` | `--transport` (mcp-proxy treats it as a client-mode option; server mode serves both `/mcp` and `/sse`) |
| `MCP_PROXY_CONFIG` | `/mcp/servers.json` | `--named-server-config` |
| `MCP_PROXY_ALLOWED_HOSTS` | *(empty)* | Comma list → repeated `--allowed-host`, e.g. `192.168.111.111:*,proxy-mcp:*` |
| `MCP_PROXY_ALLOW_ORIGINS` | `*` | Comma list → repeated `--allow-origin`. Set it to empty to turn CORS off |
| `MCP_PROXY_PASS_ENV` | `false` | `true` → `--pass-environment` |
| `MCP_PROXY_LOG_LEVEL` | *(empty)* | `--log-level` |
| `MCP_PROXY_DEBUG` | `false` | `true` → `--debug` |
| `MCP_PROXY_EXTRA_ARGS` | *(empty)* | Raw extra flags, split on whitespace |

List items are trimmed and empty items are skipped. Arguments given in compose `command:` are added at the end. The full command is printed at startup. Only flags are printed, never environment values.

## Troubleshooting: HTTP 421 "Invalid Host header"

From 0.13.3 on, mcp-proxy has DNS-rebinding protection. It accepts only loopback `Host` headers and the bind host. Requests from LAN clients (`Host: 192.168.111.111:8005`) or from other containers (`Host: proxy-mcp:8000`) fail with **421**, and the log shows `Invalid Host header: ...`. To fix it, list every hostname clients use in `MCP_PROXY_ALLOWED_HOSTS`. A wildcard port (`host:*`) is supported. `--allow-origin` only covers the `Origin` header. It does not help with this error.

## Security

- **There is no authentication.** In server mode, mcp-proxy does not check incoming credentials. `API_ACCESS_TOKEN` only applies in client mode, so setting it does **not** protect this endpoint. Limit who can reach the port (firewall, an internal Docker network, a VPN), or put an authenticating reverse proxy in front.
- If a stdio server needs `/var/run/docker.sock`, mounting it `:ro` **still effectively gives root on the host**. Read-only applies to the socket file, not to the Docker API, which can still start privileged containers. Mount it only for servers you trust.
- `MCP_PROXY_PASS_ENV=true` passes the whole container environment to every stdio server.
