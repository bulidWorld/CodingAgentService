"""构建 Starlette 应用、A2A 路由和访问控制中间件。"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv
import yaml

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from coding_agent_service.agent import AgentRouter
from coding_agent_service.logging_config import configure_logging

load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """校验 POST 请求的 Bearer token，并记录每个入站请求。"""

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        """为请求生成关联 ID，执行鉴权与下游处理，并记录访问日志。"""
        trace_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.monotonic()
        status = 500
        try:
            if request.method == "POST":
                authorization = request.headers.get("authorization", "")
                supplied = authorization.removeprefix("Bearer ")
                if not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied, self.token):
                    response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = trace_id
            return response
        except Exception:
            logger.exception(
                "Unhandled HTTP request error",
                extra={"log_type": "exception", "trace_id": trace_id, "method": request.method, "path": request.url.path},
            )
            raise
        finally:
            logger.info(
                "HTTP request",
                extra={
                    "log_type": "access", "trace_id": trace_id, "method": request.method,
                    "path": request.url.path, "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )


def create_app() -> Starlette:
    """读取 Agent Card 配置并创建带认证的 A2A Starlette 应用。"""
    token = os.getenv("A2A_API_TOKEN", "").strip()
    if len(token) < 24:
        raise RuntimeError("A2A_API_TOKEN must be configured with at least 24 characters")

    base_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    card_path = Path(__file__).resolve().parents[2] / "api" / "agent-card.yaml"
    card_data = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    public_skills = [AgentSkill(**skill) for skill in card_data["skills"]]
    extended_skills = [AgentSkill(**skill) for skill in card_data["extended_skills"]]
    scheme_name = card_data["security_scheme_name"]
    security_scheme = SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer")
    )
    security_requirements = [
        SecurityRequirement(schemes={name: StringList(list=scopes) for name, scopes in requirement.items()})
        for requirement in card_data["security_requirements"]
    ]
    capabilities = AgentCapabilities(**card_data["capabilities"])

    def make_card(skills: list[AgentSkill]) -> AgentCard:
        """用同一份服务元数据构造指定技能集合的 Agent Card。"""
        return AgentCard(
            name=card_data["name"],
            description=card_data["description"],
            version=card_data["version"],
            default_input_modes=card_data["default_input_modes"],
            default_output_modes=card_data["default_output_modes"],
            capabilities=capabilities,
            security_schemes={scheme_name: security_scheme},
            security_requirements=security_requirements,
            supported_interfaces=[
                AgentInterface(protocol_binding="JSONRPC", url=base_url, protocol_version="1.0")
            ],
            skills=skills,
        )

    public_card = make_card(public_skills)
    extended_card = make_card(public_skills + extended_skills)
    handler = DefaultRequestHandler(
        agent_executor=AgentRouter(),
        task_store=InMemoryTaskStore(),
        agent_card=public_card,
        extended_agent_card=extended_card,
    )
    routes = [*create_agent_card_routes(public_card), *create_jsonrpc_routes(handler, "/")]
    return Starlette(routes=routes, middleware=[Middleware(BearerTokenMiddleware, token=token)])


app = create_app()
