"""Claude Agent SDK 调用封装，集中管理工具权限和模型配置。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

logger = logging.getLogger(__name__)


def _log_cli_stderr(line: str) -> None:
    """将 CLI 诊断纳入服务日志。"""
    if line.startswith("[claude-code:unrecognized_model] "):
        # CLI 可能无法识别网关模型名，但仍会发出请求并完成任务。
        logger.debug("Claude Code CLI: %s", line)
    else:
        logger.warning("Claude Code CLI: %s", line)


async def run_claude(
    working_path: Path,
    instruction: str,
    cancel_event: asyncio.Event,
    *,
    chat: bool = False,
) -> str:
    """在指定工作目录执行提示词，返回 SDK 最终结果文本。"""
    options = ClaudeAgentOptions(
        cwd=str(working_path),
        stderr=_log_cli_stderr,
        allowed_tools=[] if chat else ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        setting_sources=[],
        max_turns=int(os.getenv("CLAUDE_MAX_TURNS", "30")),
    )
    # 聊天模式不开放工具；代码模式明确要求聚焦任务且不自行创建提交。
    prompt = instruction if chat else (
        "Fix the reported bug in this repository. Inspect the existing code and relevant tests, "
        "make the smallest complete code change, and run focused checks when appropriate. "
        "Do not create a Git commit; the service will commit your changes. "
        "Do not change unrelated files.\n\n"
        f"Bug/task:\n{instruction}"
    )
    result_text = ""
    async for message in query(prompt=prompt, options=options):
        # SDK 会持续产生过程消息；仅记录最终 ResultMessage 供调用方展示。
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if isinstance(message, ResultMessage) and message.result:
            result_text = message.result
    return result_text
