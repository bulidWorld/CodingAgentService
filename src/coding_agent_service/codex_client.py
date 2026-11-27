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
    AgentMessageThreadItem,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    PatchApplyStatus,
    ReasoningEffort,
    TurnCompletedNotification,
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
    on_message: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[CodexResult, list[Path]]:
    """执行一轮 Codex 请求，返回最终回复和 Markdown 文件路径。"""
    workdir = working_path.resolve()
    mode = "chat" if chat else "coding"
    started = time.monotonic()
    log_context = {"log_type": "llm", "provider": "codex", "session_id": session_id, "mode": mode}

    async def execute() -> tuple[CodexResult, list[Path]]:
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
            sandbox=Sandbox.read_only if chat else Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
            config=config,
        )

        markdown_files: set[Path] = set()
        final_text = ""
        completed = False
        async with AsyncCodex(CodexConfig(cwd=str(workdir), env={"CODEX_HOME": str(codex_home)})) as codex:
            thread = (
                await codex.thread_resume(thread_id, **options)
                if thread_id else await codex.thread_start(**options)
            )
            prompt = build_chat_prompt(instruction) if chat else build_coding_prompt(instruction)
            turn = await thread.turn(prompt, effort=ReasoningEffort(effort) if effort else None)
            try:
                async with aclosing(turn.stream()) as stream:
                    async for event in stream:
                        payload = event.payload
                        if isinstance(payload, (ItemStartedNotification, ItemCompletedNotification)):
                            item = payload.item.root
                            if isinstance(item, AgentMessageThreadItem):
                                if isinstance(payload, ItemCompletedNotification):
                                    if item.phase != MessagePhase.commentary:
                                        final_text = item.text
                                    if on_message and item.text:
                                        await on_message(item.text)
                            elif item.type in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
                                status = "started" if isinstance(payload, ItemStartedNotification) else "completed"
                                logger.info(
                                    "Codex tool event",
                                    extra={**log_context, "event": "tool_call", "tool_name": item.type,
                                           "tool_use_id": item.id, "status": status},
                                )
                                if (
                                    isinstance(item, FileChangeThreadItem)
                                    and status == "completed"
                                    and item.status == PatchApplyStatus.completed
                                ):
                                    markdown_files.update(_markdown_paths(item, workdir))
                        elif isinstance(payload, TurnCompletedNotification):
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
        return CodexResult(session_id, final_text), sorted(markdown_files)

    if cancel_event.is_set():
        raise asyncio.CancelledError
    logger.info("Codex request started", extra={**log_context, "event": "request_started", "resume": resume})
    request = asyncio.create_task(execute())
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        while True:
            done, _ = await asyncio.wait({request, cancelled}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
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
            logger.info(
                "Codex SDK stream is still running",
                extra={**log_context, "event": "stream_wait_heartbeat",
                       "duration_ms": round((time.monotonic() - started) * 1000, 2)},
            )
    except asyncio.CancelledError:
        logger.info("Codex request cancelled", extra={**log_context, "event": "request_cancelled"})
        raise
    except Exception:
        logger.exception("Codex request failed", extra={**log_context, "event": "request_failed"})
        raise
    finally:
        request.cancel()
        cancelled.cancel()
        await asyncio.gather(request, cancelled, return_exceptions=True)
