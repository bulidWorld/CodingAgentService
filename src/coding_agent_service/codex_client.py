"""Codex SDK 的会话、流式事件及工具配置。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import Artifact, TaskArtifactUpdateEvent

from coding_agent_service.errors import TaskInputError
from coding_agent_service.codex_executor import execute_codex
from coding_agent_service.codex_models import CodexResult, MCP_SERVERS
from a2a.helpers import new_data_part

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30 * 60

WAIT_MESSAGES = {
    "starting_sdk": "正在初始化 Codex SDK",
    "opening_thread": "正在建立 Codex 会话",
    "starting_turn": "正在启动 Codex 执行",
    "waiting_for_first_event": "正在等待 Codex 的首条事件",
    "streaming": "正在等待 Codex 的下一条进度",
}


def _mcp_servers() -> dict[str, dict[str, str | bool]]:
    servers: dict[str, dict[str, str | bool]] = {}
    for name, prefix in MCP_SERVERS.items():
        url_variable = f"{prefix}_MCP_URL"
        token_variable = f"{prefix}_MCP_TOKEN"
        url = os.getenv(url_variable)
        if url and os.getenv(token_variable):
            servers[name] = {"enabled": True, "url": url, "bearer_token_env_var": token_variable}
            logger.info(
                "MCP server configured",
                extra={"log_type": "llm", "event": "mcp_configuration", "server": name, "status": "configured"},
            )
        else:
            logger.warning(
                "MCP server configuration is incomplete",
                extra={"log_type": "llm", "event": "mcp_configuration", "server": name,
                       "url_variable": url_variable, "token_variable": token_variable},
            )
    return servers


async def run_codex(
    working_path: Path,
    instruction: str,
    cancel_event: asyncio.Event,
    *,
    session_id: str,
    resume: bool,
    chat: bool = False,
    branch: str | None = None,
    task_id: str | None = None,
    on_message: Callable[[str], Awaitable[None]] | None = None,
    updater: TaskUpdater | None = None,
) -> tuple[CodexResult, list[Path]]:
    """执行一轮 Codex 请求并发布结构化消息和工具事件。

    参数：
        working_path: Codex 工作目录。
        instruction: 用户输入的任务指令。
        cancel_event: 任务取消信号。
        session_id: 服务会话标识。
        resume: 是否续接已有 Codex 会话。
        chat: 是否按聊天模式执行。
        branch: coding 会话对应的 Git 分支。
        task_id: A2A 任务标识。
        on_message: 可选的纯文本进度回调。
        updater: 可选的 A2A 更新器，用于发送结构化响应事件。
    返回：
        Codex 最终文本结果和修改的 Markdown 文件路径。
    """
    workdir = working_path.resolve()
    mode = "chat" if chat else "coding"
    started = time.monotonic()
    try:
        request_timeout = float(os.getenv("CODEX_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS))
    except ValueError as error:
        raise TaskInputError("CODEX_REQUEST_TIMEOUT_SECONDS 必须是正数") from error
    if request_timeout <= 0:
        raise TaskInputError("CODEX_REQUEST_TIMEOUT_SECONDS 必须是正数")
    log_context = {
        "log_type": "llm", "provider": "codex", "session_id": session_id, "task_id": task_id, "mode": mode,
    }

    phase = "starting_sdk"
    last_event_method = None
    last_progress = started
    progress_lock = asyncio.Lock()

    async def publish_progress(
        message: str, *, idle: bool = False,
    ) -> None:
        nonlocal last_progress
        if on_message is None:
            return
        async with progress_lock:
            if idle and time.monotonic() - last_progress < HEARTBEAT_SECONDS:
                return
            await on_message(message)
            last_progress = time.monotonic()

    async def publish_response_data(data: dict[str, object]) -> None:
        """将一个符合响应模型的结构化 data part 发布为 artifact。"""
        if updater is None:
            return
        artifact = Artifact(artifact_id=str(uuid.uuid4()), parts=[new_data_part(data)])
        await updater.event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=updater.task_id,
                context_id=updater.context_id,
                artifact=artifact,
                last_chunk=True,
            )
        )

    async def set_phase(value: str) -> None:
        nonlocal phase
        phase = value
        logger.info(
            "Codex request phase changed",
            extra={**log_context, "event": "sdk_phase", "phase": phase,
                   "duration_ms": round((time.monotonic() - started) * 1000, 2)},
        )

        if phase in WAIT_MESSAGES and phase != "streaming":
            await publish_progress(WAIT_MESSAGES[phase])

    async def execute() -> tuple[CodexResult, list[Path]]:
        return await execute_codex(
            working_path, instruction, session_id=session_id, resume=resume, chat=chat,
            branch=branch, log_context=log_context, set_phase=set_phase,
            publish_progress=publish_progress, publish_response_data=publish_response_data,
            set_last_event_method=lambda value: _set_last_event_method(value), get_mcp_servers=_mcp_servers,
            updater=updater,
        )

    def _set_last_event_method(value: str) -> None:
        nonlocal last_event_method
        last_event_method = value

    if cancel_event.is_set():
        raise asyncio.CancelledError
    logger.info("Codex request started", extra={**log_context, "event": "request_started", "resume": resume})
    request = asyncio.create_task(execute())
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        while True:
            elapsed = time.monotonic() - started
            remaining = request_timeout - elapsed
            if remaining <= 0:
                logger.error(
                    "Codex request timed out",
                    extra={**log_context, "event": "request_timeout", "phase": phase,
                           "timeout_seconds": request_timeout,
                           "duration_ms": round(elapsed * 1000, 2)},
                )
                raise TimeoutError(f"Codex 请求超过 {int(request_timeout)} 秒仍未完成")
            done, _ = await asyncio.wait(
                {request, cancelled}, timeout=min(HEARTBEAT_SECONDS, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise asyncio.CancelledError
            if request in done:
                result = request.result()
                logger.info(
                    "Codex request completed",
                    extra={**log_context, "event": "request_completed",
                           "duration_ms": round((time.monotonic() - started) * 1000, 2)},
                )
                return result
            elapsed = time.monotonic() - started
            if elapsed >= request_timeout:
                logger.error(
                    "Codex request timed out",
                    extra={**log_context, "event": "request_timeout", "phase": phase,
                           "timeout_seconds": request_timeout,
                           "duration_ms": round(elapsed * 1000, 2)},
                )
                raise TimeoutError(f"Codex 请求超过 {int(request_timeout)} 秒仍未完成")
            logger.info(
                "Codex SDK stream is still running",
                extra={**log_context, "event": "stream_wait_heartbeat", "phase": phase,
                       "message_type": last_event_method, "duration_ms": round(elapsed * 1000, 2)},
            )
            await publish_progress(f"{WAIT_MESSAGES[phase]}，本次请求已用时 {int(elapsed)} 秒", idle=True)
    except asyncio.CancelledError:
        logger.info("Codex request cancelled", extra={**log_context, "event": "request_cancelled"})
        raise
    except Exception:
        logger.exception("Codex request failed", extra={**log_context, "event": "request_failed"})
        raise
    finally:
        if not request.done():
            request.cancel()
        cancelled.cancel()
        await asyncio.gather(request, cancelled, return_exceptions=True)
