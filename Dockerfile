FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

# Both versions are updated by Renovate (renovate.json, custom regex managers),
# together in one PR so they always satisfy mcp-proxy's own mcp requirement.
ARG MCP_PROXY_VERSION=0.14.0
ARG MCP_VERSION=1.30.0

# git/curl/nodejs/npm are needed by the stdio servers launched via uvx / npx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /bin/

RUN uv tool install "mcp-proxy==${MCP_PROXY_VERSION}" --with "mcp==${MCP_VERSION}"
ENV PATH="/root/.local/bin:${PATH}"

# Smoke test: fail the build if mcp-proxy cannot import, and record versions
# (entrypoint.sh prints them at startup).
RUN set -e; \
    mcp-proxy --help > /dev/null; \
    /root/.local/share/uv/tools/mcp-proxy/bin/python -c \
      "import importlib.metadata as m; print('mcp-proxy', m.version('mcp-proxy'), '| mcp', m.version('mcp'))" \
      > /etc/mcp-proxy.versions; \
    cat /etc/mcp-proxy.versions

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
