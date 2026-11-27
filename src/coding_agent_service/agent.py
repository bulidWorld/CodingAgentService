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
from weakref import WeakValueDictionary

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


def _task_fields(context: RequestContext) -> tuple[str, str | None, str | None, str, str | None]:
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

    session_id = payload.get("sessionId")
    if session_id is not None:
        if not isinstance(session_id, str):
            raise TaskInputError("sessionId must be a UUID string or null")
        session_id = session_id.strip() or None
        if session_id is not None:
            try:
                session_id = str(uuid.UUID(session_id))
            except ValueError as exc:
                raise TaskInputError("sessionId must be a UUID string or null") from exc

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
        return task_type, None, None, instruction.strip(), session_id
    if not isinstance(repository, str) or not repository.strip():
        raise TaskInputError("type='coding' requires repository (Git clone URL)")
    if not isinstance(branch, str) or not branch.strip():
        raise TaskInputError("type='coding' requires branch")
    return task_type, repository.strip(), branch.strip(), instruction.strip(), session_id


class AgentRouter(AgentExecutor):
    """根据任务类型分发聊天或代码任务，并统一处理并发、取消和清理。"""

    def __init__(self) -> None:
        self.projects_root = Path(os.environ["PROJECTS_ROOT"]).resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(os.environ["WORKSPACE_ROOT"]).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_TASKS", "2"))))
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._workspace_locks: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()
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
        try:
            task_type, repository, branch, instruction, resume_session_id = _task_fields(context)
            session_id = resume_session_id or str(uuid.uuid4())
            if task_type == "chat":
                # Claude 按工作目录读取会话记录，续聊时使用相同路径。
                workdir = self.workspace / f"chat-{session_id}"
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

            lock = self._workspace_locks.get(workdir)
            if lock is None:
                lock = self._workspace_locks[workdir] = asyncio.Lock()
            async with lock, self._semaphore:
                logger.info(
                    "Starting agent task",
                    extra={"log_type": "business", "event": "task_started", "task_id": task.id,
                           "task_type": task_type, "session_id": session_id,
                           "branch": branch, "workspace": str(workdir)},
                )
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                if task_type == "chat":
                    try:
                        await self.chat_agent.execute(
                            instruction, workdir, cancel_event, updater,
                            session_id=session_id, resume=resume_session_id is not None,
                        )
                    finally:
                        await asyncio.to_thread(shutil.rmtree, workdir, True)
                else:
                    assert repository is not None and branch is not None
                    await self.coding_agent.execute(
                        repository,
                        branch,
                        instruction,
                        workdir,
                        cancel_event,
                        updater,
                        session_id=session_id,
                        resume=resume_session_id is not None,
                    )
        except asyncio.CancelledError:
            logger.info("Agent task cancelled", extra={"log_type": "business", "event": "task_finished",
                        "task_id": task.id, "status_detail": "cancelled"})
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

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """通知正在运行的任务协作式取消。"""
        if context.current_task:
            cancel_event = self._cancel_events.get(context.current_task.id)
            if cancel_event:
                cancel_event.set()
