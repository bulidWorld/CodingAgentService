FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CODEX_HOME=/var/service/coding-agent-service/share/.codex \
    CODEX_LOG_MIN_LEVEL=WARN \
    CODEX_LOG_POLL_SECONDS=1 \
    CODEX_LOG_BATCH_SIZE=500 \
    CODEX_LOG_START_POSITION=latest

WORKDIR /app
COPY collector.py /app/collector.py

ENTRYPOINT ["python", "/app/collector.py"]
