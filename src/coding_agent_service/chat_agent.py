"""无仓库聊天任务的执行流程。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState
from a2a.helpers import new_text_message, new_text_part

from coding_agent_service.claude_client import run_claude
from claude_agent_sdk import ResultMessage


class ChatAgent:
    """在会话对应的临时目录中运行聊天请求。"""

    async def execute(
        self,
        instruction: str,
        workdir: Path,
        cancel_event: asyncio.Event,
        updater: TaskUpdater,
        *,
        session_id: str,
        resume: bool,
    ) -> Path:
        """调用 Claude 并将回复作为 A2A artifact 返回。"""
        workdir.mkdir(parents=True, exist_ok=False)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Claude 正在回复"),
        )
        async def forward_message(message: object) -> None:
            if isinstance(message, ResultMessage):
                return
            await updater.update_status(
                state=TaskState.TASK_STATE_WORKING,
                message=new_text_message(str(message)),
            )

        result, _markdown_paths = await run_claude(
            workdir, instruction, cancel_event,
            session_id=session_id, resume=resume, chat=True, on_message=forward_message,
        )
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await updater.add_artifact(parts=[
            new_text_part(result.result or "模型没有返回文本内容。"),
            Part(data={"sessionId": result.session_id}),
        ])
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("聊天完成"),
        )
        return workdir
