# 飞书生成任务 Agent 设计规格

日期：2026-07-17

## 1. 背景

现有 `ai-generation-portable-apps` 已经实现 Nano Banana 图生图和 Seedance 图生视频能力，但本项目不接入现有 Portal，也不依赖现有子应用进程。新项目是一个完全独立、仅在本机运行的单用户 Agent：读取飞书需求文档，理解其中的正文、表格和参考图片，拆解为图生图或图生视频任务，经人工审批后执行，并创建新的飞书文档交付产物。

除完成业务任务外，项目还有明确的学习目标：使用 LangGraph 构建可持久化、可观察、可暂停恢复的工作流；使用 LangChain 接入模型、结构化输出和工具边界。

## 2. 目标

1. 用户在本地页面粘贴飞书文档链接后，应用自动读取文档块、表格和图片。
2. Claude Vision 理解图片内容，DeepSeek V4 Pro 理解需求并输出结构化计划。
3. Agent 只生成两类任务：图生图和图生视频。
4. 任何付费生成调用发生前，LangGraph 必须暂停等待人工审批。
5. 用户可以勾选任务、调整提示词、参数和图片用途，再批准执行。
6. 图生图通过 Chiyun Gemini `generateContent` 兼容接口执行。
7. 图生视频通过火山方舟官方 Seedance 接口执行。
8. 本地产物下载并校验后，应用创建新的飞书文档交付图片、视频和执行记录。
9. 任务、审批、供应商任务 ID 和产物状态持久化，应用重启后可以继续。
10. 首版以本地链接提交为入口，但输入边界允许后续增加飞书机器人触发。

## 3. 非目标

首版不实现：

- 接入现有 Portal 或注册为现有平台标签页。
- 依赖现有 Nano Banana、Seedance、Dreamina 子应用进程。
- 局域网服务、公网服务、HTTPS、账号系统或多用户隔离。
- 文生图、文生视频、视频剪辑、视频拼接、专业配音或后期混音。
- 先图生图再自动把结果作为视频关键帧的隐式流水线。
- 定时监控飞书文件夹。
- 飞书机器人消息触发；仅保留扩展接口。
- 让模型自由循环调用付费生成工具。
- 自动创建或自动配置飞书企业应用；权限由用户在飞书网页完成。
- LangSmith 默认上传追踪数据。

## 4. 已确认决策

| 项目 | 决策 |
|---|---|
| 运行形态 | 完全独立、本机单用户应用，仅绑定 `127.0.0.1` |
| 首版入口 | 本地页面粘贴飞书文档链接 |
| 未来入口 | 飞书机器人消息触发，复用同一工作流 |
| 工作流 | LangGraph 显式状态图 |
| 模型与工具封装 | LangChain |
| 规划模型 | `deepseek-v4-pro`，开启 thinking，`reasoning_effort=high` |
| 视觉模型 | 沿用本机 `rag_agent` 的 Anthropic 兼容 Claude Vision 通道设计 |
| 图生图 | Chiyun Gemini `generateContent` 兼容通道，由用户提供 Key |
| 图生视频 | 火山方舟官方 Seedance，由用户提供 Ark API Key |
| 视频分镜 | 一张分镜表整理为一个 Seedance 多镜头任务，不本地拼接 |
| 视频参考图 | 文档参考图直接传给 Seedance，不先生成关键帧 |
| 审批 | 付费调用前审批；可勾选部分任务或一键批准全部 |
| 交付 | 本地保存，同时创建新的飞书交付文档 |
| 飞书鉴权 | 应用身份，App ID + App Secret，使用 tenant access token |
| LangSmith | 代码预留，默认关闭，通过环境变量显式开启 |
| 状态存储 | SQLite Checkpointer + 应用业务表 |

## 5. 技术架构

### 5.1 组件

- `web`：FastAPI 本地页面和 JSON API。
- `graph`：LangGraph 状态、节点、条件边和中断恢复。
- `domain`：Pydantic 领域模型和业务校验。
- `integrations`：飞书、Claude Vision、DeepSeek、Chiyun、Seedance 客户端。
- `storage`：LangGraph SQLite Checkpointer、业务索引和文件记录。
- `outputs`：图片、视频、文档图片缓存和临时上传文件。

FastAPI 只作为本地交互外壳。业务流程不能写在路由函数中；路由只负责创建工作流、读取状态、提交审批决定和展示结果。

### 5.2 建议目录

```text
feishu-generation-agent/
├── pyproject.toml
├── uv.lock
├── .env.example
├── README.md
├── src/feishu_generation_agent/
│   ├── main.py
│   ├── config.py
│   ├── domain/
│   │   ├── document.py
│   │   ├── plan.py
│   │   ├── task.py
│   │   └── artifact.py
│   ├── graph/
│   │   ├── state.py
│   │   ├── builder.py
│   │   ├── nodes.py
│   │   └── routing.py
│   ├── integrations/
│   │   ├── feishu.py
│   │   ├── vision.py
│   │   ├── deepseek.py
│   │   ├── chiyun.py
│   │   └── seedance.py
│   ├── storage/
│   │   ├── checkpoints.py
│   │   ├── repository.py
│   │   └── files.py
│   └── web/
│       ├── app.py
│       └── static/
│           ├── index.html
│           ├── app.js
│           └── styles.css
├── tests/
│   ├── fixtures/
│   ├── unit/
│   ├── graph/
│   └── integration/
├── data/
└── outputs/
```

`data/`、`outputs/`、`.env` 和所有凭证文件必须加入 `.gitignore`。

## 6. LangGraph 工作流

### 6.1 节点

```text
START
  → ingest_source
  → normalize_document
  → analyze_images
  → plan_requirements
  → audit_plan
  → validate_plan
  → human_approval (interrupt)
      ├─ reject_with_feedback → plan_requirements
      ├─ cancel_all → END
      └─ approve_selected → revalidate_approval
  → check_source_revision
      ├─ changed → ingest_source
      └─ unchanged → execute_selected_tasks
  → verify_and_download_artifacts
  → deliver_to_feishu
  → END
```

### 6.2 中断与恢复

`human_approval` 使用 LangGraph `interrupt()`。中断载荷只包含可 JSON 序列化的计划、风险、任务和素材引用。浏览器提交审批时，通过相同 `thread_id` 和 `Command(resume=...)` 恢复。

审批决定支持三种动作：

- `approve`：提交选中的任务及用户修改。
- `reject`：附带反馈，返回规划节点重新生成。
- `cancel`：不执行任何任务，结束工作流。

LangGraph 恢复中断时会重新执行整个节点，因此中断前不能执行外部副作用；所有副作用节点必须幂等。

### 6.3 Graph State

每个源文档运行使用唯一 `thread_id`，状态至少包含：

```text
run_id
thread_id
source_url
source_type
source_token
document_id
document_title
document_revision
normalized_document
media_assets
vision_descriptions
draft_plan
audit_report
validation_issues
approval_decision
approved_tasks
execution_records
artifacts
delivery_record
status
last_error
```

Graph State 不存储 API Key、App Secret、访问令牌或完整响应头。

## 7. 飞书文档读取与标准化

### 7.1 链接解析

支持：

- `/docx/<token>` 新版文档链接。
- `/wiki/<token>` 知识库节点链接；先解析节点的 `obj_token` 和 `obj_type`，只接受 `docx`。

其他文档类型以可操作错误返回，不自动猜测转换。

### 7.2 Block 模型

不能只读取纯文本。标准化结果保留下列信息：

- `block_id`、`parent_id`、`block_type`、文档顺序和层级路径。
- 标题、正文、列表、引用、高亮和可见文本。
- 表格行列、单元格位置和单元格内的子 Block。
- 图片 Block 的文件 Token、宽高和在文档中的位置。
- 分隔线和章节边界。

标准化结构同时生成一个供模型阅读的文本视图，但文本视图中的每一段和每张图必须带稳定引用，例如 `[block:b123]`、`[image:i2]`，使模型输出可追溯到源 Block。

### 7.3 图片下载

本机 `rag_agent` 的图片处理模式作为参考，但新项目不形成运行依赖。区别如下：

- `rag_agent` 下载的是飞书消息资源；本项目下载的是飞书文档 Image Block 对应素材。
- 本项目必须识别真实 MIME 类型，不能把所有图片都标记成 PNG。
- 每张图片计算内容哈希并缓存到运行目录；相同内容不重复下载或分析。
- 图片下载失败时保留 Block 和 Token，并把任务标记为阻塞，不静默忽略。

## 8. 图片双表示与视觉理解

DeepSeek V4 Pro 是文本模型，不能直接读取图片。每张参考图在系统内保留两种表示：

1. 原始图片文件：传给 Chiyun 或 Seedance。
2. Claude Vision 结构化描述：传给 DeepSeek 规划和审查。

视觉描述模型输出：

```text
asset_id
subjects
scene
style
composition
characters
actions
visible_text
colors
probable_role
uncertainties
```

视觉提示词要求模型只描述可见内容，不补充剧情。审批页面同时展示原图和描述，用户可以修改 `probable_role` 或补充说明。修改后的描述写回审批状态，但不覆盖原始模型输出，便于追踪。

## 9. 需求规划与审查

### 9.1 规划

DeepSeek V4 Pro 接收：

- 标准化文档文本。
- 表格结构。
- 图片描述。
- 图片在文档中的位置。
- 支持的任务类型、字段和供应商限制。

调用参数：

- 模型：`deepseek-v4-pro`。
- Thinking：开启。
- `reasoning_effort`：`high`。
- JSON Output：开启。
- LangChain 使用 Pydantic 结构化输出；若供应商原生结构化输出兼容性不足，则使用 Tool Strategy 或显式 JSON 修复流程。

### 9.2 任务类型

只允许：

- `image_to_image`
- `image_to_video`

模型不得输出文生图、文生视频或剪辑任务。无法归入两种类型的需求必须进入阻塞问题。

### 9.3 TaskPlan

每项任务包含：

```text
task_id
task_type
title
source_block_ids
user_intent
prompt
negative_constraints
reference_images
aspect_ratio
image_size
duration
resolution
generate_audio
output_count
confidence
assumptions
warnings
blocking_issues
```

字段约束：

- 图生图和图生视频都至少需要一张参考图。
- 图生图使用 `image_size`，不使用 `duration`、`resolution` 或 `generate_audio`。
- 图生视频使用 `duration` 和 `resolution`，不使用 `image_size`；参数必须满足当前 Seedance 官方模型限制。
- `output_count` 必须是受配置上限约束的正整数。
- `blocking_issues` 非空时任务不能批准。
- 所有 `reference_images` 必须引用已下载的 `asset_id`。

### 9.4 图片归属

按以下优先级推断：

1. 文本明确引用“图片 1/2/3”。
2. 图片位于“参考图”“素材”等标题下。
3. 图片与任务或表格位于同一章节。
4. 图片在文档顺序上距离任务最近。
5. 仍无法确定时产生阻塞问题，由用户在审批页指定。

### 9.5 文档格式

- 自由叙述型：整理为一个任务，保留动作顺序、风格和全局参数。
- 分镜表型：把多行分镜合并为一个 Seedance 多镜头任务，按时间段保留镜头、运镜、动作和音效。
- 混合文档：按章节和语义边界拆成多个任务。

### 9.6 独立审查

第二次 DeepSeek 调用作为审查者，检查：

- 是否遗漏任务或素材。
- 图片用途是否与文档一致。
- 时长、分辨率、比例和生成次数是否冲突。
- 是否擅自增加角色、剧情、品牌或关键动作。
- 是否存在供应商不支持的参数组合。
- 是否应产生阻塞问题而规划模型没有标出。

审查输出不能直接修改计划；它提供问题和修正建议，由确定性节点合并或退回规划。

## 10. 审批界面

已确认布局：左侧为 LangGraph 执行轨迹，右侧为审批工作区。

左侧展示：

- 当前节点。
- 已完成、运行中、等待和失败状态。
- 每个节点的耗时和简短摘要。
- 当前 `thread_id` 和恢复状态。

右侧展示：

- 源文档标题、链接和版本。
- 全部任务及勾选框。
- 任务类型、置信度、假设、警告和阻塞问题。
- 可编辑提示词、负面约束和参数。
- 参考图片缩略图、视觉描述、用途和顺序。
- 退回重新规划、全部取消、批准所选任务。

审批后必须重新执行 Pydantic 和业务规则校验。浏览器提交的结构不能直接传给供应商。

## 11. 外部适配器

### 11.1 接口边界

```text
DocumentSource
VisionAnalyzer
RequirementPlanner
ImageGenerator
VideoGenerator
DeliveryWriter
```

每个接口都有真实实现和 Fake 实现。Graph 测试只能使用 Fake 实现。

### 11.2 Chiyun 图生图

参考现有 `nano-banana` 的 Gemini `generateContent` 协议实现，但不导入原模块。适配器负责：

- 参考图片编码和请求构造。
- 模型、比例、尺寸、生成数量和提示词。
- 解析 URL 或 Base64 图片结果。
- 下载、验证并返回统一 `Artifact`。

Chiyun Key 由用户配置。真实模型名通过配置设置，默认值在本地真实冒烟测试前由 `/models` 或最小能力请求确认，不硬编码未验证的模型名称。

### 11.3 火山方舟 Seedance

参考现有 `seedance` 的官方 Ark 提交、轮询、素材引用和结果下载逻辑，但不导入原模块。适配器负责：

- 多张参考图上传或编码。
- 把图片按确定顺序传递，并在提示词中使用“图片 1/2/3”引用。
- 创建官方 Seedance 任务。
- 持久化任务 ID。
- 轮询终态并下载视频。
- 返回统一 `Artifact`。

本项目不支持视频或音频参考素材，只支持图片参考。

### 11.4 飞书交付

应用使用 App ID 和 App Secret 获取 tenant access token。用户在飞书网页为应用配置以下能力：

- 读取新版文档。
- 查看知识库节点信息。
- 下载文档图片素材。
- 创建和编辑新版文档。
- 上传图片和附件到新版文档。
- 添加文档协作者。

启动配置检查通过实际只读接口和能力探针报告缺失权限，不依赖硬编码权限名称猜测。

## 12. 执行、幂等与恢复

### 12.1 串行执行

首版对批准任务串行执行，避免并发费用失控。一个任务失败后继续后续任务。

### 12.2 幂等标识

每个外部副作用使用稳定的内部键：

```text
run_id + task_id + operation
```

执行前查询业务表：

- 已存在供应商任务 ID：继续查询，不重复提交。
- 已存在并通过哈希校验的产物：跳过下载。
- 已创建交付文档：更新原文档，不重复创建。
- 已上传飞书素材：复用素材 Token。

### 12.3 源文档版本

审批前保存飞书文档版本。执行前重新读取最新版本：

- 版本未变：继续。
- 版本变化：清除审批结果，重新读取、规划和审批。

## 13. 产物校验与存储

文件目录：

```text
outputs/<run_id>/<task_id>/
```

每个产物记录：

```text
artifact_id
task_id
kind
local_path
mime_type
size
sha256
provider_url
provider_task_id
feishu_file_token
status
```

下载后校验：

- HTTP 状态和 Content-Type。
- 文件非空且未超过配置上限。
- 图片或视频文件头与声明类型一致。
- SHA-256 写入记录。
- 已有文件必须重新校验哈希后才能复用。

## 14. 飞书交付文档

标题格式：

```text
[AI 交付] <源文档标题> - YYYY-MM-DD HH:mm
```

内容：

1. 原始需求文档链接和版本。
2. 执行摘要。
3. 每项任务的最终提示词、参数和参考图映射。
4. 参考图片缩略图。
5. 生成图片原图。
6. 生成视频附件。
7. 失败任务、错误原因和重试建议。

20 MB 以内素材直接上传；更大的视频使用飞书分片上传素材流程。首版配置固定 `LARK_OUTPUT_OWNER_OPEN_ID`，创建文档后将该用户添加为可编辑协作者。未来机器人入口使用消息发送者 Open ID。

交付失败不能改变生成任务的成功状态。用户可以只重试交付，不重新生成。

## 15. 错误与重试

错误分类：

- `configuration_error`：缺 Key、App Secret、模型名或目录配置。
- `permission_error`：飞书、Chiyun、DeepSeek、Claude 或 Ark 权限错误。
- `document_error`：链接、类型、Block 或素材错误。
- `validation_error`：计划、审批或供应商参数不合法。
- `transient_error`：超时、连接、429 或服务端 5xx。
- `provider_terminal_error`：供应商任务明确失败。
- `delivery_error`：飞书创建、上传或授权失败。

重试策略：

- 网络、429 和可重试 5xx：指数退避，记录次数和下一次时间。
- DeepSeek 空 JSON 或结构错误：把校验错误反馈给模型重试一次；仍失败则暂停人工处理。
- 供应商终态失败：不自动重新提交付费任务。
- 权限或配置错误：不自动重试，等待配置修复。
- 飞书交付失败：允许独立重试。

任何错误都必须带用户可读说明、技术详情和是否可重试标记。

## 16. 安全与隐私

- 服务仅监听 `127.0.0.1`。
- `.env`、数据库、缓存和产物不提交 Git。
- API Key、App Secret 和访问令牌不进入 Graph State、LangGraph Checkpoint、日志或 LangSmith。
- 日志不得记录 Base64 图片、完整下载 URL 的签名查询串或 Authorization Header。
- LangSmith 默认关闭。开启前界面提示飞书文档内容、模型输入和节点状态可能上传到第三方追踪服务。
- 删除运行会删除本地文档缓存、图片、视频、业务记录和对应 LangGraph Checkpoint；飞书交付文档不自动删除。
- SQLite Checkpointer 禁用 pickle fallback；Graph State 只允许受控的 JSON/MessagePack 可序列化类型，避免从不受信任数据库反序列化任意对象。

## 17. 可观察性

本地页面展示：

- LangGraph 节点状态、开始时间、结束时间和耗时。
- 规划模型、视觉模型和生成供应商名称。
- 请求次数、重试次数和供应商任务 ID。
- 经过脱敏的输入摘要和结构化输出。
- 当前等待人工、等待外部任务或等待重试的原因。

LangSmith 通过环境变量开启，不是运行必需依赖。关闭 LangSmith 时，全部功能和本地轨迹仍可用。

## 18. 测试策略

### 18.1 单元测试

不访问外网，覆盖：

- 飞书 docx 与 wiki 链接解析。
- Block、表格和图片顺序还原。
- 图片 MIME、大小、哈希和缓存。
- Claude Vision 描述解析。
- DeepSeek 结构化计划和业务校验。
- 图片匹配规则。
- Chiyun、Seedance 请求和响应解析。
- 飞书直接上传和分片上传。
- 错误分类和重试判断。

### 18.2 Graph 测试

使用 InMemorySaver 或测试 SQLite 和全部 Fake 适配器，覆盖：

- 运行到审批中断后暂停。
- 批准、修改、拒绝和取消。
- 只执行勾选任务。
- 文档版本变化后重新规划。
- 单项失败后继续。
- 重启后从 Checkpoint 恢复。
- 已有供应商任务 ID 时不重复提交。
- 交付失败时不重新生成。

### 18.3 本地集成测试

使用脱敏的真实响应样本和本地 HTTP 模拟，覆盖：

- 自由叙述文档。
- 分镜表文档。
- 飞书分页、图片下载和权限错误。
- DeepSeek 空输出、非法 JSON 和结构修复。
- Chiyun URL/Base64 图片返回。
- Seedance 创建、轮询、成功和失败。
- 大于 20 MB 视频的飞书分片上传。

### 18.4 真实冒烟测试

只有用户明确确认后运行：

1. 读取专用飞书测试文档。
2. Claude Vision 描述一至两张参考图。
3. DeepSeek 生成并审查计划。
4. 验证拒绝和恢复，不生成产物。
5. 批准一个最低成本图生图任务。
6. 批准一个最短可用时长 Seedance 任务。
7. 创建飞书交付文档并上传产物。
8. 中途重启应用，确认没有重复提交。

## 19. 验收标准

- 所有自动化测试通过。
- 未审批时生成接口调用次数为零。
- 每个批准任务最多产生一次有效供应商提交。
- 重启后任务、审批状态和产物记录不丢失。
- 两类示例飞书文档均能生成可编辑任务计划。
- 缺失或用途不明的图片会阻塞执行，不静默猜测。
- 新飞书交付文档可由配置的用户打开和编辑。
- 图片和视频在本地及飞书中正常查看。
- 飞书交付失败可以单独重试。
- LangSmith关闭时无追踪数据外发；开启时可查看完整节点轨迹。
- README 包含配置、飞书权限、启动、测试和故障排查。

## 20. 未来升级为飞书机器人

首版输入统一为：

```text
RequirementRequest {
  source_url,
  requester_open_id,
  trigger_type,
  reply_context
}
```

本地页面创建 `trigger_type=local_link` 请求。未来新增 `FeishuBotSource`：通过飞书 WebSocket 长连接接收消息，提取文档链接和发送者 Open ID，构造相同请求并启动同一 LangGraph。机器人只新增入口和通知层，不修改文档解析、规划、审批、执行或交付节点。

未来机器人审批可以使用飞书交互卡片恢复同一个 `thread_id`；本地审批页面继续保留作为管理和调试入口。
