# mcp-proxy-stack

Docker setup for [`mcp-proxy`](https://pypi.org/project/mcp-proxy/) (sparfenyuk) 0.14.0. It runs several stdio MCP servers and exposes each one over streamable HTTP (and SSE) at `/servers/<name>/mcp`. All mcp-proxy options are set through environment variables in `compose.yml` / `.env`. An entrypoint script turns them into CLI flags, because mcp-proxy does not read its options from the environment.

## Images

Both are built by CI and published to GHCR for `linux/amd64` and `linux/arm64`:

| Tag | Dockerfile | What it is |
|---|---|---|
| `ghcr.io/shuricksumy/mcp-proxy-stack:latest` | [Dockerfile](Dockerfile) | **Stable.** mcp-proxy, `mcp`, uv and Python all pinned. Renovate bumps them through PRs that only merge when CI passes. |
| `ghcr.io/shuricksumy/mcp-proxy-stack:<version>` (e.g. `0.14.0`) | [Dockerfile](Dockerfile) | Stable, kept per mcp-proxy version for pinning or rollback. |
| `ghcr.io/shuricksumy/mcp-proxy-stack:next` | [Dockerfile.next](Dockerfile.next) | **Newest.** Latest mcp-proxy, uv and `python:3.12-slim`, rebuilt every week. Can force a newer `mcp` SDK (`MCP_SPEC`). Has its own compose file, `.env` and `servers.json`. |

Both images run a smoke test while building: the build fails if mcp-proxy cannot import. A broken combination, such as forcing `mcp>=2` today, therefore never produces an image.

**mcp 2.x:** mcp-proxy (0.14.0 on PyPI, and 0.15.0 on `main`) still requires `mcp<2`, because mcp 2.0 removed `request_ctx`, which mcp-proxy imports. The weekly [mcp2-probe](.github/workflows/mcp2-probe.yml) workflow builds `:next` with `MCP_SPEC=mcp>=2`. It opens an issue the first time that works.

## Quick start

```sh
cp .env.example .env
cp data/mcp/servers.json.example data/mcp/servers.json   # then edit
docker compose pull && docker compose up -d     # or: docker compose up -d --build
docker logs -f mcp-proxy
```

The `next` image runs side by side on port 8006, with its own config:

```sh
cp .env.next.example .env.next
cp data/mcp-next/servers.json.example data/mcp-next/servers.json   # then edit
docker compose -f compose.next.yml --env-file .env.next pull
docker compose -f compose.next.yml --env-file .env.next up -d
```

To test a local image: `scripts/smoke-test.sh <image>`. It starts the image with one stdio server and expects a successful MCP `initialize` for an allowed Host, and HTTP 421 for an unknown one.

Endpoints: `http://<host>:8005/servers/<name>/mcp` (streamable HTTP) and `.../servers/<name>/sse`.

`servers.json` uses the `mcpServers` format: each entry has `command`, `args` (a list), an optional `env` (object) and an optional `enabled` (default `true`). mcp-proxy ignores any other keys, such as `timeout`, `type` or `transportType`. `env` values are literal. `${VAR}` is **not** expanded, so to keep secrets out of the file, set `MCP_PROXY_PASS_ENV=true` and pass them as container env vars.

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

## Automation

- **ci** (PRs, and before every publish): builds both images and runs the smoke test on each, plus shellcheck.
- **release** (push to `main`, weekly, manual): runs CI, then publishes `:latest`, `:<version>` and `:next`.
- **Renovate** ([renovate.json](renovate.json), shared preset `shuricksumy/renovate-config`) owns version updates.
  - mcp-proxy and `mcp` are updated together in one PR. Patch and minor updates merge automatically once CI passes.
  - `mcp` 2.x is skipped for the stable image (see mcp2-probe above).
  - Action and image digest pins, and uv patch/minor updates, also merge automatically.
- **Dependabot** handles security updates only. Security patch PRs merge automatically.
- Also: CodeQL (scans the workflows), zizmor, dependency-review, Renovate config validation, and a monthly GHCR cleanup of untagged images.
