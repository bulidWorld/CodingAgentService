"""配置符合服务日志规范的 JSON 控制台输出。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """将标准 logging 记录转换为单行 JSON，便于日志平台采集。"""

    def format(self, record: logging.LogRecord) -> str:
        """输出必需字段，并附加存在的请求上下文和异常堆栈。"""
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "log_type": getattr(record, "log_type", "business"),
            "message": record.getMessage(),
        }
        for key in (
            "trace_id", "method", "path", "status", "duration_ms", "task_id", "exceptionInfo",
            "session_id", "provider", "model", "event", "message_type", "tool_name", "tool_use_id",
            "server", "turn", "is_error", "num_turns", "cost_usd", "usage", "mode", "resume",
            "max_turns", "message_count", "tool_call_count", "subtype", "status_detail", "detail",
            "workspace", "branch", "phase", "git_command",
            "mcp_server_count",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exceptionInfo"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """替换根 logger 的 handler，统一应用日志格式和级别。"""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
