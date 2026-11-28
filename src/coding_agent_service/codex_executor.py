"""Codex SDK 执行流程及流式事件处理。"""

from __future__ import annotations

import json
import logging
import os
import time
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
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
    McpToolCallResult,
    MessagePhase,
    PatchApplyStatus,
    ReasoningEffort,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnPlanUpdatedNotification,
    TurnStatus,
)
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.helpers import new_data_part, new_text_message, new_text_part

from coding_agent_service.codex_models import CodexResult
from coding_agent_service.errors import TaskInputError
from coding_agent_service.prompts import build_chat_prompt, build_coding_prompt

logger = logging.getLogger(__name__)


def _markdown_paths(item: FileChangeThreadItem, workdir: Path) -> set[Path]:
    """筛选此次文件修改中实际存在的 Markdown 路径。

    参数：
        item: Codex 文件修改线程项。
        workdir: Codex 工作目录。
    返回：
        位于工作目录内且存在的 Markdown 文件路径集合。
    """
    paths = set()
    for change in item.changes:
        path = (workdir / change.path).resolve()
        if path.is_relative_to(workdir) and path.suffix.lower() == ".md" and path.is_file():
            paths.add(path)
    return paths

TOOL_LABELS = {
    "commandExecution": "终端命令",
    "fileChange": "文件修改",
    "mcpToolCall": "MCP 工具",
    "webSearch": "网页搜索",
}


class CodexEventHandler:
    """按 SDK 通知类型处理 Codex 流事件。"""

    def __init__(
        self,
        *,
        turn_context: dict[str, object],
        workdir: Path,
        markdown_files: set[Path],
        tool_started: dict[str, float],
        publish_progress: Callable[..., Awaitable[None]],
        publish_response_data: Callable[[dict[str, object]], Awaitable[None]],
        set_phase: Callable[[str], Awaitable[None]],
        updater: TaskUpdater | None,
    ) -> None:
        """初始化事件处理器。

        参数：
            turn_context: 当前 Codex turn 的结构化日志上下文。
            workdir: Codex 工作目录。
            markdown_files: 收集修改过的 Markdown 文件路径集合。
            tool_started: 工具开始时间表，用于计算执行耗时。
            publish_progress: 发布纯文本进度的异步回调。
            publish_response_data: 发布结构化响应事件的异步回调。
            set_phase: 更新当前执行阶段的异步回调。
            updater: 可选的 A2A 更新器，用于补充 artifact 并完成任务。
        """
        self.turn_context = turn_context
        self.workdir = workdir
        self.markdown_files = markdown_files
        self.tool_started = tool_started
        self.publish_progress = publish_progress
        self.publish_response_data = publish_response_data
        self._set_phase = set_phase
        self.updater = updater
        self.final_text = ""
        self.completed = False
        self.generating_reply = False
        self.first_event = True

    async def handle(self, event: object) -> None:
        """根据事件 payload 类型分派到对应处理方法。

        参数：
            event: Codex SDK 流中产生的通知事件。
        返回：
            无返回值；处理过程会更新执行状态或发布事件。
        """
        payload = event.payload
        if self.first_event:
            self.first_event = False
            await self.set_streaming_phase()
        if isinstance(payload, ErrorNotification):
            await self._handle_error(payload)
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
            await self._handle_token_usage(payload)
        elif isinstance(payload, TurnPlanUpdatedNotification):
            await self._handle_plan(payload)
        elif isinstance(payload, AgentMessageDeltaNotification):
            await self._handle_message_delta(payload)
        elif isinstance(payload, (ItemStartedNotification, ItemCompletedNotification)):
            await self._handle_item(payload)
        elif isinstance(payload, TurnCompletedNotification):
            await self._handle_turn_completed(payload)

    async def set_streaming_phase(self) -> None:
        """由上层回调将请求阶段切换为 streaming。

        参数：
            无。
        返回：
            无返回值。
        """
        await self._set_phase("streaming")

    async def _handle_error(self, payload: ErrorNotification) -> None:
        """记录 SDK 错误通知，并在可重试时发布进度。

        参数：
            payload: SDK 错误通知。
        返回：
            无返回值。
        """
        logger.warning(
            "Codex stream reported an error",
            extra={**self.turn_context, "event": "stream_error", "will_retry": payload.will_retry},
        )
        if payload.will_retry:
            await self.publish_progress("Codex 请求遇到错误，正在重试")

    async def _handle_token_usage(self, payload: ThreadTokenUsageUpdatedNotification) -> None:
        """记录 token 使用量更新。

        参数：
            payload: token 使用量通知。
        返回：
            无返回值。
        """
        logger.info(
            "Codex token usage updated",
            extra={**self.turn_context, "event": "token_usage", "usage": payload.token_usage.last.model_dump()},
        )

    async def _handle_plan(self, payload: TurnPlanUpdatedNotification) -> None:
        """记录计划更新并向调用方发布可读计划。

        参数：
            payload: turn 计划更新通知。
        返回：
            无返回值。
        """
        labels = {"pending": "待处理", "inProgress": "进行中", "completed": "已完成"}
        steps = [f"[{labels.get(step.status.value, step.status.value)}] {step.step}" for step in payload.plan]
        logger.info(
            "Codex plan updated",
            extra={**self.turn_context, "event": "plan_updated",
                   "plan_statuses": [step.status.value for step in payload.plan]},
        )
        if steps:
            await self.publish_progress("Codex 计划更新\n" + "\n".join(steps))

    async def _handle_message_delta(self, payload: AgentMessageDeltaNotification) -> None:
        """在模型开始输出回复时发布一次进度。

        参数：
            payload: agent 消息增量通知。
        返回：
            无返回值。
        """
        if payload.delta and not self.generating_reply:
            self.generating_reply = True
            logger.info("Codex reply started", extra={**self.turn_context, "event": "reply_started"})
            await self.publish_progress("Codex 正在生成回复")

    async def _handle_item(self, payload: ItemStartedNotification | ItemCompletedNotification) -> None:
        """处理 item 生命周期事件，并分发消息或工具事件。

        参数：
            payload: item 开始或完成通知。
        返回：
            无返回值。
        """
        item = payload.item.root
        started = isinstance(payload, ItemStartedNotification)
        status = "started" if started else "completed"
        if item.type not in TOOL_LABELS:
            logger.info(
                "Codex item lifecycle",
                extra={**self.turn_context, "event": "item_lifecycle", "message_type": item.type,
                       "item_id": item.id, "status": status},
            )
        if isinstance(item, AgentMessageThreadItem):
            await self._handle_agent_message(item, started)
        elif item.type in TOOL_LABELS:
            await self._handle_tool(item, status)

    async def _handle_agent_message(self, item: AgentMessageThreadItem, started: bool) -> None:
        """处理 agent 消息 item 的最终文本。

        参数：
            item: agent 消息线程项。
            started: 是否为 item 开始事件。
        返回：
            无返回值。
        """
        if started:
            return
        if item.phase != MessagePhase.commentary:
            self.final_text = item.text
        self.generating_reply = False
        if item.text:
            if item.phase != MessagePhase.commentary:
                await self.publish_response_data({"type": "MESSAGE", "text": item.text})
            await self.publish_progress(item.text)

    async def _handle_tool(self, item: object, status: str) -> None:
        """记录工具生命周期并发布结构化工具事件和文本进度。

        参数：
            item: Codex 工具线程项。
            status: 工具状态，取值为 started 或 completed。
        返回：
            无返回值。
        """
        now = time.monotonic()
        if status == "started":
            self.tool_started[item.id] = now
        began = self.tool_started.pop(item.id, None) if status == "completed" else None
        tool_name = item.tool if isinstance(item, McpToolCallThreadItem) else item.type
        status_detail = getattr(getattr(item, "status", None), "value", None)
        exit_code = item.exit_code if isinstance(item, CommandExecutionThreadItem) else None
        logger.info(
            "Codex tool event",
            extra={**self.turn_context, "event": "tool_call", "tool_name": tool_name,
                   "server": item.server if isinstance(item, McpToolCallThreadItem) else None,
                   "message_type": item.type, "tool_use_id": item.id, "status": status,
                   "status_detail": status_detail, "exit_code": exit_code,
                   "duration_ms": round((now - began) * 1000, 2) if began is not None else None},
        )
        if (isinstance(item, FileChangeThreadItem) and status == "completed"
                and item.status == PatchApplyStatus.completed):
            self.markdown_files.update(_markdown_paths(item, self.workdir))
        action = "开始" if status == "started" else "结束"
        label = TOOL_LABELS[item.type]
        if isinstance(item, McpToolCallThreadItem):
            label = f"{label} {item.server}/{item.tool} "
        tool_event: dict[str, object] = {
            "type": "TOOL_EXECUTION_START" if status == "started" else "TOOL_EXECUTION_END",
            "toolCallId": item.id,
        }
        if tool_name:
            tool_event["toolName"] = tool_name
        if isinstance(item, McpToolCallThreadItem) and item.arguments is not None:
            tool_event["arguments"] = item.arguments
        if status == "completed":
            result = getattr(item, "result", None) or getattr(item, "aggregated_output", None)
            if result is not None:
                tool_event["result"] = (
                    result.model_dump(mode="json", by_alias=True)
                    if isinstance(result, McpToolCallResult) else result
                )
            error = getattr(item, "error", None)
            if error is not None:
                tool_event["error"] = (
                    error.model_dump(mode="json", by_alias=True)
                    if hasattr(error, "model_dump") else str(error)
                )
            if status_detail is not None:
                tool_event["status"] = status_detail
            if isinstance(item, CommandExecutionThreadItem):
                tool_event["exitCode"] = item.exit_code
        await self.publish_response_data(tool_event)
        detail = f"（退出码 {exit_code}）" if exit_code else ""
        await self.publish_progress(f"Codex {label}{action}{detail}")

    async def _handle_turn_completed(self, payload: TurnCompletedNotification) -> None:
        """检查 turn 是否成功完成。

        参数：
            payload: turn 完成通知。
        返回：
            无返回值；失败状态会抛出运行时异常。
        """
        logger.info(
            "Codex turn ended",
            extra={**self.turn_context, "event": "turn_ended", "status": payload.turn.status.value},
        )
        if payload.turn.status != TurnStatus.completed:
            detail = payload.turn.error.message if payload.turn.error else payload.turn.status.value
            raise RuntimeError(f"Codex 任务失败：{detail}")
        completion_text = self.final_text or "Codex 执行完成。"
        if self.updater is not None:
            await self.updater.add_artifact(parts=[
                new_text_part(completion_text),
                new_data_part({"type": "MESSAGE", "text": completion_text}),
            ])
            await self.updater.update_status(
                state=TaskState.TASK_STATE_COMPLETED,
                message=new_text_message(completion_text),
            )
        self.completed = True


async def execute_codex(
    working_path: Path,
    instruction: str,
    *,
    session_id: str,
    resume: bool,
    chat: bool,
    branch: str | None,
    log_context: dict[str, object],
    set_phase: Callable[[str], Awaitable[None]],
    publish_progress: Callable[..., Awaitable[None]],
    publish_response_data: Callable[[dict[str, object]], Awaitable[None]],
    set_last_event_method: Callable[[str], None],
    get_mcp_servers: Callable[[], dict[str, dict[str, str | bool]]],
    updater: TaskUpdater | None,
) -> tuple[CodexResult, list[Path]]:
    """建立 Codex 会话、执行 turn，并收集结果和 Markdown 文件。

    参数：
        working_path: Codex 工作目录。
        instruction: 用户任务指令。
        session_id: 服务侧会话标识。
        resume: 是否恢复已有 Codex 会话。
        chat: 是否使用聊天模式。
        branch: coding 会话关联的 Git 分支。
        log_context: 请求级日志上下文。
        set_phase: 更新外层请求阶段的异步回调。
        publish_progress: 发布进度文本的异步回调。
        publish_response_data: 发布结构化响应数据的异步回调。
        set_last_event_method: 保存最近 SDK event 方法名的回调。
        get_mcp_servers: 获取并记录 MCP 服务配置的回调。
    返回：
        Codex 最终文本结果及修改的 Markdown 文件路径。
    """
    workdir = working_path.resolve()
    mode = "chat" if chat else "coding"
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

    config = {"mcp_servers": {} if chat else get_mcp_servers()}
    if chat:
        config.update({
            "web_search": "disabled",
            "features": {"shell_tool": False, "apps": False, "multi_agent": False},
        })
    logger.info(
        "Codex runtime configuration prepared",
        extra={**log_context, "event": "sdk_options_prepared", "model": os.getenv("CODEX_MODEL"),
               "mcp_server_count": len(config["mcp_servers"])},
    )
    gateway_url = os.getenv("CODEX_GATEWAY_BASE_URL")
    if gateway_url:
        if not os.getenv("CODEX_GATEWAY_API_KEY"):
            raise RuntimeError("已配置 Codex 网关，请设置 CODEX_GATEWAY_API_KEY")
        config["model_providers"] = {
            "personal_gateway": {
                "name": "Personal Codex Gateway", "base_url": gateway_url, "wire_api": "responses",
                "requires_openai_auth": False, "supports_websockets": False,
                "env_key": "CODEX_GATEWAY_API_KEY",
            },
        }
    effort = os.getenv("CODEX_REASONING_EFFORT")
    options = dict(
        cwd=str(workdir), model=os.getenv("CODEX_MODEL") or None,
        model_provider="personal_gateway" if gateway_url else None,
        service_tier=os.getenv("CODEX_SERVICE_TIER") or None,
        sandbox=Sandbox.full_access, approval_mode=ApprovalMode.auto_review, config=config,
    )

    markdown_files: set[Path] = set()
    await set_phase("starting_sdk")
    async with AsyncCodex(CodexConfig(cwd=str(workdir), env={"CODEX_HOME": str(codex_home)})) as codex:
        await set_phase("opening_thread")
        thread = await codex.thread_resume(thread_id, **options) if thread_id else await codex.thread_start(**options)
        logger.info("Codex thread ready", extra={**log_context, "event": "thread_ready", "thread_id": thread.id})
        prompt = build_chat_prompt(instruction) if chat else build_coding_prompt(instruction)
        await set_phase("starting_turn")
        turn = await thread.turn(prompt, effort=ReasoningEffort(effort) if effort else None)
        turn_context = {**log_context, "thread_id": thread.id, "turn": turn.id}
        logger.info("Codex turn ready", extra={**turn_context, "event": "turn_ready"})
        await set_phase("waiting_for_first_event")
        handler = CodexEventHandler(
            turn_context=turn_context, workdir=workdir, markdown_files=markdown_files,
            tool_started={}, publish_progress=publish_progress, publish_response_data=publish_response_data,
            set_phase=set_phase, updater=updater,
        )
        try:
            async with aclosing(turn.stream()) as stream:
                async for event in stream:
                    set_last_event_method(event.method)
                    await handler.handle(event)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(turn.interrupt(), timeout=3)
            except Exception:
                logger.debug("Codex turn interruption failed", extra=log_context, exc_info=True)
            raise
        if not handler.completed:
            raise RuntimeError("Codex 未返回任务完成事件")
        if not resume:
            session_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = session_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"scope": scope, "thread_id": thread.id}), encoding="utf-8")
            temporary.replace(session_file)
            logger.info("Codex session saved", extra={**turn_context, "event": "session_saved"})
    return CodexResult(session_id, handler.final_text), sorted(markdown_files)


