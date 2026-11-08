FROM python:3.12-slim

ARG MCP_PROXY_VERSION=0.14.0

# git/curl/nodejs/npm are needed by the stdio servers launched via uvx / npx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

# mcp-proxy 0.14.0 requires mcp>=1.30,<2.
RUN uv tool install "mcp-proxy==${MCP_PROXY_VERSION}" --with "mcp>=1.30,<2"
ENV PATH="/root/.local/bin:${PATH}"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
