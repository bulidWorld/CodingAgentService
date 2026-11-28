"""Codex 客户端间共享的数据模型和配置。"""

from dataclasses import dataclass

MCP_SERVERS = {
    "operator-agent-service": "OPERATOR_AGENT_SERVICE",
    "pod-debug-service": "POD_DEBUG_SERVICE",
    "ci-build-service": "CI_BUILD_SERVICE",
}


@dataclass(frozen=True)
class CodexResult:
    """Codex 一轮执行的结果。

    属性：
        session_id: 服务侧会话标识。
        text: Codex 最终回复文本。
    """

    session_id: str
    text: str
