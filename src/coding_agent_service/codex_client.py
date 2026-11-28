"""Codex SDK 的会话、流式事件及工具配置。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    ErrorNotification,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallThreadItem,
    MessagePhase,
    PatchApplyStatus,
    ReasoningEffort,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnPlanUpdatedNotification,
    TurnStatus,
)

from coding_agent_service.errors import TaskInputError
from coding_agent_service.prompts import build_chat_prompt, build_coding_prompt

logger = logging.getLogger(__name__)

MCP_SERVERS = {
    "operator-agent-service": "OPERATOR_AGENT_SERVICE",
    "pod-debug-service": "POD_DEBUG_SERVICE",
    "ci-build-service": "CI_BUILD_SERVICE",
}


TOOL_LABELS = {
    "commandExecution": "终端命令",
    "fileChange": "文件修改",
    "mcpToolCall": "MCP 工具",
    "webSearch": "网页搜索",
}

HEARTBEAT_SECONDS = 30
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30 * 60

WAIT_MESSAGES = {
    "starting_sdk": "正在初始化 Codex SDK",
    "opening_thread": "正在建立 Codex 会话",
    "starting_turn": "正在启动 Codex 执行",
    "waiting_for_first_event": "正在等待 Codex 的首条事件",
    "streaming": "正在等待 Codex 的下一条进度",
}


@dataclass(frozen=True)
class CodexResult:
    session_id: str
    text: str


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


def _markdown_paths(item: FileChangeThreadItem, workdir: Path) -> set[Path]:
    paths = set()
    for change in item.changes:
        path = (workdir / change.path).resolve()
        if path.is_relative_to(workdir) and path.suffix.lower() == ".md" and path.is_file():
            paths.add(path)
    return paths


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
) -> tuple[CodexResult, list[Path]]:
    """执行一轮 Codex 请求，返回最终回复和 Markdown 文件路径。"""
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
        nonlocal last_event_method
        codex_home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").resolve()
        codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        session_file = codex_home / "coding-agent-service" / f"{session_id}.json"
        scope = {"mode": mode, "cwd": str(workdir), "branch": branch}
        thread_id = None
        if resume:
            try:
                saved = json.loads(session_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise TaskInputError("找不到 Codex 会话，请省略 sessionId 创建新会话") from None
            if saved["scope"] != scope:
                raise TaskInputError("续聊必须使用原来的任务类型、仓库和分支")
            thread_id = saved["thread_id"]

        config = {"mcp_servers": {} if chat else _mcp_servers()}
        if chat:
            config.update({
                "web_search": "disabled",
                "features": {"shell_tool": False, "apps": False, "multi_agent": False},
            })
        logger.info(
            "Codex runtime configuration prepared",
            extra={**log_context, "event": "sdk_options_prepared",
                   "model": os.getenv("CODEX_MODEL"), "mcp_server_count": len(config["mcp_servers"])},
        )
        gateway_url = os.getenv("CODEX_GATEWAY_BASE_URL")
        if gateway_url:
            if not os.getenv("CODEX_GATEWAY_API_KEY"):
                raise RuntimeError("已配置 Codex 网关，请设置 CODEX_GATEWAY_API_KEY")
            config["model_providers"] = {
                "personal_gateway": {
                    "name": "Personal Codex Gateway",
                    "base_url": gateway_url,
                    "wire_api": "responses",
                    "requires_openai_auth": False,
                    "supports_websockets": False,
                    "env_key": "CODEX_GATEWAY_API_KEY",
                },
            }
        effort = os.getenv("CODEX_REASONING_EFFORT")
        options = dict(
            cwd=str(workdir),
            model=os.getenv("CODEX_MODEL") or None,
            model_provider="personal_gateway" if gateway_url else None,
            service_tier=os.getenv("CODEX_SERVICE_TIER") or None,
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.auto_review,
            config=config,
        )

        markdown_files: set[Path] = set()
        final_text = ""
        completed = False
        tool_started: dict[str, float] = {}
        await set_phase("starting_sdk")
        async with AsyncCodex(CodexConfig(cwd=str(workdir), env={"CODEX_HOME": str(codex_home)})) as codex:
            await set_phase("opening_thread")
            thread = (
                await codex.thread_resume(thread_id, **options)
                if thread_id else await codex.thread_start(**options)
            )
            logger.info("Codex thread ready", extra={**log_context, "event": "thread_ready", "thread_id": thread.id})
            prompt = build_chat_prompt(instruction) if chat else build_coding_prompt(instruction)
            await set_phase("starting_turn")
            turn = await thread.turn(prompt, effort=ReasoningEffort(effort) if effort else None)
            turn_context = {**log_context, "thread_id": thread.id, "turn": turn.id}
            logger.info("Codex turn ready", extra={**turn_context, "event": "turn_ready"})
            await set_phase("waiting_for_first_event")
            generating_reply = False
            try:
                async with aclosing(turn.stream()) as stream:
                    async for event in stream:
                        last_event_method = event.method
                        if phase == "waiting_for_first_event":
                            await set_phase("streaming")
                        payload = event.payload
                        if isinstance(payload, ErrorNotification):
                            logger.warning(
                                "Codex stream reported an error",
                                extra={**turn_context, "event": "stream_error", "will_retry": payload.will_retry},
                            )
                            if payload.will_retry:
                                await publish_progress("Codex 请求遇到错误，正在重试")
                        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                            logger.info(
                                "Codex token usage updated",
                                extra={**turn_context, "event": "token_usage",
                                       "usage": payload.token_usage.last.model_dump()},
                            )
                        elif isinstance(payload, TurnPlanUpdatedNotification):
                            labels = {"pending": "待处理", "inProgress": "进行中", "completed": "已完成"}
                            steps = [f"[{labels.get(step.status.value, step.status.value)}] {step.step}" for step in payload.plan]
                            logger.info(
                                "Codex plan updated",
                                extra={**turn_context, "event": "plan_updated",
                                       "plan_statuses": [step.status.value for step in payload.plan]},
                            )
                            if steps:
                                await publish_progress("Codex 计划更新\n" + "\n".join(steps))
                        elif isinstance(payload, AgentMessageDeltaNotification) and payload.delta and not generating_reply:
                            generating_reply = True
                            logger.info("Codex reply started", extra={**turn_context, "event": "reply_started"})
                            await publish_progress("Codex 正在生成回复")
                        elif isinstance(payload, (ItemStartedNotification, ItemCompletedNotification)):
                            item = payload.item.root
                            status = "started" if isinstance(payload, ItemStartedNotification) else "completed"
                            if item.type not in TOOL_LABELS:
                                logger.info(
                                    "Codex item lifecycle",
                                    extra={**turn_context, "event": "item_lifecycle", "message_type": item.type,
                                           "item_id": item.id, "status": status},
                                )
                            if isinstance(item, AgentMessageThreadItem):
                                if isinstance(payload, ItemCompletedNotification):
                                    if item.phase != MessagePhase.commentary:
                                        final_text = item.text
                                    generating_reply = False
                                    if item.text:
                                        await publish_progress(item.text)
                            elif item.type in TOOL_LABELS:
                                now = time.monotonic()
                                if status == "started":
                                    tool_started[item.id] = now
                                began = tool_started.pop(item.id, None) if status == "completed" else None
                                tool_name = item.tool if isinstance(item, McpToolCallThreadItem) else item.type
                                status_detail = getattr(getattr(item, "status", None), "value", None)
                                exit_code = item.exit_code if isinstance(item, CommandExecutionThreadItem) else None
                                logger.info(
                                    "Codex tool event",
                                    extra={**turn_context, "event": "tool_call", "tool_name": tool_name,
                                           "server": item.server if isinstance(item, McpToolCallThreadItem) else None,
                                           "message_type": item.type, "tool_use_id": item.id, "status": status,
                                           "status_detail": status_detail, "exit_code": exit_code,
                                           "duration_ms": round((now - began) * 1000, 2) if began is not None else None},
                                )
                                if (
                                    isinstance(item, FileChangeThreadItem)
                                    and status == "completed"
                                    and item.status == PatchApplyStatus.completed
                                ):
                                    markdown_files.update(_markdown_paths(item, workdir))
                                action = "开始" if status == "started" else "结束"
                                label = TOOL_LABELS[item.type]
                                if isinstance(item, McpToolCallThreadItem):
                                    label = f"{label} {item.server}/{item.tool} "
                                detail = f"（退出码 {exit_code}）" if exit_code else ""
                                await publish_progress(f"Codex {label}{action}{detail}")
                        elif isinstance(payload, TurnCompletedNotification):
                            logger.info(
                                "Codex turn ended",
                                extra={**turn_context, "event": "turn_ended", "status": payload.turn.status.value},
                            )
                            if payload.turn.status != TurnStatus.completed:
                                detail = payload.turn.error.message if payload.turn.error else payload.turn.status.value
                                raise RuntimeError(f"Codex 任务失败：{detail}")
                            completed = True
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(turn.interrupt(), timeout=3)
                except Exception:
                    logger.debug("Codex turn interruption failed", extra=log_context, exc_info=True)
                raise

            if not completed:
                raise RuntimeError("Codex 未返回任务完成事件")
            if not resume:
                session_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = session_file.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps({"scope": scope, "thread_id": thread.id}), encoding="utf-8",
                )
                temporary.replace(session_file)
                logger.info("Codex session saved", extra={**turn_context, "event": "session_saved"})
        return CodexResult(session_id, final_text), sorted(markdown_files)

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
