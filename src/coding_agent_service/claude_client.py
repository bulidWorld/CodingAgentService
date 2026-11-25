"""Claude Agent SDK 调用封装，集中管理工具权限和模型配置。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query
from coding_agent_service.prompts import build_chat_prompt, build_coding_prompt

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
    on_message: Callable[[Any], Any] | None = None,
) -> tuple[str, list[Path]]:
    """执行提示词并返回最终文本及执行期间写入的 Markdown 文件路径。"""
    markdown_files: set[Path] = set()

    async def capture_markdown_file(
        hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        """记录 Write/Edit 工具操作的 Markdown 文件，内容稍后从磁盘读取。"""
        tool_name = hook_input.get("tool_name")
        tool_input = hook_input.get("tool_input", {})
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
        stderr=_log_cli_stderr,
        allowed_tools=[] if chat else ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        setting_sources=[],
        max_turns=int(os.getenv("CLAUDE_MAX_TURNS", "30")),
        hooks=None if chat else {
            "PostToolUse": [HookMatcher(hooks=[capture_markdown_file])],
        },
    )
    # 聊天模式不开放工具；代码模式明确要求聚焦任务且不自行创建提交。
    prompt = build_chat_prompt(instruction) if chat else build_coding_prompt(instruction)
    result_text = ""
    async for message in query(prompt=prompt, options=options):
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if on_message is not None:
            callback_result = on_message(message)
            if asyncio.iscoroutine(callback_result):
                await callback_result
        if isinstance(message, ResultMessage) and message.result:
            result_text = message.result
    return result_text, sorted(markdown_files)
