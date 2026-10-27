# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY pyproject.toml README.md ./
COPY usesend_mcp ./usesend_mcp
RUN pip install --no-cache-dir .

FROM python:3.12-slim
RUN groupadd --gid 10001 mcp \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcp
COPY --from=builder /opt/venv /opt/venv
COPY --chmod=0755 entrypoint.sh /entrypoint.sh
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
# No USER line: entrypoint.sh reads the token file as root, then drops to 10001.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "usesend_mcp.server"]
