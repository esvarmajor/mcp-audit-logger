# syntax=docker/dockerfile:1.7
#
# Multi-stage image for mcp-audit-logger.
#
# Stage 1 builds a wheel from the working tree.
# Stage 2 is a slim runtime that installs the wheel and nothing else.
#
# Build:   docker build -t mcp-audit-logger .
# Run:     docker run --rm -p 8765:8765 -v "$PWD/audit:/data" \
#              -e AUDIT_DB_PATH=/data/audit.db mcp-audit-logger
#
# Healthcheck hits /healthz, which the proxy always exposes.

FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /wheels


FROM python:3.12-slim AS runtime
ARG UID=10001
RUN useradd --system --uid ${UID} --create-home --home-dir /home/audit audit \
 && mkdir -p /data \
 && chown audit:audit /data
WORKDIR /home/audit
COPY --from=build /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
 && rm -rf /tmp/*.whl /root/.cache

USER audit
ENV AUDIT_HOST=0.0.0.0 \
    AUDIT_PORT=8765 \
    AUDIT_DB_PATH=/data/audit.db
EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).status==200 else 1)"

ENTRYPOINT ["mcp-audit-logger"]
