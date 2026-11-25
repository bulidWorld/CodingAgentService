"""代码任务流程：校验远程仓库、克隆、调用模型并提交工作区变更。"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

from a2a.server.tasks import TaskUpdater
from a2a.types import Artifact, Part, TaskArtifactUpdateEvent, TaskState
from a2a.helpers import new_text_message, new_text_part
from claude_agent_sdk import ResultMessage

from coding_agent_service.claude_client import run_claude
from coding_agent_service.errors import TaskInputError

logger = logging.getLogger(__name__)


def _markdown_description(content: str, filename: str) -> str:
    """从 Markdown 正文提取简短描述，供 A2A artifact.description 使用。"""
    for paragraph in re.split(r"\n\s*\n", content.strip()):
        paragraph = re.sub(r"^#+\s*", "", paragraph.strip())
        paragraph = re.sub(r"[`*_>#]", "", paragraph).strip()
        if paragraph:
            return paragraph[:160]
    return f"Markdown 文件：{filename}"


async def _add_markdown_artifact(
    updater: TaskUpdater, path: Path, content: str
) -> None:
    """按 A2A 文件 part 格式返回 Markdown 原文。"""
    filename = path.name
    artifact = Artifact(
        artifact_id=str(uuid.uuid4()),
        name=filename,
        description=_markdown_description(content, filename),
        parts=[Part(text=content, filename=filename, media_type="text/markdown")],
    )
    await updater.event_queue.enqueue_event(
        TaskArtifactUpdateEvent(
            task_id=updater.task_id,
            context_id=updater.context_id,
            artifact=artifact,
            last_chunk=True,
        )
    )


def _validate_repository(repository: str) -> None:
    """限制仓库地址格式和目标主机，降低凭据泄漏及访问内网的风险。"""
    if "\n" in repository or "\r" in repository or repository.startswith("-"):
        raise TaskInputError("Invalid repository URL")

    parsed = urlparse(repository)
    if parsed.scheme not in {"https", "http", "ssh"} or not parsed.hostname:
        scp_match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):([^\s]+)", repository)
        if not scp_match:
            raise TaskInputError("Repository must be an HTTP(S), SSH, or SCP-style Git URL")
        hostname = scp_match.group(1)
    else:
        if parsed.username or parsed.password:
            raise TaskInputError("Do not put credentials in the repository URL; configure Git credentials on the service")
        hostname = parsed.hostname

    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise TaskInputError("Local repository URLs are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    ):
        raise TaskInputError("Private and local IP addresses are not allowed as repository hosts")
    allowed_hosts = {
        host.strip().lower()
        for host in os.getenv("ALLOWED_GIT_HOSTS", "").split(",")
        if host.strip()
    }
    if allowed_hosts and hostname.lower() not in allowed_hosts:
        raise TaskInputError(f"Git host is not allowed: {hostname}")


def _git(cwd: Path | None, *args: str, timeout: int = 180) -> str:
    """运行非交互 Git 命令；失败时保留有限长度的错误详情。"""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {detail[-3000:]}")
    return result.stdout.strip()


def _clone_repository(repository: str, branch: str, destination: Path) -> None:
    """克隆指定分支，并按出站请求规范记录耗时和状态。"""
    parsed = urlsplit(repository)
    remote_label = f"{parsed.hostname}{parsed.path}" if parsed.hostname else re.sub(r"^.*@", "", repository)
    started = time.monotonic()
    status = 500
    try:
        if destination.exists():
            if not (destination / ".git").exists():
                raise RuntimeError(f"Repository directory already exists and is not a Git repository: {destination}")
            remote = _git(destination, "remote", "get-url", "origin")
            if remote.rstrip("/").removesuffix(".git") != repository.rstrip("/").removesuffix(".git"):
                raise RuntimeError(f"Repository directory belongs to a different remote: {destination}")
            _git(destination, "fetch", "--no-tags", "origin", branch)
            _git(destination, "checkout", branch)
            _git(destination, "reset", "--hard", f"origin/{branch}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _git(None, "clone", "--no-tags", "--single-branch", "--branch", branch, repository, str(destination))
        status = 200
    except Exception:
        logger.exception("Git repository clone failed", extra={"log_type": "exception", "path": remote_label})
        raise
    finally:
        logger.info(
            "Git repository clone",
            extra={
                "log_type": "http",
                "method": "GIT",
                "path": remote_label,
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )


class CodingAgent:
    """克隆指定分支、执行代码修改，并将变更提交到本地仓库。"""

    async def execute(
        self,
        repository: str,
        branch: str,
        instruction: str,
        workdir: Path,
        cancel_event: asyncio.Event,
        updater: TaskUpdater,
    ) -> Path:
        """完成代码任务；无变更时直接返回结果，有变更时创建本地提交。"""
        _validate_repository(repository)
        if branch.startswith("-"):
            raise TaskInputError("Invalid branch name")
        await asyncio.to_thread(_git, None, "check-ref-format", "--branch", branch)

        await asyncio.to_thread(_clone_repository, repository, branch, workdir)
        logger.info(
            "Coding workspace ready",
            extra={"log_type": "business", "workspace": str(workdir), "branch": branch},
        )
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Claude 正在分析并修改代码"),
        )
        async def forward_message(message: object) -> None:
            if isinstance(message, ResultMessage):
                return
            await updater.update_status(
                state=TaskState.TASK_STATE_WORKING,
                message=new_text_message(str(message)),
            )

        result, markdown_paths = await run_claude(
            workdir, instruction, cancel_event, on_message=forward_message
        )
        if cancel_event.is_set():
            raise asyncio.CancelledError

        markdown_contents: list[tuple[Path, str]] = []
        for path in markdown_paths:
            if path.is_file():
                content = await asyncio.to_thread(path.read_text, encoding="utf-8")
                markdown_contents.append((path, content))

        status = await asyncio.to_thread(_git, workdir, "status", "--porcelain")
        # 以 porcelain 格式判断工作区是否有变更，避免解析面向人的 Git 输出。
        if not status:
            summary = result or "Claude 未生成代码变更；仓库保持不变。"
            for path, content in markdown_contents:
                await _add_markdown_artifact(updater, path, content)
            await updater.update_status(
                state=TaskState.TASK_STATE_COMPLETED,
                message=new_text_message(f"{summary}\n\n任务完成，没有需要提交的代码变更"),
            )
            return workdir

        await asyncio.to_thread(_git, workdir, "add", "--all")
        staged = await asyncio.to_thread(_git, workdir, "diff", "--cached", "--name-only")
        if not staged:
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("工作区存在变更，但没有可提交的暂存内容"),
            )
            return workdir

        # 通过单次 git -c 设置提交身份，不修改工作区的持久配置。
        author = os.getenv("GIT_AUTHOR_NAME", "Coding Agent")
        email = os.getenv("GIT_AUTHOR_EMAIL", "coding-agent@example.invalid")
        prefix = os.getenv("GIT_COMMIT_PREFIX", "fix")
        commit_title = re.sub(r"\s+", " ", instruction).strip()[:65] or "fix bug"
        await asyncio.to_thread(
            _git,
            workdir,
            "-c",
            f"user.name={author}",
            "-c",
            f"user.email={email}",
            "commit",
            "-m",
            f"{prefix}: {commit_title}",
        )
        commit_sha = await asyncio.to_thread(_git, workdir, "rev-parse", "HEAD")
        changed_files = staged.splitlines()
        summary = result or "代码修改已完成并提交。"
        for path, content in markdown_contents:
            await _add_markdown_artifact(updater, path, content)
        status_message = (
            f"{summary}\n\n分支：{branch}\nCommit：{commit_sha}\n修改文件：\n"
            + "\n".join(f"- {name}" for name in changed_files)
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message(status_message),
        )
        logger.info("Repository bug fix committed", extra={"log_type": "business", "commit_sha": commit_sha})
        return workdir
