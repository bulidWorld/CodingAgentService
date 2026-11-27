"""Codex 任务提示词。"""

from __future__ import annotations


def build_coding_prompt(instruction: str) -> str:
    """将用户任务包装为代码修改任务提示词。"""
    return (
        "Fix the reported bug in this repository. Inspect the existing code and relevant tests, "
        "make the smallest complete code change, and run focused checks when appropriate. "
        "Do not create a Git commit; the service will commit your changes. "
        "Do not change unrelated files.\n\n"
        f"Bug/task:\n{instruction}"
    )


def build_chat_prompt(instruction: str) -> str:
    """聊天模式直接使用用户提供的提示词。"""
    return instruction
