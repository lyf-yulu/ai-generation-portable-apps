# 飞书多维表格任务与机器人入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 `feishu-generation-agent/` 上增加一个固定飞书多维表格任务源、本地扫描入口和飞书应用机器人长连接入口，使多人可以原子领取需求、在飞书卡片审批，并把生成图片/视频直接写回原记录的 `结果` 附件列。

**Architecture:** 新增可独立测试的 `BitableTaskService`、`BitableTaskStore`、`TaskCoordinator`、`BotGateway` 和 `BitableResultWriter`。本地页面与机器人只负责输入/展示，统一调用协调器；协调器复用现有 `GraphRuntime`、LangGraph 审批中断和生成幂等逻辑，并使用持久化绑定把 `record_id → run_id → thread_id` 串起来。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、aiosqlite、LangGraph 1.2、LangChain 1.3、httpx、飞书官方机器人 SDK `lark-channel-sdk 1.2`、原生 HTML/CSS/JavaScript、pytest、Node.js 内置测试运行器。

## Global Constraints

- 新功能只修改 `feishu-generation-agent/` 与本计划/配套文档，不接入现有 Portal、Nano Banana、Seedance 或 Dreamina 进程。
- 应用首版仍只绑定 `127.0.0.1:8765`；飞书机器人通过出站长连接工作，不增加公网回调、内网穿透或局域网入口。
- 首版只配置一个固定多维表格和视图；表格 URL、table ID、view ID 和本地操作者 open_id 全部从环境变量读取。
- 多维表格的唯一任务身份是 `app_token + table_id + record_id`，不得使用 `文本` 序号或需求 URL 作为主键。
- 所有机器人用户可以扫描和领取全部可处理记录；只有当前领取者可以批准、反馈或取消该运行。
- `文本`、`需求来源`、`执行人`、`结果` 类型不符时只报错；只允许自动创建 `状态` 单选字段和 `错误信息` 多行文本字段。
- 多维表格模式把产物直接写入 `结果` 附件列，不创建新交付文档，不要求输出文件夹或固定文档协作者。
- 现有文档链接入口和新建文档交付可以保留为兼容模式，但其配置缺失不能阻塞表格模式启动。
- 机器人卡片支持任务选择、批准、取消、反馈重规划；首版不在卡片内逐字段编辑提示词、参数或参考图。
- 任何 Chiyun/Seedance 付费提交仍必须发生在 LangGraph `interrupt()` 审批恢复之后。
- 每次审批绑定 `record_id + run_id + approval_version + claimant_open_id`；旧卡片和重复 action ID 不得触发付费调用。
- 供应商已有官方任务 ID 时只轮询；`回写失败` 只重试媒体上传/记录更新，不重新生成。
- 回写前重新读取记录；`结果` 已被其他人写入时不得覆盖或合并，状态写为 `回写失败` 并提示冲突。
- 进程重启必须恢复持久化收件箱、活动领取、待审批卡片、供应商轮询和结果回写。
- Graph State、SQLite、日志和测试夹具不得保存 API Key、App Secret、tenant token、签名 URL、图片 Base64 或完整飞书原始事件。
- DeepSeek、Claude、Chiyun 和 Seedance 模型名必须通过无付费探针验证，不可用时明确失败，不静默切换模型。
- Seedance 生成继续使用 Ark Bearer Key；火山方舟 AK/SK 和分组名不写入 `ARK_API_KEY`。
- 前端保持原生 HTML/CSS/JavaScript，不引入 npm 构建链。
- 依赖安装使用项目自己的 `.venv`；网络卡顿时可使用中国镜像源，但 `uv.lock` 必须提交。
- 自动化测试不得访问真实付费接口；真实最小冒烟必须在执行当次再次取得用户明确批准。
- 不覆盖或提交仓库中与本任务无关的现有修改。

---

## File Structure

### 新建文件

- `feishu-generation-agent/src/feishu_generation_agent/domain/bitable.py` — 表格位置、字段契约、任务摘要、领取绑定和外部状态模型。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_url.py` — 严格解析 wiki/bitable URL 和需求来源字段。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_bitable.py` — wiki→app token、字段/记录读写、状态同步和附件字段客户端。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_delivery.py` — 校验产物、上传为 bitable 附件、冲突检查和幂等回写。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/routing_delivery.py` — 按 run 是否绑定表格记录选择表格交付或旧文档交付。
- `feishu-generation-agent/src/feishu_generation_agent/storage/bitable_tasks.py` — 原子领取、审批版本、收件箱和卡片 action 去重。
- `feishu-generation-agent/src/feishu_generation_agent/bitable/__init__.py` — 表格任务包。
- `feishu-generation-agent/src/feishu_generation_agent/bitable/service.py` — 扫描、字段准备度和领取前校验。
- `feishu-generation-agent/src/feishu_generation_agent/bitable/coordinator.py` — 创建/恢复运行、状态映射、审批和通知。
- `feishu-generation-agent/src/feishu_generation_agent/bot/__init__.py` — 机器人包。
- `feishu-generation-agent/src/feishu_generation_agent/bot/cards.py` — 任务列表卡、审批卡和结果卡的纯 JSON 渲染。
- `feishu-generation-agent/src/feishu_generation_agent/bot/gateway.py` — 消息/卡片事件持久化、后台消费和长连接生命周期。
- `feishu-generation-agent/src/feishu_generation_agent/bot/lark_channel.py` — 飞书官方 Channel SDK 的窄适配层。
- `feishu-generation-agent/tests/unit/test_bitable_domain.py`
- `feishu-generation-agent/tests/unit/test_bitable_store.py`
- `feishu-generation-agent/tests/unit/test_feishu_bitable.py`
- `feishu-generation-agent/tests/unit/test_bitable_delivery.py`
- `feishu-generation-agent/tests/unit/test_routing_delivery.py`
- `feishu-generation-agent/tests/unit/test_bitable_service.py`
- `feishu-generation-agent/tests/unit/test_task_coordinator.py`
- `feishu-generation-agent/tests/unit/test_bot_cards.py`
- `feishu-generation-agent/tests/unit/test_bot_gateway.py`
- `feishu-generation-agent/tests/integration/test_bitable_api.py`
- `feishu-generation-agent/tests/integration/test_bitable_restart.py`
- `feishu-generation-agent/tests/fixtures/bitable_fields.json`
- `feishu-generation-agent/tests/fixtures/bitable_records.json`
- `feishu-generation-agent/tests/frontend/bitable_state.test.cjs`

### 修改文件

- `feishu-generation-agent/pyproject.toml`、`uv.lock` — 增加并锁定飞书官方 SDK。
- `feishu-generation-agent/.env.example`、`README.md` — 增加表格/机器人配置和操作说明。
- `feishu-generation-agent/src/feishu_generation_agent/config.py` — 新配置与可配置 DeepSeek 模型名。
- `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py` — 按能力组装旧交付或表格交付。
- `feishu-generation-agent/src/feishu_generation_agent/domain/artifact.py` — 交付目标支持 docx 或 bitable record。
- `feishu-generation-agent/src/feishu_generation_agent/ports.py` — 表格客户端、通知器和领取存储端口。
- `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py` — 支持参数化上传目标。
- `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py` — 允许使用已预留的 run/thread ID 启动。
- `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py`、`web/app.py` — 扫描/领取/表格审批 API 和生命周期。
- `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`、`app.js`、`styles.css` — 本地表格任务扫描与领取界面。
- `feishu-generation-agent/src/feishu_generation_agent/cli/config_probe.py`、`cli/smoke.py` — 表格、机器人和结果回写探针/门禁冒烟。
- 相关既有测试 — 适配 `DeliveryRecord` 和服务组装，不降低原断言。

---

### Task 1: 配置契约、能力分组与飞书 SDK 依赖

**Files:**
- Modify: `feishu-generation-agent/pyproject.toml`
- Modify: `feishu-generation-agent/uv.lock`
- Modify: `feishu-generation-agent/.env.example`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/config.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py`
- Modify: `feishu-generation-agent/tests/unit/test_config.py`

**Interfaces:**
- Consumes: 现有 `Settings.require(*field_names: str) -> None`。
- Produces: `CAPABILITY_FIELDS: dict[str, tuple[str, ...]]`、`capability_is_configured(settings: Settings, name: str) -> bool` 和表格/机器人配置字段。

- [ ] **Step 1: 写失败测试，固定表格配置、模型可配置性和能力隔离**

```python
def test_table_mode_does_not_require_legacy_delivery_fields(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        lark_app_id="cli_test",
        lark_app_secret="secret",
        lark_bitable_url="https://example.feishu.cn/wiki/wiki123?table=tbl123&view=vew123",
        lark_bitable_table_id="tbl123",
        lark_bitable_view_id="vew123",
        lark_local_operator_open_id="ou_local",
        deepseek_api_key="deepseek",
        deepseek_model="account-visible-model",
        claude_api_key="claude",
        claude_model="claude-model",
        chiyun_api_key="chiyun",
        chiyun_model="chiyun-model",
        ark_api_key="ark",
    )
    assert capability_is_configured(settings, "bitable")
    assert capability_is_configured(settings, "generation")
    assert not capability_is_configured(settings, "legacy_delivery")


def test_local_claim_requires_operator_open_id():
    settings = Settings(lark_local_operator_open_id=None)
    assert not capability_is_configured(settings, "local_claim")
```

- [ ] **Step 2: 运行测试，确认当前配置仍把旧交付字段作为全局必需项**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_config.py -q`

Expected: FAIL，提示缺少 `lark_bitable_url` 或无法导入 `capability_is_configured`。

- [ ] **Step 3: 增加依赖、设置和能力分组**

在 `pyproject.toml` 的运行依赖加入：

```toml
"lark-channel-sdk>=1.2,<2",
```

在 `Settings` 加入：

```python
lark_bitable_url: str | None = None
lark_bitable_table_id: str | None = None
lark_bitable_view_id: str | None = None
lark_local_operator_open_id: str | None = None
lark_bot_enabled: bool = False
bot_scan_page_size: int = Field(default=10, ge=1, le=50)
coordinator_poll_interval_seconds: float = Field(default=1.0, ge=0.05)
deepseek_model: str = "deepseek-v4-pro"
```

在 `bootstrap.py` 用能力表替代单一 `REQUIRED_RUNTIME_FIELDS`：

```python
CAPABILITY_FIELDS = {
    "core": (
        "lark_app_id", "lark_app_secret", "deepseek_api_key",
        "claude_api_key", "claude_model",
    ),
    "generation": (
        "chiyun_api_key", "chiyun_model", "ark_api_key",
        "seedance_model",
    ),
    "bitable": (
        "lark_app_id", "lark_app_secret", "lark_bitable_url",
        "lark_bitable_table_id", "lark_bitable_view_id",
    ),
    "local_claim": ("lark_local_operator_open_id",),
    "legacy_delivery": (
        "lark_output_owner_open_id", "lark_output_folder_token",
    ),
}


def capability_is_configured(settings: Settings, name: str) -> bool:
    try:
        settings.require(*CAPABILITY_FIELDS[name])
    except (KeyError, ValueError):
        return False
    return True


def runtime_is_configured(settings: Settings) -> bool:
    return (
        capability_is_configured(settings, "core")
        and capability_is_configured(settings, "generation")
        and (
            capability_is_configured(settings, "bitable")
            or capability_is_configured(settings, "legacy_delivery")
        )
    )
```

同步 `.env.example`，所有新值保持空白或安全默认值，绝不写真实 token。

- [ ] **Step 4: 更新锁文件并验证配置测试**

Run: `cd feishu-generation-agent && uv lock && uv run pytest tests/unit/test_config.py -q`

Expected: PASS；`uv.lock` 包含 `lark-channel-sdk`，且不需要旧输出文件夹即可判定表格模式可配置。

- [ ] **Step 5: 提交**

```bash
git add feishu-generation-agent/pyproject.toml feishu-generation-agent/uv.lock feishu-generation-agent/.env.example feishu-generation-agent/src/feishu_generation_agent/config.py feishu-generation-agent/src/feishu_generation_agent/bootstrap.py feishu-generation-agent/tests/unit/test_config.py
git commit -m "feat(agent): add bitable capability configuration"
```

---

### Task 2: 多维表格领域模型与严格 URL/字段解析

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/bitable.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_url.py`
- Create: `feishu-generation-agent/tests/unit/test_bitable_domain.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py`

**Interfaces:**
- Consumes: `parse_feishu_url(url: str) -> tuple[SourceType, str]`。
- Produces: `BitableLocation`、`BitableTaskSummary`、`BitableBinding`、`TableTaskStatus`、`parse_bitable_url(url, table_id, view_id)` 和 `parse_requirement_source(value) -> str`。

- [ ] **Step 1: 写失败测试，固定 wiki 表格 URL 和需求来源的唯一性**

```python
def test_parse_bitable_url_requires_matching_query_and_config():
    location = parse_bitable_url(
        "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC&view=vewABC",
        table_id="tblABC",
        view_id="vewABC",
    )
    assert location.wiki_token == "wikiABC"
    assert location.table_id == "tblABC"
    assert location.view_id == "vewABC"

    with pytest.raises(ValueError, match="table"):
        parse_bitable_url(
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblOTHER&view=vewABC",
            table_id="tblABC",
            view_id="vewABC",
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://tenant.feishu.cn/docx/docABC", "https://tenant.feishu.cn/docx/docABC"),
        ({"link": "https://tenant.feishu.cn/wiki/wikiDOC", "text": "需求"}, "https://tenant.feishu.cn/wiki/wikiDOC"),
        ([{"link": "https://tenant.feishu.cn/docx/docABC"}], "https://tenant.feishu.cn/docx/docABC"),
    ],
)
def test_parse_requirement_source_accepts_exactly_one_document(value, expected):
    assert parse_requirement_source(value) == expected


def test_parse_requirement_source_rejects_multiple_links():
    with pytest.raises(ValueError, match="恰好一个"):
        parse_requirement_source([
            {"link": "https://tenant.feishu.cn/docx/a"},
            {"link": "https://tenant.feishu.cn/docx/b"},
        ])
```

- [ ] **Step 2: 运行测试并确认模块尚不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_domain.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现严格模型与解析器**

```python
class TableTaskStatus(StrEnum):
    PENDING = "待处理"
    PROCESSING = "处理中"
    WAITING_APPROVAL = "待审批"
    GENERATING = "生成中"
    WRITING_BACK = "回写中"
    COMPLETED = "已完成"
    FAILED = "失败"
    WRITEBACK_FAILED = "回写失败"


class BitableLocation(BaseModel):
    wiki_token: str
    app_token: str | None = None
    table_id: str
    view_id: str
    source_url: str


class BitableTaskSummary(BaseModel):
    record_id: str
    display_text: str
    source_url: str
    status: TableTaskStatus = TableTaskStatus.PENDING
    executor_open_ids: list[str] = Field(default_factory=list)
    has_result: bool = False


class BitableBinding(BaseModel):
    app_token: str
    table_id: str
    view_id: str
    record_id: str
    source_url: str
    display_text: str
    run_id: str
    thread_id: str
    claimant_open_id: str
    status: TableTaskStatus
    approval_version: int = Field(default=0, ge=0)
    plan_fingerprint: str | None = None
    reply_context: dict[str, str] = Field(default_factory=dict)
    last_error: str | None = None
```

`parse_bitable_url` 必须复用现有飞书域名白名单规则、只接受 `https` 和 `/wiki/<token>`，并用 `parse_qs` 比较 URL 与显式 table/view。`parse_requirement_source` 递归抽取字符串、`link` 字段和列表中的 URL，规范化后调用 `parse_feishu_url`，去重后数量必须为 1。

- [ ] **Step 4: 运行领域测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_domain.py tests/unit/test_feishu_source.py -q`

Expected: PASS，且原 docx/wiki 文档链接测试不回退。

- [ ] **Step 5: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/bitable.py feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_url.py feishu-generation-agent/tests/unit/test_bitable_domain.py
git commit -m "feat(agent): define bitable task contracts"
```

---

### Task 3: SQLite 原子领取、审批版本和事件去重

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/bitable_tasks.py`
- Create: `feishu-generation-agent/tests/unit/test_bitable_store.py`

**Interfaces:**
- Consumes: `BitableBinding`、`TableTaskStatus`。
- Produces: `BitableTaskStore.open(path)`、`claim(...) -> BitableBinding`、`get_by_record(...)`、`get_by_run(run_id)`、`set_status(...)`、`release(...)`、`advance_approval(...)`、`accept_ingress/finish_ingress` 和 `accept_action/finish_action`。

- [ ] **Step 1: 写失败测试，覆盖并发领取和 action 重放**

```python
@pytest.mark.asyncio
async def test_two_claimants_cannot_claim_same_record(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        async def claim(open_id):
            return await store.claim(
                app_token="app", table_id="tbl", view_id="vew",
                record_id="rec", source_url="https://x.feishu.cn/docx/doc",
                display_text="1", claimant_open_id=open_id,
                run_id=f"run-{open_id}", thread_id=f"thread-{open_id}",
                reply_context={},
            )

        results = await asyncio.gather(
            claim("ou_a"), claim("ou_b"), return_exceptions=True
        )
        assert sum(isinstance(item, BitableBinding) for item in results) == 1
        assert sum(isinstance(item, TaskAlreadyClaimed) for item in results) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ingress_id_is_accepted_once(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert await store.accept_ingress(
            dedupe_id="action-1", kind="approve",
            command={"run_id": "run-1", "approval_version": 2},
        )
        assert not await store.accept_ingress(
            dedupe_id="action-1", kind="approve",
            command={"run_id": "run-1", "approval_version": 2},
        )
    finally:
        await store.close()
```

- [ ] **Step 2: 运行测试，确认存储模块尚不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_store.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现 schema 和 `BEGIN IMMEDIATE` 原子领取**

Schema 必须包含：

```sql
CREATE TABLE IF NOT EXISTS bitable_tasks (
  app_token TEXT NOT NULL,
  table_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  view_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  display_text TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL UNIQUE,
  claimant_open_id TEXT NOT NULL,
  status TEXT NOT NULL,
  approval_version INTEGER NOT NULL DEFAULT 0,
  plan_fingerprint TEXT,
  reply_context_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (app_token, table_id, record_id)
);
CREATE TABLE IF NOT EXISTS bot_ingress (
  dedupe_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  command_json TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_actions (
  action_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  command_json TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`claim` 在同一个 `BEGIN IMMEDIATE` 事务中读取已有行：`active=1` 时抛 `TaskAlreadyClaimed`；终态旧行则更新为新 run/thread/claimant 并重新置 `active=1`。`bot_ingress` 按飞书 event ID 去重传输事件，`card_actions` 按卡片 payload 中的一次性 action ID 去重业务动作；两张表的 `command_json` 都只允许解析后的最小命令，禁止原始事件、token 和签名 URL。

`advance_approval(run_id, plan_fingerprint)` 只在 fingerprint 变化时递增版本并返回新绑定；同一 fingerprint 重复同步不得重复发卡。

- [ ] **Step 4: 运行存储测试并检查数据库不含敏感字段**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_store.py -q`

Expected: PASS；并发测试每次都只有一个成功者。

- [ ] **Step 5: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/storage/bitable_tasks.py feishu-generation-agent/tests/unit/test_bitable_store.py
git commit -m "feat(agent): persist atomic bitable claims"
```

---

### Task 4: 飞书多维表格字段、记录和状态适配器

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_bitable.py`
- Create: `feishu-generation-agent/tests/unit/test_feishu_bitable.py`
- Create: `feishu-generation-agent/tests/fixtures/bitable_fields.json`
- Create: `feishu-generation-agent/tests/fixtures/bitable_records.json`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/ports.py`

**Interfaces:**
- Consumes: `FeishuClient.request_json(...)`、`FeishuClient.iter_items(...)`、`BitableLocation`、`BitableTaskSummary`。
- Produces: `FeishuBitableClient.resolve_location(...)`、`ensure_schema(...)`、`list_tasks(...)`、`get_record(record_id)`、`claim_record(...)`、`write_status(...)` 和 `write_result_attachments(...)`。

- [ ] **Step 1: 写失败测试，固定 wiki 解析、字段类型与可处理记录**

```python
@pytest.mark.asyncio
async def test_resolve_location_requires_bitable_wiki_node(fake_feishu):
    fake_feishu.queue_json({
        "data": {"node": {"obj_type": "bitable", "obj_token": "appABC"}}
    })
    client = FeishuBitableClient(fake_feishu)
    resolved = await client.resolve_location(LOCATION)
    assert resolved.app_token == "appABC"


@pytest.mark.asyncio
async def test_ensure_schema_creates_only_status_and_error(fake_feishu):
    fake_feishu.queue_items(FIELDS_WITHOUT_STATUS_AND_ERROR)
    fake_feishu.queue_json({"data": {"field": {"field_id": "fldStatus"}}})
    fake_feishu.queue_json({"data": {"field": {"field_id": "fldError"}}})
    client = FeishuBitableClient(fake_feishu)
    schema = await client.ensure_schema(RESOLVED_LOCATION)
    assert schema.status_field_id == "fldStatus"
    assert schema.error_field_id == "fldError"
    assert [request["json_body"]["field_name"] for request in fake_feishu.requests[-2:]] == [
        "状态", "错误信息"
    ]


@pytest.mark.asyncio
async def test_wrong_result_field_type_is_readiness_error(fake_feishu):
    fake_feishu.queue_items(fields_with_override("结果", type_code=1))
    with pytest.raises(BitableSchemaError, match="结果.*附件"):
        await FeishuBitableClient(fake_feishu).ensure_schema(RESOLVED_LOCATION)
```

fixture 中使用飞书字段类型：`3` 单选、`11` 人员、`15` 超链接、`17` 附件、`1` 文本。`文本` 只要求可展示，不作为身份。

- [ ] **Step 2: 运行测试并确认适配器尚不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_bitable.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现官方端点的窄适配**

```python
class FeishuBitableClient:
    async def resolve_location(self, location: BitableLocation) -> BitableLocation:
        payload = await self._client.request_json(
            "GET", "/open-apis/wiki/v2/spaces/get_node",
            params={"token": location.wiki_token},
        )
        node = payload.get("data", {}).get("node", {})
        if node.get("obj_type") != "bitable":
            raise BitableSchemaError("配置的 wiki 节点不是多维表格")
        return location.model_copy(update={"app_token": node["obj_token"]})

    def _base(self, location: BitableLocation) -> str:
        if not location.app_token:
            raise BitableSchemaError("多维表格 app_token 尚未解析")
        return (
            f"/open-apis/bitable/v1/apps/{location.app_token}"
            f"/tables/{location.table_id}"
        )

    async def list_tasks(
        self, location: BitableLocation, schema: BitableSchema
    ) -> list[BitableTaskSummary]:
        records = await self._client.iter_items(
            f"{self._base(location)}/records",
            params={"view_id": location.view_id},
        )
        return [
            task for record in records
            if (task := self._to_summary(record, schema)) is not None
        ]
```

`ensure_schema` 列出 `/fields`，校验 `需求来源` 可解析、`执行人` 为人员、`结果` 为附件；缺失 `状态` 时 POST type `3` 并创建八个中文选项，缺失 `错误信息` 时 POST type `1`。既有字段类型错误时不得 POST 或修改字段。

记录更新统一调用：

```python
await self._client.request_json(
    "PUT",
    f"{self._base(location)}/records/{record_id}",
    json_body={"fields": fields},
)
```

`claim_record` 写人员值、`处理中` 和空错误；`write_status` 只写状态/错误；`write_result_attachments` 写入 `[{"file_token": token, "name": name}, ...]`。

- [ ] **Step 4: 运行表格适配器与 URL 测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_bitable.py tests/unit/test_bitable_domain.py -q`

Expected: PASS；请求 fixture 断言 endpoint、view_id 和字段 payload 精确匹配。

- [ ] **Step 5: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_bitable.py feishu-generation-agent/src/feishu_generation_agent/ports.py feishu-generation-agent/tests/unit/test_feishu_bitable.py feishu-generation-agent/tests/fixtures/bitable_fields.json feishu-generation-agent/tests/fixtures/bitable_records.json
git commit -m "feat(agent): add feishu bitable adapter"
```

---

### Task 5: 表格附件上传、结果冲突保护和交付路由

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_delivery.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/routing_delivery.py`
- Create: `feishu-generation-agent/tests/unit/test_bitable_delivery.py`
- Create: `feishu-generation-agent/tests/unit/test_routing_delivery.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/artifact.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_delivery.py`
- Modify: `feishu-generation-agent/tests/unit/test_feishu_delivery.py`
- Modify: `feishu-generation-agent/tests/conftest.py`

**Interfaces:**
- Consumes: `Artifact`、`DeliveryWriter`、`BitableTaskStore.get_by_run`、`FeishuBitableClient.get_record/write_result_attachments`。
- Produces: `BitableResultWriter` 和 `RoutingDeliveryWriter`，两者实现现有 `DeliveryWriter` 协议。

- [ ] **Step 1: 写失败测试，固定多附件、上传复用和冲突失败**

```python
@pytest.mark.asyncio
async def test_bitable_delivery_writes_all_artifacts_once(binding, artifacts):
    feishu = FakeBitableDeliveryClient(result_attachments=[])
    writer = BitableResultWriter(feishu, task_store, repository)
    record = await writer.deliver(
        binding.run_id, normalized_document, task_plan, artifacts
    )
    assert record.target_type == "bitable_record"
    assert record.record_id == binding.record_id
    assert [item["name"] for item in feishu.written_attachments] == [
        artifact.local_path.name for artifact in artifacts
    ]

    await writer.retry_delivery(binding.run_id)
    assert feishu.upload_calls == len(artifacts)
    assert feishu.record_update_calls == 1


@pytest.mark.asyncio
async def test_bitable_delivery_never_overwrites_existing_result(binding, artifacts):
    feishu = FakeBitableDeliveryClient(
        result_attachments=[{"file_token": "external", "name": "manual.png"}]
    )
    writer = BitableResultWriter(feishu, task_store, repository)
    with pytest.raises(BitableResultConflict, match="结果冲突"):
        await writer.deliver(binding.run_id, normalized_document, task_plan, artifacts)
    assert feishu.upload_calls == 0
    assert feishu.record_update_calls == 0
```

- [ ] **Step 2: 运行测试，确认当前交付只支持 docx**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_delivery.py tests/unit/test_routing_delivery.py -q`

Expected: FAIL，提示 `DeliveryRecord` 没有 `target_type` 或模块不存在。

- [ ] **Step 3: 扩展交付记录并参数化飞书上传目标**

`DeliveryRecord` 改为：

```python
class DeliveryRecord(BaseModel):
    target_type: Literal["docx", "bitable_record"] = "docx"
    status: str
    uploaded_artifact_ids: list[str] = Field(default_factory=list)
    document_id: str | None = None
    document_url: str | None = None
    app_token: str | None = None
    table_id: str | None = None
    record_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.target_type == "docx":
            if not self.document_id or not self.document_url:
                raise ValueError("docx delivery requires document identity")
        elif not self.app_token or not self.table_id or not self.record_id:
            raise ValueError("bitable delivery requires record identity")
        return self
```

在 `FeishuClient` 新增参数化方法：

```python
async def upload_file_all_to(
    self, *, parent_type: str, parent_node: str,
    filename: str, content: bytes, mime_type: str,
) -> str:
    payload = await self._request_multipart(
        "/open-apis/drive/v1/files/upload_all",
        data={
            "file_name": filename, "parent_type": parent_type,
            "parent_node": parent_node, "size": str(len(content)),
        },
        filename=filename, content=content, mime_type=mime_type,
    )
    return self._file_token(payload, "upload_all")
```

大文件 prepare/part/finish 同样接受并持久化 `parent_type="bitable_file"`、`parent_node=app_token`；现有 explorer 方法委托给新方法，保持旧测试通过。

- [ ] **Step 4: 实现幂等结果写入与路由**

`BitableResultWriter` 的顺序固定为：

```python
binding = await self._tasks.get_by_run(run_id)
record = await self._bitable.get_record(binding)
if record.result_attachments:
    raise BitableResultConflict("结果冲突，需要人工确认")
uploaded = [await self._ensure_uploaded(binding, artifact) for artifact in artifacts]
await self._bitable.write_result_attachments(
    binding,
    [{"file_token": token, "name": artifact.local_path.name}
     for artifact, token in uploaded],
)
return DeliveryRecord(
    target_type="bitable_record", status="succeeded",
    app_token=binding.app_token, table_id=binding.table_id,
    record_id=binding.record_id,
    uploaded_artifact_ids=[item.artifact_id for item in artifacts],
)
```

上传 operation 名称使用 `bitable_upload:<artifact_id>`；完成 token 同时写回 artifact。`retry_delivery` 从 binding 和 repository 加载现有 artifacts，复用 token 后只重试记录更新。

`RoutingDeliveryWriter.deliver/retry_delivery` 先查 `get_by_run(run_id)`：有绑定调用 `BitableResultWriter`，无绑定调用可选旧 writer；旧 writer 未配置时抛出“旧文档交付未配置”，不能误走表格。

- [ ] **Step 5: 运行新旧交付测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_delivery.py tests/unit/test_routing_delivery.py tests/unit/test_feishu_delivery.py -q`

Expected: PASS；同一 run 的回写重试不增加上传次数，旧 docx 交付断言保持通过。

- [ ] **Step 6: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/artifact.py feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_delivery.py feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_delivery.py feishu-generation-agent/src/feishu_generation_agent/integrations/routing_delivery.py feishu-generation-agent/tests/unit/test_bitable_delivery.py feishu-generation-agent/tests/unit/test_routing_delivery.py feishu-generation-agent/tests/unit/test_feishu_delivery.py feishu-generation-agent/tests/conftest.py
git commit -m "feat(agent): write artifacts to bitable results"
```

---

### Task 6: 扫描服务、原子领取与预留运行身份

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/bitable/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/bitable/service.py`
- Create: `feishu-generation-agent/tests/unit/test_bitable_service.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Modify: `feishu-generation-agent/tests/integration/test_api.py`

**Interfaces:**
- Consumes: `FeishuBitableClient`、`BitableTaskStore`、`GraphRuntime.start_run` 和 `RequirementRequest`。
- Produces: `BitableTaskService.prepare()`、`scan()`、`claim(record_id, claimant_open_id, reply_context)`；`GraphRuntime.start_run(request, *, run_id=None, thread_id=None)`。

- [ ] **Step 1: 写失败测试，固定扫描过滤和领取补偿顺序**

```python
@pytest.mark.asyncio
async def test_scan_excludes_active_and_completed_records(service, task_store):
    tasks = await service.scan()
    assert [task.record_id for task in tasks] == ["rec-pending", "rec-retryable"]


@pytest.mark.asyncio
async def test_claim_reserves_local_identity_before_feishu_write(service, fakes):
    binding = await service.claim(
        "rec-pending", claimant_open_id="ou_claimant",
        reply_context={"chat_id": "oc_chat"},
    )
    assert fakes.calls[:3] == [
        "store.claim", "bitable.claim_record", "runtime.start_run"
    ]
    assert binding.run_id == fakes.runtime.started_run_id
    assert binding.thread_id == fakes.runtime.started_thread_id


@pytest.mark.asyncio
async def test_feishu_claim_failure_does_not_start_graph(service, fakes):
    fakes.bitable.claim_error = PermissionError("forbidden")
    with pytest.raises(ClaimSyncError):
        await service.claim("rec-pending", claimant_open_id="ou_claimant", reply_context={})
    assert fakes.runtime.start_calls == 0
    assert (await fakes.store.get_by_record("app", "tbl", "rec-pending")).status.value == "失败"
```

- [ ] **Step 2: 运行测试并确认服务尚不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_service.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 允许 GraphRuntime 使用预留 ID**

```python
async def start_run(
    self,
    request: RequirementRequest,
    *,
    run_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    if self._closed:
        raise RunConflict("运行时正在关闭")
    run_id = run_id or str(uuid4())
    thread_id = thread_id or str(uuid4())
    existing = await self.repository.get_run(run_id)
    if existing is not None:
        if existing["thread_id"] != thread_id:
            raise RunConflict("预留运行身份不一致")
        return run_id
    await self.repository.create_run(
        run_id, thread_id, request.source_url, status="created"
    )
    self._start_background(
        self._run_to_approval(run_id, thread_id, request),
        name=f"approval-run-{run_id}",
    )
    return run_id
```

保留无参数调用的原行为，现有 API 测试必须继续通过。

- [ ] **Step 4: 实现扫描和领取**

`prepare` 解析/解析 wiki app token、保证 schema，并缓存本次进程内的已验证 location/schema。`scan` 用字段值和本地绑定共同过滤：结果非空、非可领取状态、活动绑定一律排除；`失败` 只有本地旧绑定 `active=0` 时才允许重领。

`claim` 生成 UUID 后依次执行：

```python
binding = await self._store.claim(
    app_token=location.app_token, table_id=location.table_id,
    view_id=location.view_id, record_id=task.record_id,
    source_url=task.source_url, display_text=task.display_text,
    claimant_open_id=claimant_open_id, run_id=str(uuid4()),
    thread_id=str(uuid4()), reply_context=reply_context,
)
try:
    await self._bitable.claim_record(binding)
except Exception as exc:
    await self._store.mark_claim_sync_failed(binding.run_id, safe_message(exc))
    raise ClaimSyncError("飞书领取同步失败，可安全重试") from None
await self._runtime.start_run(
    RequirementRequest(
        source_url=binding.source_url,
        requester_open_id=claimant_open_id,
        trigger_type="bitable",
        reply_context=reply_context,
    ),
    run_id=binding.run_id,
    thread_id=binding.thread_id,
)
return binding
```

- [ ] **Step 5: 运行服务和既有 API 测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bitable_service.py tests/integration/test_api.py -q`

Expected: PASS；旧 `POST /api/runs` 仍能自动生成 run/thread ID。

- [ ] **Step 6: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/bitable/__init__.py feishu-generation-agent/src/feishu_generation_agent/bitable/service.py feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py feishu-generation-agent/tests/unit/test_bitable_service.py feishu-generation-agent/tests/integration/test_api.py
git commit -m "feat(agent): scan and claim bitable tasks"
```

---

### Task 7: 统一协调器、状态映射、审批版本与重启恢复

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/bitable/coordinator.py`
- Create: `feishu-generation-agent/tests/unit/test_task_coordinator.py`
- Create: `feishu-generation-agent/tests/integration/test_bitable_restart.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/ports.py`

**Interfaces:**
- Consumes: `BitableTaskService`、`BitableTaskStore`、`GraphRuntime.get_run_view/resume_pending_runs`、`FeishuBitableClient.write_status`。
- Produces: `TaskCoordinator.start()`、`close()`、`scan()`、`claim()`、`sync_once(run_id)`、`resume_incomplete()` 和 `ApprovalNotifier` protocol。

- [ ] **Step 1: 写失败测试，固定内部→飞书状态和方案版本**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_status", "table_status"),
    [
        ("created", "处理中"),
        ("running", "处理中"),
        ("waiting_approval", "待审批"),
        ("resuming", "生成中"),
        ("waiting_provider", "生成中"),
        ("delivering", "回写中"),
        ("succeeded", "已完成"),
        ("failed", "失败"),
        ("delivery_failed", "回写失败"),
    ],
)
async def test_sync_maps_runtime_status(runtime_status, table_status, coordinator):
    coordinator.runtime.view["status"] = runtime_status
    await coordinator.sync_once("run-1")
    assert coordinator.bitable.last_status.value == table_status


@pytest.mark.asyncio
async def test_same_plan_is_not_notified_twice(coordinator):
    coordinator.runtime.view = waiting_view(prompt="第一版")
    await coordinator.sync_once("run-1")
    await coordinator.sync_once("run-1")
    assert coordinator.notifier.approval_calls == 1
    assert coordinator.notifier.last_version == 1

    coordinator.runtime.view = waiting_view(prompt="第二版")
    await coordinator.sync_once("run-1")
    assert coordinator.notifier.approval_calls == 2
    assert coordinator.notifier.last_version == 2
```

- [ ] **Step 2: 运行测试并确认协调器尚不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_task_coordinator.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现稳定 fingerprint 和状态同步**

```python
def plan_fingerprint(view: dict[str, Any]) -> str:
    approval = view.get("approval", {})
    payload = {
        "document_revision": approval.get("document_revision"),
        "tasks": approval.get("tasks", []),
        "validation_issues": approval.get("validation_issues", []),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


_TABLE_STATUS_BY_RUNTIME = {
    "created": TableTaskStatus.PROCESSING,
    "running": TableTaskStatus.PROCESSING,
    "waiting_approval": TableTaskStatus.WAITING_APPROVAL,
    "resuming": TableTaskStatus.GENERATING,
    "waiting_provider": TableTaskStatus.GENERATING,
    "verification_pending": TableTaskStatus.GENERATING,
    "delivering": TableTaskStatus.WRITING_BACK,
    "succeeded": TableTaskStatus.COMPLETED,
    "completed_with_errors": TableTaskStatus.FAILED,
    "failed": TableTaskStatus.FAILED,
    "delivery_failed": TableTaskStatus.WRITEBACK_FAILED,
}
```

`sync_once` 获取 view、写表格状态和安全错误；进入 `waiting_approval` 时按 fingerprint 调用 `advance_approval`，只有版本增加才调用 notifier。`completed_with_errors` 保留已有附件但状态为 `失败`，错误信息提示部分任务失败，不再把该记录扫描为新任务。

- [ ] **Step 4: 实现监控生命周期和恢复**

```python
async def start(self) -> None:
    self._closed = False
    await self.resume_incomplete()
    self._monitor = asyncio.create_task(self._monitor_loop(), name="bitable-monitor")


async def resume_incomplete(self) -> None:
    await self._runtime.resume_pending_runs()
    for binding in await self._store.list_active():
        run = await self._runtime.repository.get_run(binding.run_id)
        if run is None:
            await self._runtime.start_run(
                RequirementRequest(
                    source_url=binding.source_url,
                    requester_open_id=binding.claimant_open_id,
                    trigger_type="bitable",
                    reply_context=binding.reply_context,
                ),
                run_id=binding.run_id,
                thread_id=binding.thread_id,
            )
        await self.sync_once(binding.run_id)
```

`close` 取消 monitor 并 await；不关闭由外部拥有的 runtime/client。待审批恢复会用相同 fingerprint/version 重新通知一次启动恢复卡，但不会修改版本或自动批准。

- [ ] **Step 5: 运行协调器与重启测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_task_coordinator.py tests/integration/test_bitable_restart.py -q`

Expected: PASS；重启 fixture 中 provider `submit_calls` 保持 1，待审批无 submit。

- [ ] **Step 6: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/bitable/coordinator.py feishu-generation-agent/src/feishu_generation_agent/ports.py feishu-generation-agent/tests/unit/test_task_coordinator.py feishu-generation-agent/tests/integration/test_bitable_restart.py
git commit -m "feat(agent): coordinate bitable workflow recovery"
```

---

### Task 8: 共享审批动作、领取者鉴权和旧卡失效

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bitable/coordinator.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/bitable_tasks.py`
- Modify: `feishu-generation-agent/tests/unit/test_task_coordinator.py`
- Modify: `feishu-generation-agent/tests/unit/test_bitable_store.py`

**Interfaces:**
- Consumes: `GraphRuntime.resume_run(run_id, ApprovalDecision)` 和 `BitableBinding.approval_version`。
- Produces: `approve(...)`、`replan(...)`、`cancel(...)` 和 `ApprovalActionConflict`。

- [ ] **Step 1: 写失败测试，固定权限、版本与 action 幂等**

```python
@pytest.mark.asyncio
async def test_only_claimant_can_approve_current_version(coordinator):
    with pytest.raises(ApprovalActionConflict, match="领取者"):
        await coordinator.approve(
            run_id="run-1", actor_open_id="ou_other",
            approval_version=1, selected_task_ids=["task-1"],
            action_id="action-other",
        )
    with pytest.raises(ApprovalActionConflict, match="最新"):
        await coordinator.approve(
            run_id="run-1", actor_open_id="ou_owner",
            approval_version=0, selected_task_ids=["task-1"],
            action_id="action-old",
        )


@pytest.mark.asyncio
async def test_duplicate_approve_action_resumes_once(coordinator):
    await coordinator.approve(
        run_id="run-1", actor_open_id="ou_owner",
        approval_version=1, selected_task_ids=["task-1"],
        action_id="action-1",
    )
    result = await coordinator.approve(
        run_id="run-1", actor_open_id="ou_owner",
        approval_version=1, selected_task_ids=["task-1"],
        action_id="action-1",
    )
    assert result.replayed is True
    assert coordinator.runtime.resume_calls == 1
```

- [ ] **Step 2: 运行目标测试并确认方法不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_task_coordinator.py -q`

Expected: FAIL with `AttributeError: approve`。

- [ ] **Step 3: 实现统一动作验证和决定构造**

```python
async def _accept_action(
    self, *, action_id: str, run_id: str,
    actor_open_id: str, approval_version: int, kind: str,
) -> tuple[BitableBinding, bool]:
    binding = await self._store.get_by_run(run_id)
    if binding is None:
        raise ApprovalActionConflict("任务不存在")
    if binding.claimant_open_id != actor_open_id:
        raise ApprovalActionConflict("只有当前领取者可以操作")
    if binding.approval_version != approval_version:
        raise ApprovalActionConflict("方案已更新，请使用最新审批卡")
    accepted = await self._store.accept_action(
        action_id=action_id, kind=kind,
        command={
            "run_id": run_id, "actor_open_id": actor_open_id,
            "approval_version": approval_version,
        },
    )
    return binding, not accepted
```

`approve` 从 `get_run_view(run_id)["approval"]["tasks"]` 用 `GenerationTask.model_validate` 还原当前任务，把完整当前 tasks 和选中 ID 写入 `ApprovalDecision(action="approve", ...)`。`replan` 要求非空 feedback，构造 `reject`；`cancel` 构造 `cancel`。成功或失败都用 `finish_action` 记录脱敏结果，重复 action 返回第一次结果。

取消完成后由 `sync_once` 清空飞书执行人、写 `待处理`、调用 `store.release(active=0)`；生成开始后 GraphRuntime 已不处于 `waiting_approval`，取消自然返回冲突。

- [ ] **Step 4: 运行审批与现有 LangGraph 测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_task_coordinator.py tests/unit/test_bitable_store.py tests/graph/test_approval_graph.py -q`

Expected: PASS；重复 action 不增加 `resume_calls`，旧审批版本不进入 graph。

- [ ] **Step 5: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/bitable/coordinator.py feishu-generation-agent/src/feishu_generation_agent/storage/bitable_tasks.py feishu-generation-agent/tests/unit/test_task_coordinator.py feishu-generation-agent/tests/unit/test_bitable_store.py
git commit -m "feat(agent): guard bitable approval actions"
```

---

### Task 9: 飞书任务列表卡、审批卡和结果卡

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/bot/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/bot/cards.py`
- Create: `feishu-generation-agent/tests/unit/test_bot_cards.py`

**Interfaces:**
- Consumes: `BitableTaskSummary`、run view、`BitableBinding`。
- Produces: `render_task_list_card(...)`、`render_approval_card(...)`、`render_status_card(...)`，返回可 JSON 序列化 dict。

- [ ] **Step 1: 写失败测试，固定完整计划、选择控件和动作上下文**

```python
def test_approval_card_contains_every_task_and_bound_action():
    card = render_approval_card(
        binding=BINDING.model_copy(update={"approval_version": 3}),
        run_view=waiting_view_with_two_tasks(),
        action_ids={
            "approve": "act-approve", "replan": "act-replan", "cancel": "act-cancel"
        },
    )
    serialized = json.dumps(card, ensure_ascii=False)
    assert "任务一完整提示词" in serialized
    assert "任务二完整提示词" in serialized
    assert "task-1" in serialized and "task-2" in serialized
    assert '"approval_version": 3' in serialized
    assert "multi_select_static" in serialized


def test_task_list_card_never_contains_source_document_body():
    card = render_task_list_card(TASKS, action_ids={"rec-1": "act-1"})
    serialized = json.dumps(card, ensure_ascii=False)
    assert "领取并分析" in serialized
    assert "文档完整正文" not in serialized
```

- [ ] **Step 2: 运行测试并确认卡片模块不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bot_cards.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现 schema 2.0 纯函数卡片**

审批卡根结构固定为：

```python
{
    "schema": "2.0",
    "config": {"update_multi": True, "wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": "AI 生成方案审批"},
        "template": "blue",
    },
    "body": {
        "elements": [
            summary_markdown,
            *task_markdown_elements,
            {
                "tag": "form",
                "name": "approval_form",
                "elements": [
                    {
                        "tag": "multi_select_static",
                        "name": "selected_task_ids",
                        "placeholder": {"tag": "plain_text", "content": "选择要执行的任务"},
                        "options": task_options,
                    },
                    approve_button,
                    replan_input,
                    replan_button,
                    cancel_button,
                ],
            },
        ]
    },
}
```

每个 button callback value 只含 `action_id`、`kind`、`run_id`、`record_id`、`approval_version`；不含 key、原图、完整源文档或供应商 URL。所有用户文本先限制长度并转义卡片 Markdown；任务很多时分多个计划块，但每项任务的完整 prompt 仍必须出现。

- [ ] **Step 4: 运行卡片测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bot_cards.py -q`

Expected: PASS，且对卡片执行 `json.dumps` 不报错。

- [ ] **Step 5: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/bot/__init__.py feishu-generation-agent/src/feishu_generation_agent/bot/cards.py feishu-generation-agent/tests/unit/test_bot_cards.py
git commit -m "feat(agent): render feishu approval cards"
```

---

### Task 10: 持久化机器人网关与飞书长连接适配

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/bot/gateway.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/bot/lark_channel.py`
- Create: `feishu-generation-agent/tests/unit/test_bot_gateway.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/ports.py`

**Interfaces:**
- Consumes: `TaskCoordinator`、`BitableTaskStore.accept_ingress/finish_ingress`、卡片渲染函数。
- Produces: `BotGateway.start/close`、`handle_message(command)`、`handle_card_action(command)`、`LarkChannelConnection.start/close`。

- [ ] **Step 1: 写失败测试，固定快速确认、事件去重和后台执行**

```python
@pytest.mark.asyncio
async def test_message_is_persisted_before_background_scan(gateway, store):
    response = await gateway.handle_message({
        "event_id": "evt-1", "sender_open_id": "ou_a",
        "chat_id": "oc_a", "chat_type": "group",
        "mentioned_bot": True, "text": "@机器人 扫描任务",
    })
    assert response.accepted is True
    assert await store.get_ingress("evt-1") is not None
    assert gateway.coordinator.scan_calls == 0
    await gateway.drain_once()
    assert gateway.coordinator.scan_calls == 1


@pytest.mark.asyncio
async def test_replayed_message_is_acknowledged_without_second_scan(gateway):
    command = {
        "event_id": "evt-1", "sender_open_id": "ou_a",
        "chat_id": "oc_a", "chat_type": "group",
        "mentioned_bot": True, "text": "@机器人 扫描任务",
    }
    await gateway.handle_message(command)
    await gateway.handle_message(command)
    await gateway.drain_once()
    assert gateway.coordinator.scan_calls == 1
```

- [ ] **Step 2: 运行测试并确认网关尚不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bot_gateway.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现与 SDK 无关的持久化网关**

`handle_message` 只接受单聊或群聊明确提及机器人且文本规范化为 `扫描任务` 的命令；写入最小 command 后立即返回。`handle_card_action` 校验必需值，写入 action ID 后立即返回“已受理”。`drain_once` 领取一条 pending ingress，调用 coordinator，再发送对应卡片；异常写脱敏错误并允许按状态安全重试。

```python
async def handle_card_action(self, command: CardActionCommand) -> Ack:
    accepted = await self._store.accept_ingress(
        dedupe_id=command.event_id,
        kind=command.kind,
        command=command.model_dump(mode="json"),
    )
    self._wake.set()
    return Ack(accepted=True, replayed=not accepted)
```

- [ ] **Step 4: 用窄适配层接入飞书官方 SDK**

`lark_channel.py` 定义 `LarkChannelConnection`，且是唯一导入 `lark_channel` 的模块。使用官方异步 Channel 生命周期，不启动第二套事件循环或阻塞线程。消息/卡片 handler 只解析允许字段、await 本地持久化并返回；绝不在 handler 内调用模型。卡片回调的飞书 event ID 用于 `bot_ingress` 去重，卡片 value 中的 action ID 由协调器的 `card_actions` 再次去重，因此飞书以新 event ID 重放旧动作也不会重复执行。

```python
from lark_channel import (
    Events,
    FeishuChannel,
    PolicyConfig,
    SecurityConfig,
)

self._channel = FeishuChannel(
    app_id=app_id,
    app_secret=app_secret,
    transport="ws",
    policy=PolicyConfig(
        dm_policy="open",
        group_policy="open",
        require_mention=True,
    ),
    security=SecurityConfig(mode="audit", strict_content_text=True),
)
self._channel.on(Events.MESSAGE, on_message)
self._channel.on(Events.CARD_ACTION, on_card_action)

async def start(self) -> None:
    await self._channel.connect_until_ready(timeout=30)

async def close(self) -> None:
    await self._channel.disconnect()
```

发送任务卡和审批卡统一调用 `await channel.send(chat_id, {"card": card_json}, opts)`；更新已发送审批卡调用 `await channel.update_card(message_id, card_json)`。测试使用 fake channel，不访问飞书。

- [ ] **Step 5: 运行网关、卡片和去重测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_bot_gateway.py tests/unit/test_bot_cards.py tests/unit/test_bitable_store.py -q`

Expected: PASS；处理函数在测试超时 0.5 秒内返回，后台 drain 才执行扫描/审批。

- [ ] **Step 6: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/bot/gateway.py feishu-generation-agent/src/feishu_generation_agent/bot/lark_channel.py feishu-generation-agent/src/feishu_generation_agent/ports.py feishu-generation-agent/tests/unit/test_bot_gateway.py
git commit -m "feat(agent): add persistent feishu bot gateway"
```

---

### Task 11: 本地扫描、领取、状态与表格审批 API/UI

**Files:**
- Create: `feishu-generation-agent/tests/integration/test_bitable_api.py`
- Create: `feishu-generation-agent/tests/frontend/bitable_state.test.cjs`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css`

**Interfaces:**
- Consumes: `TaskCoordinator.scan/claim/approve/replan/cancel` 和现有 `GraphRuntime`。
- Produces: `GET /api/bitable/tasks`、`POST /api/bitable/tasks/{record_id}/claim`、`GET /api/bitable/tasks/{record_id}`、`POST /api/bitable/runs/{run_id}/decision`。

- [ ] **Step 1: 写失败 API 测试，固定本地身份和冲突响应**

```python
@pytest.mark.asyncio
async def test_scan_and_claim_bitable_task(app_with_coordinator):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_coordinator),
        base_url="http://test",
    ) as client:
        scan = await client.get("/api/bitable/tasks")
        assert scan.status_code == 200
        assert scan.json()["tasks"][0]["record_id"] == "rec-1"

        claim = await client.post("/api/bitable/tasks/rec-1/claim")
        assert claim.status_code == 202
        assert claim.json()["claimant_open_id"] == "ou_local"


@pytest.mark.asyncio
async def test_claim_is_disabled_without_local_operator(app_without_operator):
    async with AsyncClient(
        transport=ASGITransport(app=app_without_operator),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/bitable/tasks/rec-1/claim")
        assert response.status_code == 503
        assert "LARK_LOCAL_OPERATOR_OPEN_ID" in response.json()["detail"]
```

- [ ] **Step 2: 运行 API 测试并确认路由不存在**

Run: `cd feishu-generation-agent && uv run pytest tests/integration/test_bitable_api.py -q`

Expected: FAIL with HTTP 404。

- [ ] **Step 3: 增加严格 schema 和 API**

```python
class BitableDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject", "cancel"]
    approval_version: int = Field(ge=1)
    selected_task_ids: list[str] = Field(default_factory=list)
    feedback: str | None = None
    action_id: str = Field(min_length=8, max_length=128)
```

本地 API 不接收 actor open_id，始终使用 `settings.lark_local_operator_open_id`，避免浏览器伪造身份。领取冲突返回 409，字段/权限准备度错误返回 422/503，内部异常只返回安全中文摘要。

`create_app` 增加可选 `coordinator` 注入；测试仍可只注入原 `runtime`。应用 state 同时保存 runtime/coordinator，兼容旧路由。

- [ ] **Step 4: 写失败前端状态测试并实现扫描工作区**

导出无 DOM 的状态函数：

```javascript
function reduceBitableState(state, event) {
  if (event.type === "scan-start") return { ...state, loading: true, error: "" };
  if (event.type === "scan-success") {
    return { ...state, loading: false, tasks: event.tasks, error: "" };
  }
  if (event.type === "claim-success") {
    return {
      ...state,
      loading: false,
      tasks: state.tasks.filter((task) => task.record_id !== event.record_id),
      activeRunId: event.run_id,
    };
  }
  if (event.type === "failure") {
    return { ...state, loading: false, error: event.message };
  }
  return state;
}
```

Run: `cd feishu-generation-agent && node --test tests/frontend/bitable_state.test.cjs`

Expected before implementation: FAIL；实现并从 `app.js` 复用后 PASS。

页面增加“扫描多维表格任务”按钮、任务列表、状态/执行人、需求来源打开链接和“领取并分析”。领取后复用现有运行详情/本地完整审批界面，不复制第二套审批组件。未配置本地 open_id 时显示“只能扫描，不能领取”。

- [ ] **Step 5: 运行 API、前端和旧前端测试**

Run: `cd feishu-generation-agent && uv run pytest tests/integration/test_bitable_api.py tests/integration/test_api.py -q && node --test tests/frontend/*.test.cjs`

Expected: Python tests PASS；全部 Node tests PASS。

- [ ] **Step 6: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/web/schemas.py feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/src/feishu_generation_agent/web/static/index.html feishu-generation-agent/src/feishu_generation_agent/web/static/app.js feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css feishu-generation-agent/tests/integration/test_bitable_api.py feishu-generation-agent/tests/frontend/bitable_state.test.cjs
git commit -m "feat(agent): add local bitable task workspace"
```

---

### Task 12: 服务组装、机器人生命周期与能力探针

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/cli/config_probe.py`
- Modify: `feishu-generation-agent/tests/unit/test_config_probe.py`
- Modify: `feishu-generation-agent/tests/integration/test_bitable_restart.py`

**Interfaces:**
- Consumes: Tasks 1–11 的全部实现。
- Produces: `ApplicationServices` 生命周期、按能力组装的 delivery router、可选 BotGateway 和分项 probe 结果。

- [ ] **Step 1: 写失败测试，固定表格模式不需要旧交付配置**

```python
@pytest.mark.asyncio
async def test_open_table_services_without_legacy_delivery_fields(table_settings):
    table_settings.lark_output_owner_open_id = None
    table_settings.lark_output_folder_token = None
    async with open_services(table_settings) as services:
        assert services.bitable_client is not None
        assert services.graph_services.delivery_writer is services.routing_delivery


@pytest.mark.asyncio
async def test_probe_reports_bitable_bot_and_legacy_separately(table_settings):
    result = await probe(table_settings, network=False)
    assert result["capabilities"]["bitable"]["configured"] is True
    assert result["capabilities"]["bot"]["configured"] is True
    assert result["capabilities"]["legacy_delivery"]["configured"] is False
    assert result["ready"] is True
```

- [ ] **Step 2: 运行测试并确认旧 bootstrap 仍全局 require 旧字段**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_config_probe.py tests/integration/test_bitable_restart.py -q`

Expected: FAIL，提示 `LARK_OUTPUT_OWNER_OPEN_ID` 或 `LARK_OUTPUT_FOLDER_TOKEN`。

- [ ] **Step 3: 建立明确的服务所有权和组装顺序**

```python
@dataclass(slots=True)
class ApplicationServices:
    graph_services: GraphServices
    task_store: BitableTaskStore | None
    bitable_client: FeishuBitableClient | None
    bitable_location: BitableLocation | None
    routing_delivery: RoutingDeliveryWriter


@dataclass(slots=True)
class RuntimeContext:
    runtime: GraphRuntime
    bitable_service: BitableTaskService | None
    coordinator: TaskCoordinator | None
    bot_gateway: BotGateway | None
```

`open_services` 顺序：

1. require `core`。
2. 打开一个现有 `Repository` 和一个同库路径的 `BitableTaskStore`。
3. 表格配置存在时解析 location、创建 `FeishuBitableClient` 和 `BitableResultWriter`。
4. 旧交付配置存在时创建 `FeishuDeliveryWriter`，否则 legacy writer 为 None。
5. 用 `RoutingDeliveryWriter` 注入 `GraphServices`。
6. web lifespan 用静态 `ApplicationServices` 创建 `GraphRuntime`，随后组装 `RuntimeContext` 中的 `BitableTaskService/TaskCoordinator`。
7. 先 `coordinator.start()`，再在 `lark_bot_enabled=true` 时创建并启动 `BotGateway`；关闭时按 bot→coordinator→runtime→clients→stores 逆序关闭。

若表格和旧交付都未配置，应用仍可启动健康页，但运行时 `ready=false`，不得部分执行到交付才发现配置缺失。

- [ ] **Step 4: 实现分项无付费探针**

`config_probe` 输出至少：

```python
for capability in (
    "feishu_auth", "feishu_docx_read", "feishu_wiki_read",
    "bitable", "bitable_schema", "bitable_record_write",
    "bitable_attachment_write", "bot", "legacy_delivery",
    "deepseek", "claude_vision", "chiyun", "seedance",
):
    checks[capability] = result_for(capability)
```

`--network` 表格探针只做读取和字段类型检查；写权限探针默认报告“需专用测试记录验证”，只有显式 `--bitable-test-record <record_id>` 才允许写入并恢复原状态/错误字段。模型探针验证配置模型在账户可见列表中；无法列出时做不产生生成费用的最小鉴权检查，不发送生成请求。

- [ ] **Step 5: 运行探针、恢复与全部单元测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_config.py tests/unit/test_config_probe.py tests/integration/test_bitable_restart.py -q`

Expected: PASS；table-only fixture `ready=true`，legacy-only fixture 保持兼容。

- [ ] **Step 6: 提交**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/bootstrap.py feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/src/feishu_generation_agent/cli/config_probe.py feishu-generation-agent/tests/unit/test_config_probe.py feishu-generation-agent/tests/integration/test_bitable_restart.py
git commit -m "feat(agent): wire bitable bot runtime capabilities"
```

---

### Task 13: 操作文档、零生成联调、付费门禁与全量验收

**Files:**
- Modify: `feishu-generation-agent/README.md`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/cli/smoke.py`
- Modify: `feishu-generation-agent/tests/integration/test_bitable_api.py`
- Modify: `feishu-generation-agent/tests/integration/test_bitable_restart.py`
- Modify: `feishu-generation-agent/.env.example`

**Interfaces:**
- Consumes: 完整应用。
- Produces: `agent-config-probe` 的表格/机器人检查说明、`agent-smoke --mode bitable --record-id ...` 和本地上线手册。

- [ ] **Step 1: 增加端到端 Fake 测试，固定扫描到回写的完整边界**

```python
@pytest.mark.asyncio
async def test_bitable_flow_claims_approves_and_attaches_results(full_fake_app):
    client, fakes = full_fake_app
    tasks = (await client.get("/api/bitable/tasks")).json()["tasks"]
    claimed = await client.post(f"/api/bitable/tasks/{tasks[0]['record_id']}/claim")
    run_id = claimed.json()["run_id"]
    await fakes.runtime.wait_until_waiting(run_id)
    binding = await fakes.task_store.get_by_run(run_id)

    approved = await client.post(
        f"/api/bitable/runs/{run_id}/decision",
        json={
            "action": "approve",
            "approval_version": binding.approval_version,
            "selected_task_ids": ["task-image", "task-video"],
            "feedback": None,
            "action_id": "local-approve-0001",
        },
    )
    assert approved.status_code == 202
    await fakes.runtime.wait_for_terminal(run_id)
    assert fakes.image_generator.submit_calls == 1
    assert fakes.video_generator.submit_calls == 1
    assert len(fakes.bitable.result_attachments) == 2
    assert fakes.bitable.status == "已完成"
```

同时增加：

- 重放同一 approve action，submit 次数保持 1。
- 重启后恢复 provider official ID，只 poll 不 submit。
- 制造记录更新失败后状态为 `回写失败`；retry 后生成 submit 次数不变。
- 回写前注入外部附件，原附件保持不变。
- 文档 revision 变化后旧 approval version 失效。

- [ ] **Step 2: 运行完整自动化测试**

Run: `cd feishu-generation-agent && uv run pytest -q && node --test tests/frontend/*.test.cjs`

Expected: 所有 Python 和 Node 测试 PASS；不得出现真实网络调用。

- [ ] **Step 3: 完成 README 与安全冒烟入口**

README 必须写明：

- 飞书应用机器人、`im.message.receive_v1`、群聊 @、卡片动作、wiki/docx/bitable/drive 权限清单。
- 把应用身份加入固定多维表格、知识库和需求文档的操作步骤。
- 表格六个字段的精确名称/类型以及系统只自动创建两个字段。
- 本地启动、扫描、领取、卡片批准、反馈、取消、回写失败重试和重启恢复。
- `.env` 必须 `chmod 600`，不复制到文档或聊天。
- Ark Bearer Key 与 AK/SK 的用途区别。
- 迁移常驻主机时需要配置的路径、进程守护和出站网络，不改变核心业务接口。

`smoke.py` 新增：

```python
parser.add_argument("--mode", choices=["legacy", "bitable"], default="legacy")
parser.add_argument("--record-id")
parser.add_argument("--allow-paid-generation", action="store_true")
```

`--mode bitable` 必须要求专用 record ID，先确认结果为空；无 `--allow-paid-generation` 时只走“扫描→领取→规划→审批载荷→取消”，并断言 provider submit 计数为 0。付费标志存在时仍打印预计任务/规格并要求交互输入精确确认短语。

- [ ] **Step 4: 只读和零生成真实联调**

Run:

```bash
cd feishu-generation-agent
chmod 600 .env
test -n "$BITABLE_SMOKE_RECORD_ID"
uv run agent-config-probe --network
uv run agent-smoke --mode bitable --record-id "$BITABLE_SMOKE_RECORD_ID"
```

Expected:

- tenant token、wiki→bitable、字段/记录读取、机器人长连接和全部模型鉴权通过。
- 测试记录走到审批后取消，`状态` 回到 `待处理`、`执行人` 清空、`结果` 为空。
- Chiyun/Seedance submit 计数为 0。

如果飞书权限或模型名失败，停在此步修正配置/网页权限；不得进入付费冒烟。

- [ ] **Step 5: 经用户当次明确批准后执行最小付费冒烟**

只有用户再次明确同意，才运行：

```bash
cd feishu-generation-agent
test -n "$BITABLE_SMOKE_RECORD_ID"
uv run agent-smoke --mode bitable --record-id "$BITABLE_SMOKE_RECORD_ID" --allow-paid-generation
```

Expected:

- 1 张最低合理规格图片和 1 条最短、低分辨率、无音频视频。
- 两个附件出现在同一记录 `结果`，状态为 `已完成`。
- 重启应用后 submit 数不增加。
- 人为制造一次回写失败后只补回写，不增加生成任务。

- [ ] **Step 6: 最终回归、敏感信息扫描与提交**

Run:

```bash
cd feishu-generation-agent
uv run pytest -q
node --test tests/frontend/*.test.cjs
git diff --check
feature_base=$(git merge-base main HEAD)
if git diff "$feature_base"..HEAD -- . ':!*.lock' | rg -n 'sk-[A-Za-z0-9]|ark-[A-Za-z0-9]{8}|SecretAccessKey|LARK_APP_SECRET=.+'; then
  echo "检测到疑似凭证，停止提交"
  exit 1
fi
```

Expected: 全部测试 PASS、`git diff --check` 无输出、敏感信息扫描无匹配。

```bash
git add feishu-generation-agent/README.md feishu-generation-agent/.env.example feishu-generation-agent/src/feishu_generation_agent/cli/smoke.py feishu-generation-agent/tests/integration/test_bitable_api.py feishu-generation-agent/tests/integration/test_bitable_restart.py
git commit -m "docs(agent): add bitable bot rollout workflow"
```

---

## Execution Notes

- 每个 Task 开始前重新检查 `git status --short`，只暂存该 Task 的文件。
- 每个实现 Task 必须使用 `superpowers:test-driven-development`：先确认目标测试因缺少行为而失败，再写最小实现。
- 遇到测试失败或接口行为与假设不符时，先使用 `superpowers:systematic-debugging` 查根因，不能通过放宽断言掩盖问题。
- Task 4、5、9、10 涉及飞书结构时，以锁定 SDK 和真实无付费探针为准；如果官方字段 payload 与 fixture 不同，先更新设计事实和测试 fixture，再继续实现。
- 完成 Task 13 后使用 `superpowers:verification-before-completion` 重新运行全量验证，再使用 `superpowers:requesting-code-review` 做合并前审查。
- 不自动重启现有 Portal/子应用；本 Agent 独立运行，真实生成任务执行期间也不得无确认重启。
