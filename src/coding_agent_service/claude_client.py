"""Claude Agent SDK 调用封装，集中管理工具权限和模型配置。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query
from coding_agent_service.prompts import build_chat_prompt, build_coding_prompt

logger = logging.getLogger(__name__)


def _mcp_servers() -> dict[str, dict[str, Any]]:
    """Build remote MCP configuration from runtime environment variables."""
    servers = {
        "operator-agent-service": (
            "OPERATOR_AGENT_SERVICE_MCP_URL",
            "OPERATOR_AGENT_SERVICE_MCP_TOKEN",
        ),
        "pod-debug-service": (
            "POD_DEBUG_SERVICE_MCP_URL",
            "POD_DEBUG_SERVICE_MCP_TOKEN",
        ),
        "ci-build-service": (
            "CI_BUILD_SERVICE_MCP_URL",
            "CI_BUILD_SERVICE_MCP_TOKEN",
        ),
    }
    configs: dict[str, dict[str, Any]] = {}
    for name, (url_variable, token_variable) in servers.items():
        url = os.getenv(url_variable)
        token = os.getenv(token_variable)
        if url and token:
            configs[name] = {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        else:
            logger.warning(
                "MCP server token is not configured",
                extra={"log_type": "llm", "event": "mcp_configuration", "server": name,
                       "status": "missing_configuration", "url_variable": url_variable,
                       "token_variable": token_variable},
            )
    return configs


def _log_cli_stderr(line: str) -> None:
    """将 CLI 诊断纳入服务日志。"""
    if line.startswith("[claude-code:unrecognized_model] "):
        # CLI 可能无法识别网关模型名，但仍会发出请求并完成任务。
        logger.debug("Claude Code CLI diagnostic", extra={"log_type": "llm", "event": "cli_diagnostic", "detail": line})
    else:
        logger.warning("Claude Code CLI diagnostic", extra={"log_type": "llm", "event": "cli_diagnostic", "detail": line})


async def run_claude(
    working_path: Path,
    instruction: str,
    cancel_event: asyncio.Event,
    *,
    session_id: str,
    resume: bool,
    chat: bool = False,
    on_message: Callable[[Any], Any] | None = None,
) -> tuple[ResultMessage, list[Path]]:
    """执行提示词并返回 SDK 结果及执行期间写入的 Markdown 文件路径。"""
    markdown_files: set[Path] = set()
    started = time.monotonic()
    tool_calls = 0
    message_count = 0
    tool_use_ids: set[str] = set()

    async def log_tool_start(
        hook_input: dict[str, Any], tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        """记录任意 SDK 工具调用的开始，不记录工具参数内容。"""
        nonlocal tool_calls
        tool_calls += 1
        tool_name = hook_input.get("tool_name")
        if tool_use_id:
            tool_use_ids.add(tool_use_id)
        logger.info("Claude tool call started", extra={"log_type": "llm", "event": "tool_call",
                    "provider": "claude", "session_id": session_id,
                    "tool_name": tool_name if isinstance(tool_name, str) else "unknown",
                    "tool_use_id": tool_use_id, "status": "started"})
        return {}

    async def capture_markdown_file(
        hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        """记录 Write/Edit 工具操作的 Markdown 文件，内容稍后从磁盘读取。"""
        tool_name = hook_input.get("tool_name")
        tool_input = hook_input.get("tool_input", {})
        tool_use_id = _tool_use_id
        if tool_use_id:
            tool_use_ids.discard(tool_use_id)
        logger.info(
            "Claude tool call completed",
            extra={"log_type": "llm", "event": "tool_call", "provider": "claude",
                   "tool_name": tool_name if isinstance(tool_name, str) else "unknown",
                   "tool_use_id": tool_use_id, "session_id": session_id, "status": "completed"},
        )
        if tool_name not in {"Write", "Edit", "MultiEdit"} or not isinstance(tool_input, dict):
            return {}
        raw_path = tool_input.get("file_path") or tool_input.get("path")
        if not isinstance(raw_path, str) or Path(raw_path).suffix.lower() != ".md":
            return {}
        raw_file_path = Path(raw_path)
        path = (working_path / raw_file_path).resolve() if not raw_file_path.is_absolute() else raw_file_path.resolve()
        try:
            path.relative_to(working_path.resolve())
        except ValueError:
            return {}
        markdown_files.add(path)
        return {}

    options = ClaudeAgentOptions(
        cwd=str(working_path),
        mcp_servers=_mcp_servers(),
        stderr=_log_cli_stderr,
        allowed_tools=[] if chat else ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        setting_sources=[],
        resume=session_id if resume else None,
        extra_args={} if resume else {"session-id": session_id},
        max_turns=int(os.getenv("CLAUDE_MAX_TURNS", "30")),
        hooks={
            "PreToolUse": [HookMatcher(hooks=[log_tool_start])],
            "PostToolUse": [HookMatcher(hooks=[capture_markdown_file])],
        },
    )
    logger.info(
        "Claude SDK options prepared",
        extra={"log_type": "llm", "event": "sdk_options_prepared", "provider": "claude",
               "session_id": session_id, "mode": "chat" if chat else "coding",
               "mcp_server_count": len(options.mcp_servers or {})},
    )
    # 聊天模式不开放工具；代码模式明确要求聚焦任务且不自行创建提交。
    prompt = build_chat_prompt(instruction) if chat else build_coding_prompt(instruction)
    result = None
    async def log_stream_heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            logger.info(
                "Claude SDK stream is still waiting for messages",
                extra={"log_type": "llm", "event": "stream_wait_heartbeat", "provider": "claude",
                       "session_id": session_id,
                       "duration_ms": round((time.monotonic() - started) * 1000, 2)},
            )

    logger.info("Claude request started", extra={"log_type": "llm", "event": "request_started",
                "provider": "claude", "session_id": session_id, "mode": "chat" if chat else "coding",
                "resume": resume, "max_turns": options.max_turns})
    heartbeat_task = asyncio.create_task(log_stream_heartbeat())
    try:
        logger.info(
            "Waiting for Claude SDK stream",
            extra={"log_type": "llm", "event": "stream_wait_started", "provider": "claude",
                   "session_id": session_id},
        )
        async for message in query(prompt=prompt, options=options):
            if cancel_event.is_set():
                logger.info("Claude request cancelled", extra={"log_type": "llm", "event": "request_cancelled",
                            "provider": "claude", "session_id": session_id})
                raise asyncio.CancelledError
            message_count += 1
            if isinstance(message, ResultMessage):
                result = message
                logger.info("Claude request result received", extra={"log_type": "llm", "event": "result",
                            "provider": "claude", "session_id": session_id, "is_error": message.is_error,
                            "subtype": message.subtype, "num_turns": message.num_turns,
                            "cost_usd": message.total_cost_usd, "usage": message.usage})
            else:
                logger.info("Claude stream event received", extra={"log_type": "llm", "event": "stream_message",
                            "provider": "claude", "session_id": session_id,
                            "message_type": type(message).__name__})
            if on_message is not None:
                callback_result = on_message(message)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
        if result is None:
            raise RuntimeError("Claude did not return a session result")
        if result.is_error:
            details = result.result or "; ".join(getattr(result, "errors", None) or [])
            raise RuntimeError(details or f"Claude task failed ({result.subtype})")
        if result.session_id != session_id:
            raise RuntimeError("Claude returned an unexpected session ID")
        logger.info("Claude request completed", extra={"log_type": "llm", "event": "request_completed",
                    "provider": "claude", "session_id": session_id, "message_count": message_count,
                    "tool_call_count": tool_calls, "duration_ms": round((time.monotonic() - started) * 1000, 2)})
        return result, sorted(markdown_files)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Claude request failed", extra={"log_type": "llm", "event": "request_failed",
                         "provider": "claude", "session_id": session_id,
                         "duration_ms": round((time.monotonic() - started) * 1000, 2)})
        raise
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
