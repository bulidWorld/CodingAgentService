FROM python:3.11-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN uv sync -vv --frozen --no-dev --no-install-project

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    SERVICE_NAME="coding-agent-service" \
    CODEX_HOME="/var/service/coding-agent-service/share/.codex" \
    PROJECTS_ROOT="/var/service/coding-agent-service/share/projects" \
    WORKSPACE_ROOT="/var/service/coding-agent-service/share/work"

# Use Tsinghua's Debian mirrors before installing system packages.
RUN sed -i \
    -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
    -e 's|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
    /etc/apt/sources.list.d/debian.sources

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY api ./api

RUN mkdir -p /var/service/coding-agent-service/share/projects \
    /var/service/coding-agent-service/share/work

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "coding_agent_service.server:app", "--app-dir", "/app/src", "--host", "0.0.0.0", "--port", "8000"]
