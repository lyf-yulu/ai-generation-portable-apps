# 飞书生成任务 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `feishu-generation-agent/` 中构建一个完全独立的本地应用，读取飞书需求文档和参考图片，使用 Claude Vision 与 DeepSeek 生成可审批的图生图/图生视频计划，审批后调用 Chiyun 和官方 Seedance，并把本地产物交付到新飞书文档。

**Architecture:** FastAPI 只负责本地页面和命令入口；LangGraph 负责从文档读取、视觉分析、规划、审查、人工中断、付费执行到交付的持久化工作流。所有外部系统通过小型 Protocol 端口接入，业务状态使用 SQLite，LangGraph Checkpoint 使用独立 SQLite 文件，生成文件按 `run_id/task_id` 保存。

**Tech Stack:** Python 3.12、uv、FastAPI、Pydantic v2、LangChain 1.3、LangGraph 1.2、langgraph-checkpoint-sqlite 3.1、httpx、aiosqlite、Pillow、原生 HTML/CSS/JavaScript、pytest。

## Global Constraints

- 新应用目录固定为 `feishu-generation-agent/`，不得导入或启动现有 Portal、Nano Banana、Seedance、Dreamina 模块。
- 首版只实现本地链接入口；`RequirementRequest` 和 `DocumentSource` 不得依赖浏览器路由，为未来 `FeishuBotSource` 和交互卡片恢复同一 `thread_id` 保留边界，但本计划不实现机器人。
- 运行时只绑定 `127.0.0.1`，默认端口 `8765`；首版不提供局域网、公网、HTTPS、登录或多用户能力。
- 使用 `/opt/homebrew/bin/python3.12`，依赖由 uv 安装到新应用自己的 `.venv`；不改变仓库根 `requirements.txt`。
- 前端必须是浏览器原生 HTML/CSS/JavaScript，不增加 Node.js、npm 或构建流程。
- DeepSeek 模型固定为 `deepseek-v4-pro`，Thinking 开启，`reasoning_effort=high`；DeepSeek 只接收文本和结构化图片描述。
- Claude Vision 只描述可见内容；原始图片直接传给 Chiyun 或 Seedance，视觉描述只供规划与审批使用。
- 任务类型只允许 `image_to_image` 和 `image_to_video`，两类任务都至少需要一张参考图片。
- 一张分镜表必须合并为一个 Seedance 多镜头任务，不生成独立片段，不做本地视频拼接。
- 任何 Chiyun 或 Seedance 付费提交必须发生在 LangGraph `interrupt()` 审批恢复之后；自动化测试不得访问真实付费接口。
- 审批页必须允许勾选部分任务，并允许增添、替换、删除、排序参考图以及修改图片用途。
- 批准前后都运行 Pydantic 与业务规则校验；存在 `blocking_issues` 的任务不能执行。
- 执行前检查飞书源文档版本；版本变化必须清除审批并重新读取、规划和审批。
- 任务串行执行；一个任务失败后继续后续任务；供应商终态失败不得自动重新提交。
- 外部副作用幂等键固定为 `run_id + task_id + operation`；恢复时优先继续已有供应商任务或已有交付文档。
- API Key、App Secret、访问令牌、签名 URL 和图片 Base64 不得进入 Graph State、Checkpoint、日志或 LangSmith。
- `LANGGRAPH_STRICT_MSGPACK=true`；Graph State 只存 JSON/MessagePack 可序列化的字典、列表、字符串、数字、布尔值和空值。
- LangSmith 默认关闭；只有 `LANGSMITH_TRACING=true` 时启用，并在界面显示数据外发提醒。
- `data/`、`outputs/`、`.env`、数据库、缓存、测试临时文件和真实 API 响应必须被新应用的 `.gitignore` 排除。
- 所有提交只包含当前任务列出的文件；不得顺手修复仓库现有 4 个基线测试失败。

---

## File Structure

### 新建应用文件

- `feishu-generation-agent/pyproject.toml` — Python 版本、运行依赖、测试依赖和 pytest 配置。
- `feishu-generation-agent/uv.lock` — uv 生成的可复现依赖锁。
- `feishu-generation-agent/.env.example` — 全部配置名称和安全默认值，不含真实凭证。
- `feishu-generation-agent/.gitignore` — 运行状态、产物、凭证和虚拟环境排除规则。
- `feishu-generation-agent/README.md` — 本地配置、飞书权限、启动、测试、恢复和冒烟操作手册。
- `feishu-generation-agent/src/feishu_generation_agent/config.py` — `Settings`、路径初始化和按能力检查配置。
- `feishu-generation-agent/src/feishu_generation_agent/domain/document.py` — 文档 Block、媒体素材、标准化文档和输入请求模型。
- `feishu-generation-agent/src/feishu_generation_agent/domain/plan.py` — 任务、计划、审查、审批和验证规则。
- `feishu-generation-agent/src/feishu_generation_agent/domain/artifact.py` — 供应商提交、执行记录、产物和交付记录。
- `feishu-generation-agent/src/feishu_generation_agent/domain/errors.py` — 可序列化错误分类与重试属性。
- `feishu-generation-agent/src/feishu_generation_agent/ports.py` — 文档、视觉、规划、生成和交付 Protocol。
- `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py` — 业务 SQLite schema、运行、事件、操作和产物记录。
- `feishu-generation-agent/src/feishu_generation_agent/storage/files.py` — 安全路径、MIME、哈希、缓存、上传和下载校验。
- `feishu-generation-agent/src/feishu_generation_agent/storage/checkpoints.py` — 严格序列化的 AsyncSqliteSaver 生命周期。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py` — tenant token、分页、统一错误和能力探针。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_source.py` — docx/wiki 解析、Block 标准化和图片下载。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/vision.py` — Claude Vision 结构化描述与哈希缓存。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py` — DeepSeek 规划、独立审查和一次 JSON 修复。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/chiyun.py` — Gemini `generateContent` 图生图适配器。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/seedance.py` — Ark Seedance 创建、查询和结果解析适配器。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_delivery.py` — 新建交付文档、直接/分片上传和协作者授权。
- `feishu-generation-agent/src/feishu_generation_agent/graph/state.py` — 纯 JSON `AgentState`。
- `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py` — 节点实现和 `GraphServices` 依赖。
- `feishu-generation-agent/src/feishu_generation_agent/graph/builder.py` — 节点、边和条件路由编译。
- `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py` — 创建运行、恢复审批、查询状态和后台执行。
- `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py` — 本地 API 请求/响应模型。
- `feishu-generation-agent/src/feishu_generation_agent/web/app.py` — FastAPI 路由和生命周期。
- `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html` — 链接提交、轨迹和审批工作区。
- `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js` — 轮询、编辑、素材上传和审批交互。
- `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css` — 双栏审批布局和状态样式。
- `feishu-generation-agent/src/feishu_generation_agent/main.py` — 只绑定本机的启动入口。
- `feishu-generation-agent/src/feishu_generation_agent/cli/__init__.py` — 命令行工具包。
- `feishu-generation-agent/src/feishu_generation_agent/cli/config_probe.py` — 不付费的飞书/模型能力检查。
- `feishu-generation-agent/src/feishu_generation_agent/cli/smoke.py` — 需要显式确认参数的真实端到端冒烟入口。

### 新建测试文件

- `feishu-generation-agent/tests/conftest.py` — 临时 Settings、SQLite 和 Fake 适配器夹具。
- `feishu-generation-agent/tests/unit/test_config.py`
- `feishu-generation-agent/tests/unit/test_domain.py`
- `feishu-generation-agent/tests/unit/test_storage.py`
- `feishu-generation-agent/tests/unit/test_feishu_source.py`
- `feishu-generation-agent/tests/unit/test_vision.py`
- `feishu-generation-agent/tests/unit/test_planner.py`
- `feishu-generation-agent/tests/unit/test_chiyun.py`
- `feishu-generation-agent/tests/unit/test_seedance.py`
- `feishu-generation-agent/tests/unit/test_feishu_delivery.py`
- `feishu-generation-agent/tests/graph/test_approval_graph.py`
- `feishu-generation-agent/tests/graph/test_execution_graph.py`
- `feishu-generation-agent/tests/integration/test_api.py`
- `feishu-generation-agent/tests/integration/test_restart_recovery.py`
- `feishu-generation-agent/tests/fixtures/feishu_docx_blocks.json`
- `feishu-generation-agent/tests/fixtures/feishu_storyboard_blocks.json`
- `feishu-generation-agent/tests/fixtures/chiyun_inline_response.json`
- `feishu-generation-agent/tests/fixtures/seedance_succeeded.json`

## Delivery Milestones

- **里程碑 A（Task 1–7）— 可审查计划后端：** 能用 Fake 外部系统把两种飞书样本文档运行到持久化审批中断；未审批付费调用为零。
- **里程碑 B（Task 8–11）— 本地可用生成应用：** 有双栏审批页面、素材调整、Chiyun/Seedance 适配器、串行幂等执行和本地产物；真实接口仍由显式配置控制。
- **里程碑 C（Task 12–14）— 完整交付与恢复：** 能创建飞书交付文档、分片上传视频、重启恢复、单独重试交付，并提供配置探针、操作手册和付费冒烟门禁。

---

### Task 1: 建立独立 Python 应用与安全配置

**Files:**
- Create: `feishu-generation-agent/pyproject.toml`
- Create: `feishu-generation-agent/.env.example`
- Create: `feishu-generation-agent/.gitignore`
- Create: `feishu-generation-agent/src/feishu_generation_agent/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/config.py`
- Create: `feishu-generation-agent/tests/unit/test_config.py`
- Create: `feishu-generation-agent/uv.lock`

**Interfaces:**
- Consumes: 无。
- Produces: `Settings`, `Settings.ensure_paths() -> None`, `Settings.require(*field_names: str) -> None`。

- [ ] **Step 1: 写失败测试，固定本机绑定、路径和按能力校验**

```python
from pathlib import Path

import pytest

from feishu_generation_agent.config import Settings


def test_settings_are_local_and_create_runtime_paths(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", outputs_dir=tmp_path / "outputs")
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8765
    settings.ensure_paths()
    assert settings.data_dir.is_dir()
    assert settings.outputs_dir.is_dir()


def test_require_reports_missing_secret_names():
    settings = Settings(deepseek_api_key=None, ark_api_key=None)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY, ARK_API_KEY"):
        settings.require("deepseek_api_key", "ark_api_key")
```

- [ ] **Step 2: 创建 `pyproject.toml` 和空包，运行测试确认导入失败**

```toml
[project]
name = "feishu-generation-agent"
version = "0.1.0"
description = "Local LangGraph agent for Feishu image and video generation requirements"
requires-python = ">=3.12,<3.13"
dependencies = [
  "aiosqlite>=0.21,<1",
  "fastapi>=0.115,<1",
  "httpx>=0.28,<1",
  "langchain>=1.3,<1.4",
  "langchain-anthropic>=1,<2",
  "langchain-openai>=1,<2",
  "langgraph>=1.2,<1.3",
  "langgraph-checkpoint-sqlite>=3.1,<3.2",
  "pillow>=11,<13",
  "pydantic-settings>=2.7,<3",
  "python-multipart>=0.0.20,<1",
  "uvicorn[standard]>=0.34,<1",
]

[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/feishu_generation_agent"]

[dependency-groups]
dev = [
  "pytest>=8,<10",
  "pytest-asyncio>=0.25,<2",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]
```

Run: `cd feishu-generation-agent && uv sync && uv run pytest tests/unit/test_config.py -q`

Expected: FAIL，提示无法导入 `feishu_generation_agent.config` 或找不到 `Settings`。

- [ ] **Step 3: 实现 `Settings`**

```python
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_host: Literal["127.0.0.1"] = "127.0.0.1"
    app_port: int = 8765
    data_dir: Path = Path("data")
    outputs_dir: Path = Path("outputs")
    business_db_path: Path = Path("data/agent.sqlite3")
    checkpoint_db_path: Path = Path("data/checkpoints.sqlite3")

    lark_app_id: str | None = None
    lark_app_secret: SecretStr | None = None
    lark_output_owner_open_id: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    claude_api_key: SecretStr | None = None
    claude_base_url: str | None = None
    claude_model: str | None = None
    chiyun_api_key: SecretStr | None = None
    chiyun_base_url: str = "https://chiyun.work"
    chiyun_model: str | None = None
    ark_api_key: SecretStr | None = None
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    seedance_model: str = "doubao-seedance-2-0-260128"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "feishu-generation-agent-local"
    max_output_count: int = 4
    max_download_bytes: int = 500 * 1024 * 1024

    def ensure_paths(self) -> None:
        for path in (self.data_dir, self.outputs_dir, self.business_db_path.parent, self.checkpoint_db_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    def require(self, *field_names: str) -> None:
        missing = [name.upper() for name in field_names if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(", ".join(missing))
```

- [ ] **Step 4: 写入安全配置样例与忽略规则**

`.env.example` 写入以下完整内容：

```dotenv
APP_HOST=127.0.0.1
APP_PORT=8765
DATA_DIR=data
OUTPUTS_DIR=outputs
BUSINESS_DB_PATH=data/agent.sqlite3
CHECKPOINT_DB_PATH=data/checkpoints.sqlite3
LARK_APP_ID=
LARK_APP_SECRET=
LARK_OUTPUT_OWNER_OPEN_ID=
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
CLAUDE_API_KEY=
CLAUDE_BASE_URL=
CLAUDE_MODEL=
CHIYUN_API_KEY=
CHIYUN_BASE_URL=https://chiyun.work
CHIYUN_MODEL=
ARK_API_KEY=
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
SEEDANCE_MODEL=doubao-seedance-2-0-260128
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=feishu-generation-agent-local
LANGGRAPH_STRICT_MSGPACK=true
```

`.gitignore` 必须包含：

```gitignore
.env
.venv/
data/
outputs/
.pytest_cache/
__pycache__/
*.pyc
tests/.tmp/
```

- [ ] **Step 5: 生成锁文件并验证测试**

Run: `cd feishu-generation-agent && uv lock && uv run pytest tests/unit/test_config.py -q`

Expected: `2 passed`，并生成 `uv.lock`。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/pyproject.toml feishu-generation-agent/uv.lock feishu-generation-agent/.env.example feishu-generation-agent/.gitignore feishu-generation-agent/src/feishu_generation_agent/__init__.py feishu-generation-agent/src/feishu_generation_agent/config.py feishu-generation-agent/tests/unit/test_config.py
git commit -m "build(agent): bootstrap standalone Python application"
```

---

### Task 2: 定义领域模型、验证规则与外部端口

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/document.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/plan.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/artifact.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/errors.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/ports.py`
- Create: `feishu-generation-agent/tests/unit/test_domain.py`

**Interfaces:**
- Consumes: `Settings.max_output_count`。
- Produces: `RequirementRequest`, `NormalizedDocument`, `MediaAsset`, `VisionDescription`, `GenerationTask`, `TaskPlan`, `AuditReport`, `ApprovalDecision`, `ProviderSubmission`, `Artifact`, `DeliveryRecord` 及六个 Protocol。

- [ ] **Step 1: 写失败测试，固定任务类型与阻塞规则**

```python
import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.plan import GenerationTask, TaskPlan


def task_payload(task_type: str) -> dict:
    return {
        "task_id": "task-1",
        "task_type": task_type,
        "title": "熊猫拉抽屉",
        "source_block_ids": ["block-1"],
        "user_intent": "保持角色一致并完成动作",
        "prompt": "熊猫拉开抽屉，彩球滚出",
        "reference_images": [{"asset_id": "asset-1", "role": "reference_image", "order": 1}],
        "aspect_ratio": "9:16",
        "output_count": 1,
    }


def test_image_task_requires_image_size_and_rejects_video_fields():
    payload = task_payload("image_to_image")
    payload["image_size"] = "2K"
    assert GenerationTask.model_validate(payload).image_size == "2K"
    payload["duration"] = 10
    with pytest.raises(ValidationError, match="duration"):
        GenerationTask.model_validate(payload)


def test_video_task_requires_duration_and_resolution():
    payload = task_payload("image_to_video")
    payload.update(duration=10, resolution="720p", generate_audio=True)
    task = GenerationTask.model_validate(payload)
    assert task.duration == 10


def test_blocking_task_cannot_be_approved():
    payload = task_payload("image_to_image")
    payload.update(image_size="2K", blocking_issues=["图片用途不明确"])
    plan = TaskPlan(tasks=[GenerationTask.model_validate(payload)])
    with pytest.raises(ValueError, match="blocking"):
        plan.approved_subset(["task-1"], max_output_count=4)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_domain.py -q`

Expected: FAIL，提示 `domain.plan` 不存在。

- [ ] **Step 3: 实现文档与计划模型**

`document.py` 必须包含以下字段和枚举：

```python
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    DOCX = "docx"
    WIKI = "wiki"


class RequirementRequest(BaseModel):
    source_url: str
    requester_open_id: str | None = None
    trigger_type: str = "local_link"
    reply_context: dict[str, str] = Field(default_factory=dict)


class DocumentBlock(BaseModel):
    block_id: str
    parent_id: str | None
    block_type: str
    order: int
    path: list[str]
    text: str = ""
    table_row: int | None = None
    table_column: int | None = None
    image_asset_id: str | None = None


class MediaAsset(BaseModel):
    asset_id: str
    source_block_id: str
    origin: str
    file_token: str | None = None
    local_path: Path
    mime_type: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    download_error: str | None = None


class VisionDescription(BaseModel):
    asset_id: str
    subjects: list[str]
    scene: str
    style: str
    composition: str
    characters: list[str]
    actions: list[str]
    visible_text: list[str]
    colors: list[str]
    probable_role: str
    uncertainties: list[str]


class NormalizedDocument(BaseModel):
    document_id: str
    title: str
    revision: int
    source_type: SourceType
    source_token: str
    blocks: list[DocumentBlock]
    text_view: str
    media_assets: list[MediaAsset]
    ingest_issues: list[str] = Field(default_factory=list)
```

`plan.py` 使用 `model_validator(mode="after")` 强制：图生图只允许 `image_size`；图生视频只允许 `duration/resolution/generate_audio`；参考图非空；`output_count >= 1`；`approved_subset()` 拒绝阻塞任务、未知 ID 和超过配置上限的数量。

`plan.py` 的公共字段固定如下，后续任务不得另起同义字段：

```python
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class TaskType(StrEnum):
    IMAGE_TO_IMAGE = "image_to_image"
    IMAGE_TO_VIDEO = "image_to_video"


class ImageReference(BaseModel):
    asset_id: str
    role: str
    order: int = Field(ge=1)


class GenerationTask(BaseModel):
    task_id: str
    task_type: TaskType
    title: str
    source_block_ids: list[str]
    user_intent: str
    prompt: str
    negative_constraints: list[str] = Field(default_factory=list)
    reference_images: list[ImageReference]
    aspect_ratio: str
    image_size: str | None = None
    duration: int | None = None
    resolution: str | None = None
    generate_audio: bool | None = None
    output_count: int = Field(default=1, ge=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    tasks: list[GenerationTask]
    document_summary: str = ""


class AuditReport(BaseModel):
    issues: list[str] = Field(default_factory=list)
    corrections_required: bool = False


class ApprovalDecision(BaseModel):
    action: Literal["approve", "reject", "cancel"]
    selected_task_ids: list[str] = Field(default_factory=list)
    tasks: list[GenerationTask] = Field(default_factory=list)
    feedback: str | None = None
```

`artifact.py` 的公共字段固定如下：

```python
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ProviderResult(BaseModel):
    url: str | None = None
    base64_data: str | None = None
    mime_type: str


class ProviderSubmission(BaseModel):
    provider: str
    provider_task_id: str
    status: str
    result_items: list[ProviderResult] = Field(default_factory=list)
    error_message: str | None = None


class ExecutionRecord(BaseModel):
    task_id: str
    provider: str
    provider_task_id: str | None = None
    status: str
    error: dict[str, object] | None = None


class Artifact(BaseModel):
    artifact_id: str
    task_id: str
    kind: Literal["image", "video"]
    local_path: Path
    mime_type: str
    size: int
    sha256: str
    provider_url: str | None = None
    provider_task_id: str | None = None
    feishu_file_token: str | None = None
    status: str


class DeliveryRecord(BaseModel):
    document_id: str
    document_url: str
    status: str
    uploaded_artifact_ids: list[str] = Field(default_factory=list)
```

`errors.py` 使用统一的可序列化错误载荷：

```python
from enum import StrEnum

from pydantic import BaseModel


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration_error"
    PERMISSION = "permission_error"
    DOCUMENT = "document_error"
    VALIDATION = "validation_error"
    TRANSIENT = "transient_error"
    PROVIDER_TERMINAL = "provider_terminal_error"
    DELIVERY = "delivery_error"


class ErrorDetail(BaseModel):
    category: ErrorCategory
    message: str
    technical_detail: str
    retryable: bool


class AgentError(RuntimeError):
    def __init__(self, detail: ErrorDetail):
        super().__init__(detail.message)
        self.detail = detail
```

- [ ] **Step 4: 实现产物、错误和 Protocol**

```python
from enum import StrEnum
from typing import Protocol

from feishu_generation_agent.domain.artifact import Artifact, DeliveryRecord, ProviderSubmission
from feishu_generation_agent.domain.document import MediaAsset, NormalizedDocument, RequirementRequest, VisionDescription
from feishu_generation_agent.domain.plan import AuditReport, GenerationTask, TaskPlan


class DocumentSource(Protocol):
    async def ingest(self, request: RequirementRequest) -> NormalizedDocument:
        raise NotImplementedError
    async def get_revision(self, source_url: str) -> int:
        raise NotImplementedError


class VisionAnalyzer(Protocol):
    async def analyze(self, asset: MediaAsset) -> VisionDescription:
        raise NotImplementedError


class RequirementPlanner(Protocol):
    async def plan(self, document: NormalizedDocument, descriptions: list[VisionDescription], feedback: str | None) -> TaskPlan:
        raise NotImplementedError
    async def audit(self, document: NormalizedDocument, plan: TaskPlan) -> AuditReport:
        raise NotImplementedError


class ImageGenerator(Protocol):
    async def submit(self, task: GenerationTask, assets: list[MediaAsset]) -> ProviderSubmission:
        raise NotImplementedError
    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        raise NotImplementedError


class VideoGenerator(Protocol):
    async def submit(self, task: GenerationTask, assets: list[MediaAsset]) -> ProviderSubmission:
        raise NotImplementedError
    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        raise NotImplementedError


class DeliveryWriter(Protocol):
    async def deliver(self, document: NormalizedDocument, plan: TaskPlan, artifacts: list[Artifact]) -> DeliveryRecord:
        raise NotImplementedError
```

- [ ] **Step 5: 运行领域测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_domain.py -q`

Expected: `3 passed`。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain feishu-generation-agent/src/feishu_generation_agent/ports.py feishu-generation-agent/tests/unit/test_domain.py
git commit -m "feat(agent): define generation domain and adapter ports"
```

---

### Task 3: 建立业务 SQLite 与文件校验层

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/files.py`
- Create: `feishu-generation-agent/tests/unit/test_storage.py`

**Interfaces:**
- Consumes: `MediaAsset`, `Artifact`, `Settings.business_db_path`, `Settings.outputs_dir`。
- Produces: `Repository.open()`, `create_run()`, `append_event()`, `get_operation()`, `save_operation()`, `save_artifact()`, `list_events()`；`FileStore.save_input()`, `save_download()`, `validate()`。

- [ ] **Step 1: 写失败测试，固定幂等操作和内容寻址缓存**

```python
from pathlib import Path

from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


async def test_operation_is_unique_by_run_task_and_name(tmp_path: Path):
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    await repo.save_operation("run-1", "task-1", "submit", "provider-123", "submitted")
    await repo.save_operation("run-1", "task-1", "submit", "provider-123", "submitted")
    operation = await repo.get_operation("run-1", "task-1", "submit")
    assert operation["provider_id"] == "provider-123"
    assert await repo.count_operations() == 1
    await repo.close()


def test_same_image_bytes_reuse_hash_path(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    first = store.save_input("run-1", "ref.png", png)
    second = store.save_input("run-1", "copy.png", png)
    assert first.sha256 == second.sha256
    assert first.local_path == second.local_path
    assert first.mime_type == "image/png"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_storage.py -q`

Expected: FAIL，提示 `storage.files` 或 `storage.repository` 不存在。

- [ ] **Step 3: 实现 SQLite schema 和原子 upsert**

`Repository.open()` 必须执行以下表结构；SQL 参数只能使用占位参数，不能拼接用户输入：

```sql
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL UNIQUE,
  source_url TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  node TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  provider_id TEXT,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id, operation)
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  artifact_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vision_cache (
  cache_key TEXT PRIMARY KEY,
  description_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`save_operation()` 使用 `INSERT ... ON CONFLICT(run_id, task_id, operation) DO UPDATE`；`append_event()` 的 `summary` 在写入前移除 Bearer Token、`token=` 查询参数和超过 500 字符的内容。

- [ ] **Step 4: 实现安全文件存储**

`FileStore.save_input()` 计算 SHA-256，使用 Pillow 验证图片，目录固定为 `data/runs/<run_id>/inputs/<sha256>.<ext>`；`save_download()` 先写同目录 `.part` 文件，检查 HTTP Content-Type、配置大小上限和文件头，完成后使用 `Path.replace()` 原子改名。任何调用方提供的文件名只用于显示，不参与目录拼接。

- [ ] **Step 5: 运行测试并检查数据库没有重复行**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_storage.py -q`

Expected: `2 passed`。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/storage feishu-generation-agent/tests/unit/test_storage.py
git commit -m "feat(agent): persist runs operations and verified files"
```

---

### Task 4: 读取并标准化飞书 docx/wiki 文档

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_source.py`
- Create: `feishu-generation-agent/tests/fixtures/feishu_docx_blocks.json`
- Create: `feishu-generation-agent/tests/fixtures/feishu_storyboard_blocks.json`
- Create: `feishu-generation-agent/tests/unit/test_feishu_source.py`

**Interfaces:**
- Consumes: `RequirementRequest`, `NormalizedDocument`, `FileStore`, `Settings.lark_app_id`, `Settings.lark_app_secret`。
- Produces: `parse_feishu_url(url: str) -> tuple[SourceType, str]`, `FeishuClient`, `FeishuDocumentSource.ingest()`, `FeishuDocumentSource.get_revision()`。

- [ ] **Step 1: 写失败测试，覆盖 docx/wiki、分页、表格和图片顺序**

```python
import json
from pathlib import Path

import pytest

from feishu_generation_agent.domain.document import RequirementRequest, SourceType
from feishu_generation_agent.integrations.feishu_source import FeishuDocumentSource, parse_feishu_url


def test_parse_docx_and_wiki_links():
    assert parse_feishu_url("https://acme.feishu.cn/docx/doccn123") == (SourceType.DOCX, "doccn123")
    assert parse_feishu_url("https://acme.feishu.cn/wiki/wikcn456") == (SourceType.WIKI, "wikcn456")
    with pytest.raises(ValueError, match="只支持 docx 或 wiki"):
        parse_feishu_url("https://acme.feishu.cn/sheets/sht123")


async def test_ingest_preserves_table_cells_and_image_markers(fake_feishu_client, file_store, fixtures_dir: Path):
    fake_feishu_client.blocks = json.loads((fixtures_dir / "feishu_storyboard_blocks.json").read_text("utf-8"))
    source = FeishuDocumentSource(fake_feishu_client, file_store)
    doc = await source.ingest(RequirementRequest(source_url="https://acme.feishu.cn/docx/doccn123"))
    assert "[block:shot-1]" in doc.text_view
    assert "[image:image-1]" in doc.text_view
    assert [(b.table_row, b.table_column) for b in doc.blocks if b.table_row is not None] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert [asset.source_block_id for asset in doc.media_assets] == ["image-block-1", "image-block-2"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_source.py -q`

Expected: FAIL，提示 `integrations.feishu_source` 不存在。

- [ ] **Step 3: 实现 tenant token、分页与统一错误**

`FeishuClient` 使用一个 `httpx.AsyncClient`，并提供以下确定签名：

```python
class FeishuClient:
    async def tenant_token(self) -> str:
        raise NotImplementedError

    async def request_json(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        raise NotImplementedError

    async def iter_items(self, path: str, *, params: dict | None = None) -> list[dict]:
        raise NotImplementedError

    async def download_media(self, file_token: str) -> tuple[bytes, str]:
        raise NotImplementedError
```

实际实现必须：缓存 token 到过期前 60 秒；401/99991663 时只刷新一次；非零飞书 `code` 转为 `AgentError(category="permission_error" 或 "document_error")`；分页循环使用 `page_token` 且检测重复 token，避免死循环。

- [ ] **Step 4: 实现 wiki 解析、Block 树遍历和图片下载**

`FeishuDocumentSource.ingest()` 按固定顺序执行：解析链接；wiki token 转 docx `obj_token`；读取文档标题和 `revision_id`；分页读取全部 Block；根据 parent-child 关系深度优先遍历；展开表格单元格子 Block；为文本写 `[block:<id>]`；为图片写 `[image:<asset_id>]`；下载图片并调用 `FileStore.save_input()`。下载失败时仍建立一个带 `download_error` 的素材记录，并在标准化文档的 `ingest_issues` 中加入阻塞说明。

- [ ] **Step 5: 用两个脱敏 Fixture 验证自由叙述和分镜表**

Fixture 只能保留虚构 token、虚构文本和最小 1x1 PNG Base64；不得提交真实企业域名、Open ID 或文档正文。

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_source.py -q`

Expected: docx、wiki、分页、表格顺序、图片缓存和下载失败用例全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_source.py feishu-generation-agent/src/feishu_generation_agent/integrations/__init__.py feishu-generation-agent/tests/fixtures/feishu_docx_blocks.json feishu-generation-agent/tests/fixtures/feishu_storyboard_blocks.json feishu-generation-agent/tests/unit/test_feishu_source.py
git commit -m "feat(agent): ingest Feishu documents tables and images"
```

---

### Task 5: 用 Claude Vision 建立图片双表示与缓存

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/vision.py`
- Create: `feishu-generation-agent/tests/unit/test_vision.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py`

**Interfaces:**
- Consumes: `MediaAsset`, `VisionDescription`, `Repository.vision_cache`、Claude 兼容 Base URL/Key/模型。
- Produces: `ClaudeVisionAnalyzer.analyze(asset: MediaAsset) -> VisionDescription`。

- [ ] **Step 1: 写失败测试，验证真实 MIME、结构化字段和缓存命中**

```python
from pathlib import Path

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations.vision import ClaudeVisionAnalyzer


async def test_analyze_sends_original_mime_and_caches_by_hash(fake_vision_model, repository, tmp_path: Path):
    image = tmp_path / "reference.webp"
    image.write_bytes(b"RIFF" + b"x" * 40 + b"WEBPVP8 ")
    asset = MediaAsset(
        asset_id="asset-1",
        source_block_id="block-1",
        origin="feishu",
        local_path=image,
        mime_type="image/webp",
        size=image.stat().st_size,
        sha256="abc123",
    )
    analyzer = ClaudeVisionAnalyzer(fake_vision_model, repository, prompt_version="vision-v1")
    first = await analyzer.analyze(asset)
    second = await analyzer.analyze(asset)
    assert first.asset_id == "asset-1"
    assert second == first
    assert fake_vision_model.calls == 1
    assert fake_vision_model.last_media_type == "image/webp"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_vision.py -q`

Expected: FAIL，提示 `ClaudeVisionAnalyzer` 不存在。

- [ ] **Step 3: 实现严格视觉提示词和结构化输出**

`ClaudeVisionAnalyzer` 使用 `ChatAnthropic` 的用户配置 `base_url/api_key/model`，输入一个图片 content block 和以下系统约束：只描述可见内容；不得推断未出现的剧情、品牌或人物身份；`visible_text` 逐项抄录；不确定信息只能写入 `uncertainties`。输出通过 `with_structured_output(VisionDescription)` 校验。

缓存键固定为：

```python
cache_key = f"{asset.sha256}:{self.model_name}:{self.prompt_version}"
```

缓存只保存 `VisionDescription.model_dump(mode="json")`，不保存 Base64、模型原始响应或凭证。

- [ ] **Step 4: 增加失败分类测试**

当模型拒绝、输出缺字段或连接失败时，转换为包含 `asset_id` 的 `AgentError`；连接和 429 标记 `retryable=True`，结构错误标记 `retryable=False`。图片分析失败不能丢弃原图片，后续计划必须收到对应阻塞问题。

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_vision.py -q`

Expected: 缓存、MIME、结构化输出和错误分类用例全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/vision.py feishu-generation-agent/src/feishu_generation_agent/storage/repository.py feishu-generation-agent/tests/unit/test_vision.py
git commit -m "feat(agent): analyze references with cached Claude vision"
```

---

### Task 6: 用 DeepSeek 规划并独立审查需求

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Create: `feishu-generation-agent/tests/unit/test_planner.py`

**Interfaces:**
- Consumes: `NormalizedDocument`, `VisionDescription`, `TaskPlan`, `AuditReport`、DeepSeek 配置。
- Produces: `DeepSeekPlanner.plan()`, `DeepSeekPlanner.audit()`, `validate_plan(plan, document, max_output_count) -> list[str]`。

- [ ] **Step 1: 写失败测试，覆盖分镜合并、任务白名单和一次 JSON 修复**

```python
from feishu_generation_agent.integrations.planner import DeepSeekPlanner, validate_plan


async def test_storyboard_rows_become_one_video_task(fake_deepseek_model, storyboard_document, vision_descriptions):
    planner = DeepSeekPlanner(fake_deepseek_model, max_output_count=4)
    plan = await planner.plan(storyboard_document, vision_descriptions, feedback=None)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].task_type == "image_to_video"
    assert "镜头 1" in plan.tasks[0].prompt
    assert "镜头 4" in plan.tasks[0].prompt


async def test_invalid_json_is_repaired_once(fake_deepseek_model, narrative_document, vision_descriptions):
    fake_deepseek_model.responses = ["not-json", fake_deepseek_model.valid_plan_json]
    planner = DeepSeekPlanner(fake_deepseek_model, max_output_count=4)
    await planner.plan(narrative_document, vision_descriptions, feedback=None)
    assert fake_deepseek_model.calls == 2


def test_validator_rejects_text_to_video_task(raw_plan, narrative_document):
    raw_plan["tasks"][0]["task_type"] = "text_to_video"
    assert "task_type" in " ".join(validate_plan(raw_plan, narrative_document, 4))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_planner.py -q`

Expected: FAIL，提示 `integrations.planner` 不存在。

- [ ] **Step 3: 实现规划输入和 JSON 校验**

`DeepSeekPlanner` 使用 `ChatOpenAI(base_url=settings.deepseek_base_url, model="deepseek-v4-pro")`，并绑定：

```python
model = model.bind(
    response_format={"type": "json_object"},
    extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
)
```

用户消息必须包含：带稳定 Block/Image 引用的 `text_view`；序列化表格；所有视觉描述；两种允许任务类型；完整 `TaskPlan.model_json_schema()`；图片匹配优先级；分镜合并规则。第一次解析失败时，第二次消息只附校验错误和原始输出，最多修复一次。

- [ ] **Step 4: 实现独立审查和确定性校验**

`audit()` 使用独立 system prompt 输出 `AuditReport`，只报告遗漏、冲突、虚构和供应商限制，不直接改写计划。`validate_plan()` 确定性检查任务类型、引用 asset 是否存在、图片是否缺失、字段组合、输出数量和 `source_block_ids` 是否存在；审查发现必须修正的问题转成 `validation_issues` 或触发重新规划。

- [ ] **Step 5: 运行规划测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_planner.py -q`

Expected: 分镜合并、自由叙述、混合任务、图片匹配、非法 JSON、非法任务类型和审查用例全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py feishu-generation-agent/tests/unit/test_planner.py
git commit -m "feat(agent): plan and audit generation requirements"
```

---

### Task 7: 构建运行到人工审批的 LangGraph

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/graph/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/graph/state.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/graph/builder.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/checkpoints.py`
- Create: `feishu-generation-agent/tests/conftest.py`
- Create: `feishu-generation-agent/tests/graph/test_approval_graph.py`

**Interfaces:**
- Consumes: `DocumentSource`, `VisionAnalyzer`, `RequirementPlanner`, `Repository`, `AsyncSqliteSaver`。
- Produces: `AgentState`, `GraphServices`, `build_graph(services, checkpointer)`, `human_approval` interrupt payload。

- [ ] **Step 1: 写失败 Graph 测试，证明未审批时付费调用为零**

```python
from langgraph.checkpoint.memory import InMemorySaver

from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices


async def test_graph_pauses_before_any_generation(fake_services: GraphServices):
    graph = build_graph(fake_services, InMemorySaver())
    config = {"configurable": {"thread_id": "thread-1"}}
    result = await graph.ainvoke(
        {"run_id": "run-1", "thread_id": "thread-1", "source_url": "https://acme.feishu.cn/docx/doccn123", "status": "created"},
        config=config,
    )
    assert result["__interrupt__"][0].value["action"] == "review_plan"
    assert fake_services.image_generator.submit_calls == 0
    assert fake_services.video_generator.submit_calls == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/graph/test_approval_graph.py -q`

Expected: FAIL，提示 `graph.builder` 不存在。

- [ ] **Step 3: 定义纯 JSON State 和节点依赖**

`AgentState` 使用 `TypedDict(total=False)` 并包含规格列出的全部字段；模型进入 State 前统一 `model_dump(mode="json")`。`GraphServices` 是 dataclass，字段固定为：`document_source`, `vision_analyzer`, `planner`, `image_generator`, `video_generator`, `delivery_writer`, `repository`, `file_store`, `settings`。

- [ ] **Step 4: 实现审批前节点与事件记录**

实现 `ingest_source → normalize_document → analyze_images → plan_requirements → audit_plan → validate_plan → human_approval`。每个节点开始和结束都调用 `Repository.append_event()`；摘要不得包含原图 Base64、Key 或完整模型输入。`human_approval` 在 `interrupt()` 之前只能构造 JSON payload，不允许写操作表或调用生成器。

- [ ] **Step 5: 编译图并验证 reject/cancel/approve 路由**

`human_approval` 收到 resume payload 后：`reject` 返回 `Command(update={"planner_feedback": feedback}, goto="plan_requirements")`；`cancel` 返回 `Command(update={"status": "cancelled"}, goto=END)`；`approve` 更新 `approved_tasks` 并转到 `revalidate_approval`。测试分别恢复三种动作，并断言 reject 再次中断、cancel 结束、approve 仍未提交付费任务。

- [ ] **Step 6: 接入严格 SQLite Checkpointer**

`checkpoints.py` 在导入 saver 前设置 `os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")`，通过 `AsyncSqliteSaver.from_conn_string(str(path))` 管理应用生命周期。测试 State 全部可以 JSON dump，且 `checkpoint_db_path` 中不出现测试 Key 字符串。

- [ ] **Step 7: 运行 Graph 测试**

Run: `cd feishu-generation-agent && uv run pytest tests/graph/test_approval_graph.py -q`

Expected: 审批中断、三种恢复路径、无审批零付费调用、严格序列化全部 PASS。

- [ ] **Step 8: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/graph feishu-generation-agent/src/feishu_generation_agent/storage/checkpoints.py feishu-generation-agent/tests/conftest.py feishu-generation-agent/tests/graph/test_approval_graph.py
git commit -m "feat(agent): pause LangGraph for durable human approval"
```

---

### Task 8: 提供本地审批 API 与双栏页面

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css`
- Create: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/main.py`
- Create: `feishu-generation-agent/tests/integration/test_api.py`

**Interfaces:**
- Consumes: `build_graph()`, `Repository`, `FileStore`, `RequirementRequest`, `ApprovalDecision`。
- Produces: `GraphRuntime.start_run()`, `get_run_view()`, `resume_run()`；`POST /api/runs`, `GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/references`, `POST /api/runs/{run_id}/decision`。

- [ ] **Step 1: 写失败 API 测试，覆盖链接提交和审批 payload**

```python
from fastapi.testclient import TestClient


def test_create_run_and_read_waiting_approval(app_with_fake_graph):
    with TestClient(app_with_fake_graph) as client:
        created = client.post("/api/runs", json={"source_url": "https://acme.feishu.cn/docx/doccn123"})
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        view = client.get(f"/api/runs/{run_id}").json()
        assert view["status"] == "waiting_approval"
        assert view["approval"]["tasks"][0]["task_id"] == "task-1"


def test_approval_rejects_unknown_task_id(app_with_fake_graph):
    with TestClient(app_with_fake_graph) as client:
        run_id = client.post("/api/runs", json={"source_url": "https://acme.feishu.cn/docx/doccn123"}).json()["run_id"]
        response = client.post(f"/api/runs/{run_id}/decision", json={"action": "approve", "selected_task_ids": ["missing"], "tasks": []})
        assert response.status_code == 422
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/integration/test_api.py -q`

Expected: FAIL，提示 `web.app` 不存在。

- [ ] **Step 3: 实现 GraphRuntime 和 API schema**

`GraphRuntime.start_run(request: RequirementRequest) -> str` 生成独立 UUID `run_id/thread_id`，先写 `runs` 表，再用 `asyncio.create_task()` 运行到中断；后台异常必须写入事件和 `status=failed`。`resume_run(run_id: str, decision: ApprovalDecision) -> None` 使用同一 `thread_id` 调用 `graph.ainvoke(Command(resume=decision.model_dump(mode="json")))`。同一 run 使用 `asyncio.Lock` 防止双击审批。

API schema 必须拒绝：空链接、未知 action、重复图片顺序、未选任务的 approve、引用不存在 asset 的编辑结果和包含阻塞问题的任务。

- [ ] **Step 4: 实现本地素材增添、替换和删除**

`POST /references` 接收 multipart 图片和 `task_id/role/order/replaces_asset_id`；先调用 `FileStore.save_input()`，再生成新的 `MediaAsset(origin="local_upload")`，更新待审批 State。删除只解除任务引用，不立即删除内容寻址文件；只有删除整个 run 时才清理。任何素材变更都使已有批准决定失效。

- [ ] **Step 5: 实现双栏原生页面**

`index.html` 左栏包含节点轨迹、当前状态、耗时、thread ID；右栏包含任务勾选框、提示词、负面约束、比例、图片尺寸或视频时长/分辨率/声音、参考图缩略图、描述、role、顺序和增添/替换/删除按钮。页面按钮固定为“退回重新规划”“全部取消”“批准所选任务”。`app.js` 每 1 秒轮询当前 run；提交期间禁用按钮；所有非 2xx 响应展示后端 `detail`，不得乐观更新成功状态。

- [ ] **Step 6: 固定本机启动入口**

```python
import argparse

import uvicorn

from feishu_generation_agent.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="本地飞书生成任务 Agent")
    parser.add_argument("--port", type=int, help="本机监听端口，默认读取 APP_PORT")
    args = parser.parse_args()
    settings = Settings()
    uvicorn.run(
        "feishu_generation_agent.web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port or settings.app_port,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 验证 API 与静态页面**

Run: `cd feishu-generation-agent && uv run pytest tests/integration/test_api.py -q`

Expected: 创建、查询、并发审批保护、局部批准、素材 CRUD、错误展示 schema 全部 PASS。

Run: `cd feishu-generation-agent && uv run python -m feishu_generation_agent.main`

Expected: 只出现 `Uvicorn running on http://127.0.0.1:8765`；`lsof -nP -iTCP:8765 -sTCP:LISTEN` 的监听地址为 `127.0.0.1:8765`。验证后用 Ctrl-C 停止。

- [ ] **Step 8: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/web feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py feishu-generation-agent/src/feishu_generation_agent/main.py feishu-generation-agent/tests/integration/test_api.py
git commit -m "feat(agent): add local review and approval workspace"
```

---

### Task 9: 接入 Chiyun Gemini 图生图

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/chiyun.py`
- Create: `feishu-generation-agent/tests/fixtures/chiyun_inline_response.json`
- Create: `feishu-generation-agent/tests/unit/test_chiyun.py`

**Interfaces:**
- Consumes: `GenerationTask(task_type=image_to_image)`, 有序 `MediaAsset`、Chiyun Base URL/Key/模型。
- Produces: `ChiyunImageGenerator.submit() -> ProviderSubmission`, `poll() -> ProviderSubmission`；同步响应直接返回 `succeeded`。

- [ ] **Step 1: 写失败测试，固定请求结构与两种结果格式**

```python
import base64
import json

from feishu_generation_agent.integrations.chiyun import ChiyunImageGenerator


async def test_submit_uses_generate_content_and_original_mime(mock_http, image_task, reference_assets):
    mock_http.add_json({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"png-result").decode()}}]}}]})
    generator = ChiyunImageGenerator(mock_http.client, base_url="https://chiyun.work", api_key="test-key", model="verified-model")
    submission = await generator.submit(image_task, reference_assets)
    request = mock_http.requests[0]
    assert request.url.path == "/v1beta/models/verified-model:generateContent"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == reference_assets[0].mime_type
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "9:16", "imageSize": "2K"}
    assert submission.status == "succeeded"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_chiyun.py -q`

Expected: FAIL，提示 `ChiyunImageGenerator` 不存在。

- [ ] **Step 3: 实现请求构造和结果解析**

请求路径固定为 `/v1beta/models/{url_quote(model)}:generateContent`；`contents[0].parts` 第一项为 text，后续按审批顺序写 `inline_data.mime_type/data`。解析 `inlineData`、`inline_data` 和 HTTPS URL 三种图片结果，统一写入 `ProviderSubmission(result_items=[ProviderResult(url|base64_data, mime_type)])`。日志只记录结果数量和 MIME，不记录图片内容。

- [ ] **Step 4: 增加模型能力探针但不硬编码未验证模型**

实现 `probe_models()`：只请求 `/v1beta/models`。若通道不支持模型列表，则报告“该通道无法无费用验证模型”，不得调用 `generateContent`；模型能力只在用户明确批准的付费冒烟测试中验证。`chiyun_model` 为空时配置检查失败，不能猜测模型名。

- [ ] **Step 5: 运行 Chiyun 单元测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_chiyun.py -q`

Expected: inline Base64、URL、缺结果、429、非法 MIME、模型编码和 Key 脱敏用例全部 PASS；HTTP 请求只命中本地 MockTransport。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/chiyun.py feishu-generation-agent/tests/fixtures/chiyun_inline_response.json feishu-generation-agent/tests/unit/test_chiyun.py
git commit -m "feat(agent): generate approved images through Chiyun"
```

---

### Task 10: 接入官方 Ark Seedance 图生视频

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/seedance.py`
- Create: `feishu-generation-agent/tests/fixtures/seedance_succeeded.json`
- Create: `feishu-generation-agent/tests/unit/test_seedance.py`

**Interfaces:**
- Consumes: `GenerationTask(task_type=image_to_video)`, 有序图片 `MediaAsset`、Ark Base URL/API Key/模型。
- Produces: `SeedanceVideoGenerator.submit() -> ProviderSubmission`, `poll() -> ProviderSubmission`。

- [ ] **Step 1: 写失败测试，固定图片顺序、多镜头提示词和轮询**

```python
import json

from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


async def test_submit_preserves_reference_order_and_video_parameters(mock_http, video_task, reference_assets):
    mock_http.add_json({"id": "task-ark-123", "status": "queued"})
    generator = SeedanceVideoGenerator(mock_http.client, base_url="https://ark.example/api/v3", api_key="ark-key", model="doubao-seedance-2-0-260128")
    submission = await generator.submit(video_task, list(reversed(reference_assets)))
    body = json.loads(mock_http.requests[0].content)
    images = [item for item in body["content"] if item["type"] == "image_url"]
    assert [item["role"] for item in images] == ["reference_image", "reference_image"]
    assert body["duration"] == 10
    assert body["ratio"] == "9:16"
    assert body["resolution"] == "720p"
    assert body["generate_audio"] is True
    assert submission.provider_task_id == "task-ark-123"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_seedance.py -q`

Expected: FAIL，提示 `SeedanceVideoGenerator` 不存在。

- [ ] **Step 3: 实现 Ark 提交和图片引用**

POST `{base_url}/contents/generations/tasks`，Bearer 鉴权。第一项 content 是多镜头 text；图片按 `GenerationTask.reference_images.order` 排序并编码为真实 MIME 的 data URL，role 使用审批值 `first_frame`、`last_frame` 或 `reference_image`。确定性校验禁止首尾帧和普通参考图混用。payload 固定包含 `model/duration/ratio/resolution/generate_audio/watermark=false`。

- [ ] **Step 4: 实现轮询与终态解析**

GET `{base_url}/contents/generations/tasks/{provider_task_id}`；`queued/running` 返回原提交加新状态；`succeeded` 提取 `content.video_url`；`failed/cancelled/expired` 转为 `provider_terminal_error` 且 `retryable=False`。一次 poll 不 sleep，轮询间隔由执行节点调度，便于 Checkpoint 恢复和测试。

- [ ] **Step 5: 运行 Seedance 单元测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_seedance.py -q`

Expected: 创建、状态轮询、成功 URL、终态失败、非法字段组合、图片顺序和 Authorization 脱敏用例全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/seedance.py feishu-generation-agent/tests/fixtures/seedance_succeeded.json feishu-generation-agent/tests/unit/test_seedance.py
git commit -m "feat(agent): generate approved videos through Seedance"
```

---

### Task 11: 实现审批后串行执行、幂等恢复与产物校验

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/builder.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/files.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py`
- Create: `feishu-generation-agent/tests/graph/test_execution_graph.py`

**Interfaces:**
- Consumes: 批准任务、`ImageGenerator`、`VideoGenerator`、`Repository` 幂等操作、`FileStore.save_download()`。
- Produces: `revalidate_approval`, `check_source_revision`, `execute_selected_tasks`, `verify_and_download_artifacts` 节点。

- [ ] **Step 1: 写失败 Graph 测试，覆盖局部执行、失败继续和重复恢复**

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.graph.builder import build_graph


async def test_only_selected_tasks_run_and_failure_does_not_stop_next(fake_services_with_two_tasks):
    graph = build_graph(fake_services_with_two_tasks, InMemorySaver())
    config = {"configurable": {"thread_id": "thread-exec"}}
    await graph.ainvoke({"run_id": "run-exec", "thread_id": "thread-exec", "source_url": "https://acme.feishu.cn/docx/doccn123"}, config=config)
    result = await graph.ainvoke(Command(resume={"action": "approve", "selected_task_ids": ["task-fail", "task-ok"], "tasks": fake_services_with_two_tasks.approved_payload}), config=config)
    assert result["execution_records"][0]["status"] == "failed"
    assert result["execution_records"][1]["status"] == "succeeded"
    assert fake_services_with_two_tasks.image_generator.submitted_ids == ["task-fail", "task-ok"]


async def test_existing_provider_id_is_polled_not_resubmitted(fake_services_with_existing_operation):
    result = await fake_services_with_existing_operation.run_from_execution_node()
    assert fake_services_with_existing_operation.video_generator.submit_calls == 0
    assert fake_services_with_existing_operation.video_generator.poll_calls >= 1
    assert result["artifacts"][0]["provider_task_id"] == "ark-existing"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/graph/test_execution_graph.py -q`

Expected: FAIL，图缺少审批后执行节点或幂等逻辑。

- [ ] **Step 3: 实现批准后再次校验与源版本守卫**

`revalidate_approval` 重新构造 `TaskPlan` 并调用全部领域校验。`check_source_revision` 比较当前 revision 和审批 revision；不同则清除 `approval_decision/approved_tasks`，写事件 `source_changed`，返回 `Command(goto="ingest_source")`；相同才进入执行。

- [ ] **Step 4: 实现串行幂等执行**

对每个批准任务：查询 operation=`submit`；无记录时调用对应生成器 `submit()` 并立即保存 provider ID；有记录时构造 submission 直接 poll；每次状态变更写 operation；终态失败写执行记录后继续下一项。轮询使用可注入 `async_sleep` 和配置间隔；超时只标记当前任务失败，不删除 provider ID。

- [ ] **Step 5: 实现原子下载和 Artifact 记录**

生成器返回 Base64 时先校验解码大小和文件头；返回 URL 时使用不带 Authorization 的独立 GET 下载，限制重定向次数和总字节数。图片用 Pillow verify，视频至少验证 ISO BMFF `ftyp` 或 WebM EBML 头。成功后记录真实 MIME、size、sha256、provider URL 的脱敏版本和 provider task ID。

- [ ] **Step 6: 验证恢复不会重复付费提交**

Run: `cd feishu-generation-agent && uv run pytest tests/graph/test_execution_graph.py -q`

Expected: 局部批准、失败继续、已有 provider ID、已有有效文件、文件哈希不符重下、文档版本变化和超时用例全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py feishu-generation-agent/src/feishu_generation_agent/graph/builder.py feishu-generation-agent/src/feishu_generation_agent/storage/files.py feishu-generation-agent/src/feishu_generation_agent/storage/repository.py feishu-generation-agent/tests/graph/test_execution_graph.py
git commit -m "feat(agent): execute approved tasks with idempotent recovery"
```

---

### Task 12: 创建飞书交付文档并支持大文件分片上传

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_delivery.py`
- Create: `feishu-generation-agent/tests/unit/test_feishu_delivery.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py`

**Interfaces:**
- Consumes: `FeishuClient`, `NormalizedDocument`, 最终 `TaskPlan`, `Artifact` 列表、`LARK_OUTPUT_OWNER_OPEN_ID`。
- Produces: `FeishuDeliveryWriter.deliver() -> DeliveryRecord`, `retry_delivery(run_id) -> DeliveryRecord`。

- [ ] **Step 1: 写失败测试，覆盖标题、直接上传、分片上传和文档复用**

```python
from feishu_generation_agent.integrations.feishu_delivery import FeishuDeliveryWriter


async def test_delivery_creates_expected_title_and_reuses_document(fake_feishu_client, repository, source_document, approved_plan, small_artifact):
    writer = FeishuDeliveryWriter(fake_feishu_client, repository, owner_open_id="ou_test")
    first = await writer.deliver(source_document, approved_plan, [small_artifact])
    second = await writer.deliver(source_document, approved_plan, [small_artifact])
    assert first.document_id == second.document_id
    assert fake_feishu_client.created_titles[0].startswith("[AI 交付] 棋局 - ")
    assert fake_feishu_client.create_document_calls == 1
    assert fake_feishu_client.add_member_calls == 1


async def test_video_larger_than_20mb_uses_chunk_upload(fake_feishu_client, repository, source_document, approved_plan, large_video_artifact):
    writer = FeishuDeliveryWriter(fake_feishu_client, repository, owner_open_id="ou_test")
    await writer.deliver(source_document, approved_plan, [large_video_artifact])
    assert fake_feishu_client.upload_prepare_calls == 1
    assert fake_feishu_client.upload_part_calls > 1
    assert fake_feishu_client.upload_finish_calls == 1
    assert fake_feishu_client.upload_all_calls == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_delivery.py -q`

Expected: FAIL，提示 `FeishuDeliveryWriter` 不存在。

- [ ] **Step 3: 实现交付文档结构**

标题严格使用 `[AI 交付] <源标题> - YYYY-MM-DD HH:mm`。正文依次写：原文档链接和 revision、执行摘要、每项任务最终提示词和参数、参考图映射、生成图片、视频附件、失败任务与重试建议。Block 创建按飞书 API 上限分批；每批成功后保存进度，失败恢复时从未写入批次继续。

- [ ] **Step 4: 实现上传选择与素材复用**

文件 `<= 20 * 1024 * 1024` 使用直接上传；大文件执行 prepare → 逐 part 上传 → finish。幂等 operation 名分别是 `delivery_document`、`upload:<artifact_id>`、`delivery_blocks:<batch_index>`、`delivery_permission`。已有 `feishu_file_token` 时不重复上传；已有 document ID 时更新原交付文档。

- [ ] **Step 5: 实现协作者授权与独立重试**

创建文档后把配置的 Open ID 添加为可编辑协作者。生成任务成功但交付失败时，Graph status 使用 `delivery_failed`，不得把 Artifact 改成失败；`retry_delivery()` 只能调用交付适配器，测试断言生成器 submit 调用次数不增加。

- [ ] **Step 6: 运行交付测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_delivery.py -q`

Expected: 标题、Block 顺序、直接/分片边界、分片恢复、素材复用、协作者和独立重试全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_delivery.py feishu-generation-agent/src/feishu_generation_agent/storage/repository.py feishu-generation-agent/tests/unit/test_feishu_delivery.py
git commit -m "feat(agent): deliver artifacts to a new Feishu document"
```

---

### Task 13: 完成全图、重启恢复、运行轨迹与删除

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/builder.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Create: `feishu-generation-agent/tests/integration/test_restart_recovery.py`

**Interfaces:**
- Consumes: 全部节点、业务 SQLite、Checkpoint SQLite、交付适配器。
- Produces: 完整 `START → deliver_to_feishu → END`、恢复未完成 run、删除 run、独立重试交付和可观察状态 API。

- [ ] **Step 1: 写失败集成测试，模拟进程重建后继续同一 thread**

```python
from feishu_generation_agent.domain.document import RequirementRequest


async def test_restart_reuses_checkpoint_and_provider_task(runtime_factory, persistent_paths):
    first_runtime, first_fakes = await runtime_factory(persistent_paths)
    request = RequirementRequest(source_url="https://acme.feishu.cn/docx/doccn123")
    run_id = await first_runtime.start_run(request)
    await first_runtime.resume_run(run_id, first_fakes.approve_one_video)
    await first_fakes.wait_until_provider_id_saved()
    await first_runtime.close()

    second_runtime, second_fakes = await runtime_factory(persistent_paths)
    await second_runtime.resume_pending_runs()
    final = await second_runtime.wait_for_terminal(run_id)
    assert final["status"] == "succeeded"
    assert second_fakes.video_generator.submit_calls == 0
    assert second_fakes.video_generator.poll_calls >= 1
    await second_runtime.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/integration/test_restart_recovery.py -q`

Expected: FAIL，缺少恢复 pending run 或完整交付边。

- [ ] **Step 3: 完整编译图与终态**

最终边固定为：`revalidate_approval → check_source_revision → execute_selected_tasks → verify_and_download_artifacts → deliver_to_feishu → END`。取消为 `cancelled`；全部生成失败仍进入交付，状态为 `completed_with_errors`；至少一个产物成功且交付成功为 `succeeded`；交付失败为 `delivery_failed`。

- [ ] **Step 4: 实现启动恢复与单运行互斥**

应用生命周期先处理 LangSmith：`langsmith_tracing=false` 时显式禁用；为 true 时要求 `langsmith_api_key`，把 Key 和 project 只传给 LangChain/LangGraph 追踪配置，不写数据库或状态。随后打开 Repository 和 AsyncSqliteSaver，查询 `created/running/waiting_provider/delivering`；`waiting_approval` 只恢复展示，不自动 resume；其余运行用保存的 thread ID 继续。每个 run 一个锁；进程内重复恢复和 API 双击都不能产生第二个 worker。

- [ ] **Step 5: 实现可观察状态与安全删除**

`GET /api/runs/{run_id}` 返回节点状态、开始/结束时间、耗时、脱敏摘要、重试次数和 provider task ID。`DELETE /api/runs/{run_id}` 只允许终态或等待审批状态；删除业务行、本地 run 目录和 `checkpointer.adelete_thread(thread_id)`，不删除飞书交付文档。页面显示删除确认和 LangSmith 开启时的数据外发警告。

- [ ] **Step 6: 实现交付重试端点**

`POST /api/runs/{run_id}/retry-delivery` 只接受 `delivery_failed`，复用 Artifact、document ID 和已上传 token；返回 202。测试断言 Chiyun/Seedance submit 和 poll 调用计数均不增加。

- [ ] **Step 7: 运行完整自动化测试**

Run: `cd feishu-generation-agent && uv run pytest -q`

Expected: 全部测试 PASS；测试日志中搜索 `Bearer test-key`、`ark-key` 和测试 Base64 均无命中。

- [ ] **Step 8: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/graph feishu-generation-agent/src/feishu_generation_agent/web feishu-generation-agent/tests/integration/test_restart_recovery.py
git commit -m "feat(agent): recover complete workflows across restarts"
```

---

### Task 14: 配置探针、使用文档与显式付费冒烟门禁

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/cli/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/cli/config_probe.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/cli/smoke.py`
- Create: `feishu-generation-agent/README.md`
- Modify: `feishu-generation-agent/.env.example`
- Modify: `feishu-generation-agent/pyproject.toml`
- Modify: `feishu-generation-agent/tests/integration/test_api.py`

**Interfaces:**
- Consumes: 全部真实适配器和 `Settings`。
- Produces: `uv run agent-config-probe`, `uv run agent-smoke --confirm-paid-smoke <测试文档URL>`、完整本地操作手册。

- [ ] **Step 1: 写失败测试，锁定健康检查和付费门禁**

```python
def test_health_reports_capabilities_without_secrets(app_with_missing_keys):
    response = app_with_missing_keys.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["capabilities"]["feishu_read"]["configured"] is False
    assert "secret" not in response.text.lower()
    assert "api_key" not in response.text.lower()


def test_smoke_requires_exact_confirmation(smoke_runner):
    result = smoke_runner.invoke(["https://acme.feishu.cn/docx/doccn123"])
    assert result.exit_code != 0
    assert "--confirm-paid-smoke" in result.output
    assert smoke_runner.paid_calls == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/integration/test_api.py -q`

Expected: FAIL，缺少 `/api/health` 或 smoke 门禁。

- [ ] **Step 3: 实现不付费配置探针**

探针依次报告：本地目录可写；飞书 token、docx 只读、wiki 节点、素材下载、创建文档和协作者能力；DeepSeek 模型连接；Claude Vision 通道连接；Chiyun 模型存在；Ark API Key 鉴权。会产生资源的飞书创建/上传能力只做接口权限探针或对用户提供的专用测试文档执行，不在普通启动时创建文档。输出只包含 `configured/reachable/permission_ok/message`。

- [ ] **Step 4: 实现真实冒烟脚本的双重确认**

脚本必须同时满足命令行存在 `--confirm-paid-smoke` 和环境变量 `ALLOW_PAID_SMOKE=YES`；否则在构造真实生成器之前退出。执行顺序固定为：读取测试文档；分析最多两张图；规划和审查；执行一次 reject/resume；批准 `output_count=1` 的最低成本图生图；批准一个最短允许时长 Seedance；交付；重启 runtime 并验证 operation 计数不增加。脚本打印预计付费步骤，并在每个付费调用前再次等待终端输入 `YES`。

- [ ] **Step 5: 编写 README**

README 必须包含：项目边界；架构图；每个 LangGraph 节点解释；依赖安装；`.env` 全字段；飞书网页配置的能力清单；`LARK_OUTPUT_OWNER_OPEN_ID` 获取方式；启动命令；审批页面操作；本地素材增添/替换/删除；测试命令；配置探针；恢复语义；交付重试；数据删除；LangSmith 隐私提醒；未来通过 `FeishuBotSource` 构造相同 `RequirementRequest` 并用交互卡片恢复同一 `thread_id` 的扩展点；常见 401/403/429、文档图片失败、模型 JSON 错误、Ark 长轮询和飞书分片失败排查。

- [ ] **Step 6: 添加 console scripts 并验证帮助文本**

```toml
[project.scripts]
feishu-generation-agent = "feishu_generation_agent.main:main"
agent-config-probe = "feishu_generation_agent.cli.config_probe:main"
agent-smoke = "feishu_generation_agent.cli.smoke:main"
```

Run: `cd feishu-generation-agent && uv sync && uv run feishu-generation-agent --help && uv run agent-config-probe --help && uv run agent-smoke --help`

Expected: 三个入口退出码 0；smoke 帮助明确显示双重确认要求。

- [ ] **Step 7: 最终自动化验收**

Run: `cd feishu-generation-agent && uv run pytest -q`

Expected: 全部测试 PASS。

Run: `cd feishu-generation-agent && rg -n "[T]BD|[T]ODO|[F]IXME|请填写|sk-[A-Za-z0-9]|Bearer [A-Za-z0-9]" README.md .env.example src tests || true`

Expected: 无真实占位说明、硬编码 Key 或 Bearer Token 命中；测试中虚构的 `Bearer test-key` 必须改为通过变量构造，避免扫描误报。

Run: `cd feishu-generation-agent && git status --short`

Expected: 只出现 Task 14 列出的文件，没有 `data/`、`outputs/`、`.env`、数据库或真实响应。

- [ ] **Step 8: Commit**

```bash
git add feishu-generation-agent/README.md feishu-generation-agent/.env.example feishu-generation-agent/pyproject.toml feishu-generation-agent/uv.lock feishu-generation-agent/src/feishu_generation_agent/cli feishu-generation-agent/tests/integration/test_api.py
git commit -m "docs(agent): add probes smoke gate and operating guide"
```

---

## Final Verification Gate

在声称本地版本完成前，执行者必须使用 `superpowers:verification-before-completion`，并保留以下新鲜证据：

1. `cd feishu-generation-agent && uv sync --locked` 成功。
2. `cd feishu-generation-agent && uv run pytest -q` 全绿。
3. `cd feishu-generation-agent && uv run agent-config-probe` 对未配置项给出可操作错误且不泄露凭证。
4. `cd feishu-generation-agent && uv run feishu-generation-agent` 只监听 `127.0.0.1:8765`。
5. Fake 端到端用例证明未审批时 Chiyun/Seedance 调用次数为 0。
6. 重启恢复用例证明已有 provider task ID 时不重复 submit。
7. 交付失败重试用例证明不重新生成。
8. `git status --short` 不含运行数据、凭证和非本任务文件。
9. 真实 Chiyun/Seedance 冒烟测试只能在用户提供全部 Key、专用飞书测试文档并再次明确同意付费后运行。
