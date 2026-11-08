FROM python:3.12-slim

# Both versions are updated by Renovate (renovate.json, custom regex managers),
# together in one PR so they always satisfy mcp-proxy's own mcp requirement.
ARG MCP_PROXY_VERSION=0.14.0
ARG MCP_VERSION=1.30.0

# git/curl/nodejs/npm are needed by the stdio servers launched via uvx / npx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm tini \
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
COPY mcp-supervise.py /usr/local/bin/mcp-supervise
COPY mcp-manager.py /usr/local/bin/mcp-manager
RUN chmod +x /entrypoint.sh /usr/local/bin/mcp-supervise /usr/local/bin/mcp-manager

# 8000: mcp-proxy, 8001: mcp-manager admin page (MCP_MANAGER=true)
EXPOSE 8000 8001
# tini: reaps orphaned server processes and forwards stop signals.
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
