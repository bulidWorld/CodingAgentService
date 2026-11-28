# Codex SQLite log collector

This sidecar tails Codex app-server `logs_*.sqlite` files from a shared `CODEX_HOME` and writes line-delimited JSON to stdout. Mount the Codex home volume read-only in the collector container.

Build the image from the repository root:

```sh
docker build --provenance=false -f codex_log_collect/Dockerfile \
  -t lingxi.image.io/kunpeng/agent/coding-agent/codex-log-collector:v0.2 \
  codex_log_collect
```

Configuration:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_HOME` | `/var/service/coding-agent-service/share/.codex` | Directory containing Codex SQLite logs |
| `CODEX_LOG_MIN_LEVEL` | `WARN` | Minimum level: `TRACE`, `DEBUG`, `INFO`, `WARN`, or `ERROR` |
| `CODEX_LOG_POLL_SECONDS` | `1` | SQLite polling interval, minimum 0.2 seconds |
| `CODEX_LOG_BATCH_SIZE` | `500` | Maximum rows read per database per poll |
| `CODEX_LOG_START_POSITION` | `latest` | `latest` skips existing rows at startup; `beginning` reads retained rows |

`WARN` is the default to avoid sending high-volume request and payload traces to cluster logs. Set `CODEX_LOG_MIN_LEVEL=TRACE` only when detailed runtime traces are needed. Common bearer and API key patterns are redacted before output.
