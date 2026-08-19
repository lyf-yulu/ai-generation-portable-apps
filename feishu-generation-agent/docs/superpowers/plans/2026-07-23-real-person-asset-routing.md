# 真人类官方资产路由 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让真人类生产表任务通过火山官方 Asset API 上传参考图片并生成视频，同时保持动画类现有生成链路不变。

**Architecture:** `ProductionBitableService` 按已配置的任务类型决定是否可领取；图工作流按运行绑定的 `snapshot.task_type` 选择供应商。新增 `VolcenginePortraitVideoGenerator` 以“运行级 Asset 分组 + 图片解析器”的方式复用 `SeedanceVideoGenerator` 的 Ark 提交、轮询与结果解析，不经由 `volcengine-portrait` HTTP 子应用。

**Tech Stack:** Python 3.12、httpx、Pydantic、SQLite、FastAPI、pytest、火山 Ark Asset OpenAPI SigV4。

## Global Constraints

- 动画类图生图和图生视频行为保持不变。
- 真人类图生图继续走 Chiyun；真人类图生视频才使用 Asset API。
- AK、SK、API Key 和签名材料不得写入前端、测试输出或日志。
- 每次真人类运行创建并保留独立 Asset 分组；资产状态和 ID 持久化以支持重跑。
- 不在自动化测试中发送真实付费生成请求。
- 不删除 `volcengine-portrait` 子应用或其配置文件。

---

### Task 1: 增加真人类运行配置并安全迁移本机凭据

**Files:**
- Modify: `src/feishu_generation_agent/config.py`
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_config_probe.py`
- Local only: `.env`

**Interfaces:**
- Produces: `Settings.volcengine_access_key`, `Settings.volcengine_secret_key` 与 `Settings.volcengine_project_name`；`capability_is_configured(settings, "portrait_generation")`。
- Consumes: 已存在的 `ARK_API_KEY`、`ARK_BASE_URL`、`SEEDANCE_MODEL`。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_config.py` 增加：

```python
def test_portrait_generation_requires_ak_sk_and_ark_key() -> None:
    settings = Settings(_env_file=None, ark_api_key="ark")
    assert not capability_is_configured(settings, "portrait_generation")

    configured = Settings(
        _env_file=None,
        ark_api_key="ark",
        volcengine_access_key="ak",
        volcengine_secret_key="sk",
    )
    assert capability_is_configured(configured, "portrait_generation")
    assert configured.volcengine_project_name == "Seedance2.0"
```

- [ ] **Step 2: 运行失败测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_config.py::test_portrait_generation_requires_ak_sk_and_ark_key -q`

预期：失败，因为 Settings 与 capability 尚未定义真人资产配置。

- [ ] **Step 3: 最小实现**

在 `Settings` 增加仅后端使用的 `SecretStr | None` 字段：

```python
volcengine_access_key: SecretStr | None = None
volcengine_secret_key: SecretStr | None = None
volcengine_project_name: str = "Seedance2.0"
```

在 `CAPABILITY_FIELDS` 增加：

```python
"portrait_generation": (
    "ark_api_key", "volcengine_access_key", "volcengine_secret_key",
),
```

本机迁移时以不回显的 Python 读取 `volcengine-portrait/config.json` 中的 `access_key`、`secret_key`，仅在 Agent `.env` 中缺失时追加 `VOLCENGINE_ACCESS_KEY=` 和 `VOLCENGINE_SECRET_KEY=`；不打印值、不提交 `.env`。

- [ ] **Step 4: 运行测试确认通过**

运行：`./.venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_config_probe.py -q`

预期：配置测试全部通过。

- [ ] **Step 5: 提交**

```bash
git add src/feishu_generation_agent/config.py src/feishu_generation_agent/bootstrap.py tests/unit/test_config.py tests/unit/test_config_probe.py
git commit -m "feat(agent): configure real-person asset generation"
```

### Task 2: 实现官方 Asset API 客户端与运行级资产记录

**Files:**
- Create: `src/feishu_generation_agent/integrations/volcengine_portrait.py`
- Create: `src/feishu_generation_agent/storage/portrait_assets.py`
- Test: `tests/unit/test_volcengine_portrait.py`

**Interfaces:**
- Produces: `VolcengineAssetClient.create_group(run_id) -> str`、`ensure_image_asset(run_id, asset) -> str`、`get_active_asset_url(asset_id) -> "asset://..."`。
- Consumes: `PublicMediaHost.upload()`、`MediaAsset`、`httpx.AsyncClient`、AK/SK、`PortraitAssetStore`。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_volcengine_portrait.py` 用 `httpx.MockTransport` 与假的公开素材主机写入：

```python
async def test_portrait_client_creates_group_activates_image_then_submits_asset_url(tmp_path):
    client = VolcengineAssetClient(
        http, access_key="ak-test", secret_key="sk-test",
        project_name="Seedance2.0", public_media_host=public_host,
        store=await PortraitAssetStore.open(tmp_path / "portrait.sqlite3"),
    )
    url = await client.ensure_image_asset("run-1", image_asset)
    assert url == "asset://asset-1"
    assert actions == ["CreateAssetGroup", "CreateAsset", "GetAsset"]
    assert "authorization" in requests[0].headers
    assert "ak-test" not in captured_log_output
```

- [ ] **Step 2: 运行失败测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_volcengine_portrait.py::test_portrait_client_creates_group_activates_image_then_submits_asset_url -q`

预期：失败，因为客户端与存储尚未实现。

- [ ] **Step 3: 实现最小客户端和存储**

`PortraitAssetStore` 使用 SQLite 表 `portrait_runs(run_id PRIMARY KEY, group_id, created_at)` 与 `portrait_assets(run_id, source_asset_id, volcengine_asset_id, status, PRIMARY KEY(run_id, source_asset_id))`。在创建分组、创建资产得到 ID、资产状态更新后各自提交事务。

`VolcengineAssetClient` 从现有子应用复制并收紧 SigV4 算法：固定 `https://ark.cn-beijing.volcengineapi.com/?Action=<action>&Version=2024-01-01`、region/service 为 `cn-beijing/ark`、`ProjectName` 为配置值。禁止任何凭据相关 `print`。图片先通过 `PublicMediaHost.upload()` 得到匿名 HTTPS URL，调用 `CreateAsset` 后轮询 `GetAsset`；仅 `Active` 返回 `asset://<id>`，`Failed` 与超时抛出 `AgentError`。

- [ ] **Step 4: 运行单元测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_volcengine_portrait.py -q`

预期：测试覆盖签名请求、组/资产复用、Active/Failed 状态和无密钥日志。

- [ ] **Step 5: 提交**

```bash
git add src/feishu_generation_agent/integrations/volcengine_portrait.py src/feishu_generation_agent/storage/portrait_assets.py tests/unit/test_volcengine_portrait.py
git commit -m "feat(agent): add Volcengine portrait asset client"
```

### Task 3: 以运行级图片解析器复用 Ark Seedance 生成逻辑

**Files:**
- Modify: `src/feishu_generation_agent/integrations/seedance.py`
- Modify: `src/feishu_generation_agent/integrations/volcengine_portrait.py`
- Test: `tests/unit/test_seedance.py`
- Test: `tests/unit/test_volcengine_portrait.py`

**Interfaces:**
- Produces: `SeedanceVideoGenerator(..., provider_name="seedance", image_url_resolver=None)`；`VolcenginePortraitVideoGenerator.for_run(run_id) -> VideoGenerator`。
- Consumes: Asset client的 `ensure_image_asset()`；Seedance 的视频参数验证与 `poll()`。

- [ ] **Step 1: 写失败测试**

```python
async def test_real_person_video_uses_asset_urls_and_keeps_audio_video_public(tmp_path):
    generator = portrait_generator.for_run("run-real-1")
    result = await generator.submit(video_task, mixed_assets)
    content = submitted_payload["content"]
    assert content[1]["image_url"]["url"] == "asset://asset-blue"
    assert content[2]["video_url"]["url"].startswith("https://public.example/")
    assert result.provider == "volcengine_portrait"
```

- [ ] **Step 2: 运行失败测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_volcengine_portrait.py -k asset_urls -q`

预期：失败，因为 Seedance 当前总是把图片编码为 data URL，且供应商名固定。

- [ ] **Step 3: 实现最小复用点**

为 `SeedanceVideoGenerator` 增加 `provider_name` 和异步 `image_url_resolver` 可选参数。默认路径不变：图片仍编码 data URL。提供解析器时，只替换图片 URL 为解析器返回的 `asset://`，保留参考图角色（含首尾帧）以及现有视频/音频公网托管。

`VolcenginePortraitVideoGenerator.for_run()` 创建 `SeedanceVideoGenerator(provider_name="volcengine_portrait", image_url_resolver=...)`，解析器调用本运行的 Asset 客户端。`poll()` 用实例 provider 名校验提交身份，复用原有结果 URL 校验和 15 分钟运行时轮询。

- [ ] **Step 4: 运行生成器测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_seedance.py tests/unit/test_volcengine_portrait.py -q`

预期：既有动画 Seedance payload 不变；真人 payload 使用 `asset://`。

- [ ] **Step 5: 提交**

```bash
git add src/feishu_generation_agent/integrations/seedance.py src/feishu_generation_agent/integrations/volcengine_portrait.py tests/unit/test_seedance.py tests/unit/test_volcengine_portrait.py
git commit -m "feat(agent): generate real-person videos from assets"
```

### Task 4: 按生产表任务类型选择生成器并开放真人类领取

**Files:**
- Modify: `src/feishu_generation_agent/bitable/production_service.py`
- Modify: `src/feishu_generation_agent/domain/production_bitable.py`
- Modify: `src/feishu_generation_agent/graph/nodes.py`
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Modify: `tests/unit/test_production_service.py`
- Modify: `tests/integration/test_production_bitable_api.py`
- Create: `tests/unit/test_real_person_routing.py`

**Interfaces:**
- Produces: `GraphServices.portrait_video_generator: VolcenginePortraitVideoGenerator | None`；运行级 `_generator_for_task(run_id, task, services) -> (provider, generator)`。
- Consumes: `ProductionTaskStore.get_by_run(run_id)` 的 `snapshot.task_type`。

- [ ] **Step 1: 写失败测试**

```python
async def test_real_person_video_uses_portrait_generator_for_production_run():
    provider, generator = await _generator_for_task(
        "run-real", image_to_video_task, services
    )
    assert provider == "volcengine_portrait"
    assert generator is services.portrait_video_generator.bound["run-real"]

async def test_real_person_image_task_uses_chiyun_for_production_run():
    provider, generator = await _generator_for_task(
        "run-real", image_to_image_task, services
    )
    assert provider == "chiyun"
    assert generator is services.image_generator
```

并增加生产服务测试：在 `enabled_task_types={"动画类", "真人类"}` 时真人类可 claim/approve/rerun；未配置 portrait 时任务卡显示禁用且 claim 返回 `真人类任务暂未启用`。

- [ ] **Step 2: 运行失败测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_production_service.py tests/unit/test_real_person_routing.py -q`

预期：失败，因为服务只允许动画类，图节点没有运行来源类型。

- [ ] **Step 3: 实现最小路由**

`ProductionBitableService` 接受 `enabled_task_types`，将允许逻辑从硬编码动画类改为集合判断；扫描结果同步为前端可用状态，未知类型仍禁用。

`GraphServices` 新增 `portrait_video_generator` 与 `production_task_store`。将 `_provider_for_task()` / `_generator_for_task()` 合并为异步选择器：图生图始终返回 `( "chiyun", image_generator )`；只有当前 `run_id` 对应生产绑定且类型为真人类的图生视频返回 `( "volcengine_portrait", portrait_video_generator.for_run(run_id) )`；其余图生视频返回 `( "seedance", video_generator )`。`_execute_one_task()` 使用返回的 provider 参与现有幂等操作记录。

Bootstrap 仅在 `portrait_generation` 配置完整时构造资产存储和真人适配器，并把允许类型传给生产服务；未配置时动画类服务仍可启动。

- [ ] **Step 4: 运行路由测试**

运行：`./.venv/bin/python -m pytest tests/unit/test_production_service.py tests/integration/test_production_bitable_api.py tests/unit/test_real_person_routing.py -q`

预期：真人类和动画类分别命中正确供应商，图生图始终命中 Chiyun。

- [ ] **Step 5: 提交**

```bash
git add src/feishu_generation_agent/bitable/production_service.py src/feishu_generation_agent/domain/production_bitable.py src/feishu_generation_agent/graph/nodes.py src/feishu_generation_agent/bootstrap.py tests/unit/test_production_service.py tests/integration/test_production_bitable_api.py tests/unit/test_real_person_routing.py
git commit -m "feat(agent): route real-person production tasks"
```

### Task 5: 本机配置迁移、完整回归与无付费上线验证

**Files:**
- Local only: `feishu-generation-agent/.env`

- [ ] **Step 1: 安全迁移本机 AK/SK**

运行一个不打印凭据的本地迁移：仅在 `.env` 未设置时复制已有 `volcengine-portrait/config.json` 的 `access_key` / `secret_key`，并验证三个环境变量均为非空。

- [ ] **Step 2: 完整回归**

运行：`./.venv/bin/python -m pytest -q && node --test tests/frontend/*.test.cjs`

预期：全部 Python 与前端测试通过。

- [ ] **Step 3: 重启与只读验证**

运行：`launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent`，再请求 `/api/health` 与 `/api/bitable/tasks`。

预期：服务 ready；真人类任务显示可处理；不领取任务、不创建资产、不触发付费生成。

- [ ] **Step 4: 检查本地配置未进入版本控制**

运行：`git status --short`

预期：`.env` 不出现；只保留用户原有的无关改动。前四个任务已经分别提交所有代码与测试。
