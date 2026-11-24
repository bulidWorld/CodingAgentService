"""无仓库聊天任务的执行流程。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.helpers import new_text_message, new_text_part

from coding_agent_service.claude_client import run_claude


class ChatAgent:
    """在独立临时目录中运行一次无状态聊天请求。"""

    async def execute(
        self,
        instruction: str,
        workdir: Path,
        cancel_event: asyncio.Event,
        updater: TaskUpdater,
    ) -> Path:
        """调用 Claude 并将回复作为 A2A artifact 返回。"""
        workdir.mkdir(parents=True, exist_ok=False)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Claude 正在回复"),
        )
        result = await run_claude(workdir, instruction, cancel_event, chat=True)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await updater.add_artifact(parts=[new_text_part(result or "模型没有返回文本内容。")])
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("聊天完成"),
        )
        return workdir
