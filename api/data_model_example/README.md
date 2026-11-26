# A2A JSON-RPC 请求与响应示例

请求示例可作为 `curl -d` 的内容。HTTP 调用还需要设置：

```text
Content-Type: application/json
A2A-Version: 1.0
Authorization: Bearer <A2A_API_TOKEN>
```

请求结构基于 [`../a2a.json`](../a2a.json) 的 A2A `SendMessageRequest` 和 `Message`，并使用 [`../a2a_extend.json`](../a2a_extend.json) 中的 `CodingAgentSendMessageRequest`、`CodingAgentMessage`、`CodingAgentMessagePart` 和 `CodingAgentTaskPayload` 扩展模型。

## `CodingAgentMessagePart` 的含义

`CodingAgentMessagePart` 的 `allOf` 要求一个 part 同时符合：

1. A2A 标准 `Part` 的结构。
2. 服务扩展对 `data` 字段的约束：`data` 是包含任务字段的对象。

因此，标准 part 字段仍然可用，例如 `text`；当 part 使用 `data` 时，该对象应包含服务识别的任务类型和任务字段。`allOf` 不会把 A2A 模型改写成另一种模型，而是要求同一个数据同时通过两组 schema 约束。

## 示例

- [`coding-file-response.json`](coding-file-response.json)：coding 任务完成响应示例；响应放在 `result.task`，代码修改结果和 Git 信息放在 `status.message`，每个生成或修改的 `.md` 文件单独作为 artifact 返回。artifact 的 `name` 和 part 的 `filename` 使用文件名，`mediaType` 为 `text/markdown`，`text` 是不含代码围栏的原始文件内容。
- [`chat-data-part.json`](chat-data-part.json)：chat，通过 `data.message` 传入问题，不需要仓库。
- [`chat-text-part.json`](chat-text-part.json)：任务类型放在 DataPart，问题放在另一个 text part。
- [`chat-json-text-part.json`](chat-json-text-part.json)：把 JSON 对象编码为 text part。
- [`coding-data-part.json`](coding-data-part.json)：coding，通过 `repository`、`branch` 和 `instruction` 提交任务。
- [`coding-repo-url-alias.json`](coding-repo-url-alias.json)：coding 使用 `repo_url` 和 `task` 字段别名。
- [`coding-streaming.json`](coding-streaming.json)：通过 `SendStreamingMessage` 提交 coding 任务。

服务接受 `instruction`、`task`、`prompt`、`message` 作为提示字段，按此顺序取第一个非空值；没有这些字段时使用 text part 的文本。`repository` 也可写成 `repo_url`。未提供 `type` 时服务默认为 `coding`；coding 必须提供仓库地址和 `branch`。

## 延续 Claude 会话

1. Chat 使用 [新建请求](chat-data-part.json) 发起会话，读取 [普通响应](chat-response.json) 中的 `sessionId`，再填入 [续聊请求](chat-resume.json)。
2. Coding 从 [普通响应](coding-file-response.json) 读取 `sessionId`，再填入 [流式续聊请求](coding-streaming-resume.json)。[流式会话事件](coding-streaming-session-event.json) 展示收到的一条 SSE 事件的 `data` 内容。

示例中的 UUID 用于展示字段格式。实际续聊时，请使用服务上一次返回的值，并为每条新消息生成新的 `messageId`。

在 `params.message.parts[].data` 中传入可选的 `sessionId`。省略、传 `null` 或传空白字符串都会新建会话。下一次请求传入返回的 UUID 即可续聊，也可在 JSON text part 中传入该字段。

普通响应在 `result.task.artifacts[].parts[].data.sessionId` 返回会话 ID。流式响应在 `result.artifactUpdate.artifact.parts[].data.sessionId` 返回。该字段由 Claude 会话产生，A2A 的 `taskId` 和 `contextId` 仍按原协议使用。

续聊时使用相同的任务类型，coding 任务还需传入相同的仓库和分支。续聊恢复 Claude 的对话历史，代码工作区仍会按现有规则同步到远端分支。跨服务重启续聊需要保留 Claude 配置目录中的会话记录，并保持工作目录路径一致。
