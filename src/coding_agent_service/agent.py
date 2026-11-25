"""A2A 任务入口：解析请求、路由到对应智能体并管理任务生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from a2a.helpers import (
    get_data_parts,
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

from coding_agent_service.chat_agent import ChatAgent
from coding_agent_service.coding_agent import CodingAgent
from coding_agent_service.errors import TaskInputError

logger = logging.getLogger(__name__)


def _task_fields(context: RequestContext) -> tuple[str, str | None, str | None, str]:
    """从 A2A 消息的文本和 data part 中提取并校验任务参数。"""
    message = context.message
    if message is None:
        raise TaskInputError("A2A message is required")

    payload: dict[str, object] = {}
    # data part 优先于文本 JSON，便于调用方用结构化字段覆盖文本中的默认值。
    for item in get_data_parts(message.parts):
        if isinstance(item, dict):
            payload.update(item)

    text = get_message_text(message).strip()
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                payload = {**decoded, **payload}
        except json.JSONDecodeError:
            pass

    task_type = payload.get("type", "coding")
    if not isinstance(task_type, str) or task_type not in {"chat", "coding"}:
        raise TaskInputError("type must be either 'chat' or 'coding'")

    repository = payload.get("repository") or payload.get("repo_url")
    branch = payload.get("branch")
    instruction = (
        payload.get("instruction")
        or payload.get("task")
        or payload.get("prompt")
        or payload.get("message")
        or text
    )
    if not isinstance(instruction, str) or not instruction.strip():
        raise TaskInputError("Provide a non-empty message/instruction")
    if task_type == "chat":
        return task_type, None, None, instruction.strip()
    if not isinstance(repository, str) or not repository.strip():
        raise TaskInputError("type='coding' requires repository (Git clone URL)")
    if not isinstance(branch, str) or not branch.strip():
        raise TaskInputError("type='coding' requires branch")
    return task_type, repository.strip(), branch.strip(), instruction.strip()


class AgentRouter(AgentExecutor):
    """根据任务类型分发聊天或代码任务，并统一处理并发、取消和清理。"""

    def __init__(self) -> None:
        service_name = os.getenv("SERVICE_NAME", "coding-agent-service")
        self.projects_root = Path(
            os.getenv("PROJECTS_ROOT", f"/var/service/{service_name}/share/projects")
        ).resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(os.getenv("WORKSPACE_ROOT", "./work")).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_TASKS", "2"))))
        self._cancel_events: dict[str, asyncio.Event] = {}
        self.chat_agent = ChatAgent()
        self.coding_agent = CodingAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """执行一项 A2A 任务，并通过 updater 持续发布任务状态。"""
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("正在准备任务"),
        )

        cancel_event = asyncio.Event()
        # cancel() 通过任务 ID 设置事件；执行结束后必须移除，避免字典持续增长。
        self._cancel_events[task.id] = cancel_event
        workdir: Path | None = None
        task_type: str | None = None
        try:
            task_type, repository, branch, instruction = _task_fields(context)
            if task_type == "chat":
                workdir = self.workspace / f"chat-{uuid.uuid4().hex}"
            else:
                assert repository is not None
                parsed = urlparse(repository)
                repo_name = Path(unquote(parsed.path.rstrip("/")).removesuffix(".git")).name
                if not repo_name:
                    repo_name = "repository"
                # URL-derived folder names are human-readable; a suffix avoids collisions
                # between different hosts or owners that use the same repository name.
                repo_key = hashlib.sha256(repository.rstrip("/").removesuffix(".git").encode()).hexdigest()[:10]
                workdir = self.projects_root / f"{repo_name}-{repo_key}"

            async with self._semaphore:
                logger.info(
                    "Starting agent task",
                    extra={"log_type": "business", "task_id": task.id, "task_type": task_type},
                )
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                if task_type == "chat":
                    await self.chat_agent.execute(instruction, workdir, cancel_event, updater)
                else:
                    assert repository is not None and branch is not None
                    await self.coding_agent.execute(
                        repository,
                        branch,
                        instruction,
                        workdir,
                        cancel_event,
                        updater,
                    )
        except asyncio.CancelledError:
            await updater.update_status(
                state=TaskState.TASK_STATE_CANCELED,
                message=new_text_message("任务已取消"),
            )
        except Exception as exc:
            logger.exception("Agent task failed", extra={"log_type": "exception", "task_id": task.id})
            await updater.add_artifact(parts=[new_text_part(f"任务失败：{exc}")])
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("智能体任务失败"),
            )
        finally:
            self._cancel_events.pop(task.id, None)
            # Coding workspaces are persistent projects. Chat workspaces remain ephemeral
            # and are cleaned up after each request.
            if workdir is not None and task_type == "chat" and workdir.exists():
                await asyncio.to_thread(shutil.rmtree, workdir, True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """通知正在运行的任务协作式取消。"""
        if context.current_task:
            cancel_event = self._cancel_events.get(context.current_task.id)
            if cancel_event:
                cancel_event.set()
