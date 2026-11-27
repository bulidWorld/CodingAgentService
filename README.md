# Coding Agent Service

基于官方 Python `a2a-sdk` 的 A2A HTTP JSON-RPC 服务。服务支持两种任务：`coding` 接收仓库地址、分支和 bug 描述，克隆指定分支并调用 Claude Agent SDK 修改代码、创建本地 Git commit；`chat` 不需要代码仓库，直接与模型对话。Agent Card 元数据维护在 [`agent-card.yaml`](api/agent-card.yaml)，服务启动时由 SDK 类型构建协议卡片。

## 启动

要求 Python 3.11+、Git、Claude Code CLI（Claude Agent SDK 运行时需要）和可用的 Claude 凭据。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

设置至少 24 个字符的 `A2A_API_TOKEN`，并配置 `ANTHROPIC_API_KEY`，或者为服务进程提供 Claude Code 已登录的凭据。私有 Git 仓库需要在服务运行环境中配置 SSH key 或 Git credential helper；不要把凭据放进任务 URL。

加载 `.env` 可使用 `python-dotenv`，或由 systemd、容器编排平台等注入环境变量。然后启动：

```bash
uvicorn coding_agent_service.server:app --app-dir src --host 0.0.0.0 --port 8000
```

卡片地址为 `GET /.well-known/agent-card.json`。所有 `POST` A2A JSON-RPC 请求都必须带 `A2A-Version: 1.0` 和 `Authorization: Bearer <A2A_API_TOKEN>`。服务支持 A2A streaming，长任务可通过 `SendStreamingMessage` 接收状态事件。生产环境应通过 HTTPS 暴露服务。

## Agent 能力发现

| 接口 | 请求方式 | 作用 | 认证 |
| --- | --- | --- | --- |
| Agent Card | `GET /.well-known/agent-card.json` | 获取公开能力描述 | 不需要 |
| `GetExtendedAgentCard` | A2A JSON-RPC 2.0 | 获取扩展能力描述 | Bearer Token |

请求扩展卡片示例：

```bash
curl http://localhost:8000/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -H "Authorization: Bearer $A2A_API_TOKEN" \
  -d '{"jsonrpc":"2.0","id":"capabilities-1","method":"GetExtendedAgentCard","params":{}}'
```

公开卡片声明支持 `extendedAgentCard`；扩展卡片额外描述了使用服务端 Git 凭据访问私有仓库的能力。能力描述来自 YAML 配置，并由 A2A SDK 提供的请求处理器通过 RPC 返回。

启动前设置 `PROJECTS_ROOT` 和 `WORKSPACE_ROOT`。Coding 仓库保存在 `PROJECTS_ROOT` 下，首次任务会克隆，后续任务会复用仓库并更新指定分支或 tag。`WORKSPACE_ROOT` 用于服务工作目录。需要指定 Claude 配置目录时，设置 `CLAUDE_CONFIG_DIR`，Claude Code 会从环境中读取。Docker 镜像已设置这三个目录。其他常用配置包括 `PUBLIC_BASE_URL`、`MAX_CONCURRENT_TASKS`、`CLAUDE_MODEL`、`CLAUDE_MAX_TURNS`、`GIT_AUTHOR_NAME`、`GIT_AUTHOR_EMAIL` 和 `ALLOWED_GIT_HOSTS`。

## 提交任务

使用 A2A 1.0 `SendMessage` JSON-RPC 方法。消息 `data` part 中的 `type` 为 `coding` 或 `chat`。`coding` 必须提供 `repository`、`branch`、`tag` 和 `instruction`；服务检出分支的远端最新代码，`tag` 仅作为任务信息记录，不影响检出的版本。`chat` 可只提供 `message`，不需要仓库地址。两类任务示例见 [`curl.example`](curl.example)。

在同一个 data part 中传入可选的 `sessionId`。省略或传空值会新建 Claude 会话，传入响应 artifact 的 `data.sessionId` 会续聊。普通和流式响应的字段位置见[会话示例](api/data_model_example/README.md#延续-claude-会话)。

```bash
curl http://localhost:8000/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -H "Authorization: Bearer $A2A_API_TOKEN" \
  -d '{
    "jsonrpc":"2.0",
    "id":"bug-001",
    "method":"SendMessage",
    "params":{
      "message":{
        "messageId":"bug-001",
        "role":"ROLE_USER",
        "parts":[
          {"data":{"type":"coding","repository":"https://github.com/example/project.git","branch":"main","tag":"v1.0.0","instruction":"修复解析空配置时的空指针异常，并补充回归测试。"}}
        ]
      }
    }
  }'
```

SDK 负责 A2A 任务状态和 JSON-RPC 格式。任务完成后响应 artifact 会包含 Claude 摘要、commit SHA 和修改文件。日志以 JSON 输出到控制台，区分 access、http、business 和 exception 类型。当前使用 SDK 的 `InMemoryTaskStore`；服务进程重启后任务状态不保留，水平扩容前需要换成持久化 TaskStore。服务只在本地创建 commit，不会自动 push 到远端。

## 开发

依赖由 `pyproject.toml` 管理，也可以使用 `uv sync`。Agent Card 将服务声明为 A2A 1.0 JSON-RPC 接口。
