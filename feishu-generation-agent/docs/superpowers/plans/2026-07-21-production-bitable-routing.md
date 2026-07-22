# Production Bitable Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the production Feishu Bitable as a non-mutating task source and deliver approved artifacts to automatically created, per-maker result Bitables.

**Architecture:** Keep the existing MVP Bitable adapter intact for compatibility. Add a production-specific adapter, store, service, and delivery writer, then select that path when the production configuration is present. The production service owns source-record snapshots and approval gating; the production delivery writer owns result-table provisioning, maker permissions, attachment upload, and idempotent result-row updates.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, aiosqlite, httpx, LangGraph, Feishu Bitable/Drive APIs, browser JavaScript with Node built-in test runner, pytest.

## Global Constraints

- Production source Bitable is strictly read-only: do not create, update, or delete its fields, records, views, or permissions.
- Read only records whose `需求附件` is exactly one valid HTTPS Feishu `docx` or `wiki` link.
- Test mode includes `未开始`、`制作中`、`待修改`、`已确认完成`; normal mode excludes `已确认完成`.
- A task without `需求制作人` may be read and planned but must be rejected at approval before any provider submission or result-table mutation.
- Result tables live only in `LARK_RESULT_FOLDER_TOKEN`; never fall back to another folder, an app root, or the production table.
- Every result table has exactly six columns in this order: `需求名称`, `需求附件`, `项目名称`, `发起人`, `需求制作人`, `结果`.
- Each result-table row is a snapshot of the five source values plus generated artifact attachments. No migration of historical production rows.
- Store secrets only in `.env`; never add production URLs, app tokens, folder tokens, keys, or customer content to committed files or test fixtures.
- Preserve existing legacy Bitable and document-delivery behavior when production configuration is absent.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/feishu_generation_agent/config.py` | Production Bitable settings and defaults. |
| `.env.example` | Non-secret placeholders for production configuration. |
| `src/feishu_generation_agent/domain/production_bitable.py` | Typed production schema, task summary, binding, source snapshot, and result-table target models. |
| `src/feishu_generation_agent/integrations/production_bitable.py` | Read-only production source schema/record parser. |
| `src/feishu_generation_agent/integrations/feishu_client.py` | Typed Feishu requests for app/table/field/record creation and file collaborator permission. |
| `src/feishu_generation_agent/storage/production_tasks.py` | SQLite persistence for claims, result-table mapping, and source-record-to-result-row mapping. |
| `src/feishu_generation_agent/bitable/production_service.py` | Scan, claim, approval gate, active-run recovery, and status synchronization. |
| `src/feishu_generation_agent/integrations/production_delivery.py` | Result table provisioning and artifact delivery. |
| `src/feishu_generation_agent/integrations/production_routing.py` | Select production delivery for production-bound runs, otherwise legacy delivery. |
| `src/feishu_generation_agent/bootstrap.py` | Construct the production service/writer when production settings are configured. |
| `src/feishu_generation_agent/web/app.py` | Expose production service through existing Bitable endpoints and enforce approval gate. |
| `src/feishu_generation_agent/web/static/{index.html,app.js,bitable-state.js}` | Display production metadata, non-deliverable state, and delivered result-table link. |
| `src/feishu_generation_agent/cli/config_probe.py` | Read-only production schema/document/folder permission preflight. |
| `tests/unit/test_production_bitable.py` | Source parsing, status selection, and safe link validation. |
| `tests/unit/test_production_tasks.py` | Claim/mapping persistence and restart-safe idempotency. |
| `tests/unit/test_production_delivery.py` | Provisioning, permission request, attachments, and row update behavior. |
| `tests/unit/test_config.py` | Production configuration behavior. |
| `tests/integration/test_production_bitable_api.py` | API scan/claim/approval/return-to-result-table flow. |
| `tests/frontend/bitable_state.test.cjs` | Production task UI state behavior. |

## Task 1: Define production configuration and typed boundary

**Files:**
- Modify: `src/feishu_generation_agent/config.py`
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Modify: `.env.example`
- Create: `src/feishu_generation_agent/domain/production_bitable.py`
- Modify: `src/feishu_generation_agent/domain/__init__.py`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_production_bitable.py`

**Consumes:** Existing `Settings`, `BitableLocation`, `RequirementRequest`, and Pydantic models.

**Produces:** `Settings.production_bitable_configured`, `ProductionLocation`, `ProductionTaskSummary`, `ProductionBinding`, `ProductionSourceSnapshot`, and `ResultTableTarget`.

- [ ] **Step 1: Write failing configuration and domain tests**

```python
def test_production_settings_require_source_and_result_folder() -> None:
    settings = Settings(
        lark_production_bitable_url="https://tenant.feishu.cn/wiki/wikiProd",
        lark_production_table_id="tblProd",
        lark_production_view_id="vewProd",
        lark_result_folder_token="fldResults",
    )

    assert settings.production_bitable_configured is True
    assert settings.lark_include_completed_for_test is False


def test_production_task_without_maker_is_readable_but_not_deliverable() -> None:
    task = ProductionTaskSummary(
        record_id="rec1",
        display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/doc1",
        progress="未开始",
        maker_open_id=None,
        maker_name=None,
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A", requirement_attachment="https://tenant.feishu.cn/docx/doc1",
            project_names=["项目"], requester_open_ids=["ou_requester"],
            requester_names=["发起人"], maker_open_ids=[], maker_names=[],
        ),
    )

    assert task.deliverable is False
    assert task.delivery_block_reason == "缺少需求制作人"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_config.py tests/unit/test_production_bitable.py`

Expected: FAIL because the production settings and domain models do not exist.

- [ ] **Step 3: Add settings, capability predicates, and typed models**

```python
# config.py
lark_production_bitable_url: str | None = None
lark_production_table_id: str | None = None
lark_production_view_id: str | None = None
lark_result_folder_token: str | None = None
lark_include_completed_for_test: bool = False

@property
def production_bitable_configured(self) -> bool:
    return all((
        self.lark_production_bitable_url,
        self.lark_production_table_id,
        self.lark_production_view_id,
        self.lark_result_folder_token,
    ))
```

```python
# domain/production_bitable.py
class ProductionSourceSnapshot(BaseModel):
    requirement_name: str
    requirement_attachment: str
    project_names: list[str]
    requester_open_ids: list[str]
    requester_names: list[str]
    maker_open_ids: list[str]
    maker_names: list[str]

class ProductionTaskSummary(BaseModel):
    record_id: str
    display_text: str
    source_url: str
    progress: str
    maker_open_id: str | None
    maker_name: str | None
    snapshot: ProductionSourceSnapshot

    @property
    def deliverable(self) -> bool:
        return self.maker_open_id is not None

    @property
    def delivery_block_reason(self) -> str | None:
        return None if self.deliverable else "缺少需求制作人"
```

Add explicit `production_bitable` capability entries in `bootstrap.py`; make `runtime_is_configured()` accept production mode as an alternative to old Bitable/legacy delivery, while keeping all old fields and behavior unchanged. Add only non-secret example keys to `.env.example`.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_config.py tests/unit/test_production_bitable.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/config.py src/feishu_generation_agent/bootstrap.py \
  src/feishu_generation_agent/domain/production_bitable.py \
  src/feishu_generation_agent/domain/__init__.py .env.example \
  tests/unit/test_config.py tests/unit/test_production_bitable.py
git commit -m "feat(agent): define production bitable configuration"
```

## Task 2: Implement read-only production source adapter

**Files:**
- Create: `src/feishu_generation_agent/integrations/production_bitable.py`
- Modify: `src/feishu_generation_agent/integrations/bitable_url.py`
- Test: `tests/unit/test_production_bitable.py`
- Test fixture: `tests/fixtures/production_bitable_fields.json`
- Test fixture: `tests/fixtures/production_bitable_records.json`

**Consumes:** `FeishuClient.request_json/iter_items`, `parse_bitable_url`, and Task 1 models.

**Produces:** `ProductionBitableClient.resolve_location()`, `ensure_schema()`, and `list_tasks()`; all calls are GET-only.

- [ ] **Step 1: Write failing adapter tests for production names and status filtering**

```python
async def test_lists_readable_production_tasks_and_keeps_completed_only_in_test_mode():
    client = ProductionBitableClient(fake_feishu)
    location = await client.resolve_location(production_location)
    schema = await client.ensure_schema(location)

    normal = await client.list_tasks(location, schema, include_completed=False)
    test_mode = await client.list_tasks(location, schema, include_completed=True)

    assert [task.record_id for task in normal] == ["rec-new", "rec-making", "rec-revise"]
    assert [task.record_id for task in test_mode] == [
        "rec-new", "rec-making", "rec-revise", "rec-done"
    ]
    assert all(task.source_url.startswith("https://tenant.feishu.cn/") for task in test_mode)


async def test_rejects_non_feishu_or_non_single_requirement_attachment():
    tasks = await client.list_tasks(location, schema, include_completed=True)
    assert {task.record_id for task in tasks}.isdisjoint({"rec-empty", "rec-note", "rec-two-links"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_production_bitable.py -k production`

Expected: FAIL because `ProductionBitableClient` is not defined.

- [ ] **Step 3: Implement strict schema and record parsing**

```python
_PRODUCTION_FIELDS = {
    "需求名称": frozenset({1}),
    "需求附件": frozenset({1}),
    "项目名称": frozenset({4}),
    "发起人": frozenset({11}),
    "需求制作人": frozenset({11}),
    "当前进度": frozenset({3}),
}
_ACTIVE_PROGRESS = frozenset({"未开始", "制作中", "待修改"})
_TEST_PROGRESS = _ACTIVE_PROGRESS | {"已确认完成"}

async def list_tasks(self, location, schema, *, include_completed: bool):
    records = await self._client.iter_items(self._records_path(location), params={"view_id": location.view_id})
    allowed = _TEST_PROGRESS if include_completed else _ACTIVE_PROGRESS
    return [task for record in records if (task := self._to_task(record)) is not None and task.progress in allowed]
```

`_to_task()` must use `parse_requirement_source()` for the attachment, extract `open_id`/display names from person arrays, and retain all first-five-column values in `ProductionSourceSnapshot`. It must not infer a maker from `发起人`, change the source record, or accept plain text as a source URL.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_production_bitable.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/production_bitable.py \
  src/feishu_generation_agent/integrations/bitable_url.py \
  tests/unit/test_production_bitable.py tests/fixtures/production_bitable_fields.json \
  tests/fixtures/production_bitable_records.json
git commit -m "feat(agent): read production bitable tasks"
```

## Task 3: Persist production claims, result targets, and delivery rows

**Files:**
- Create: `src/feishu_generation_agent/storage/production_tasks.py`
- Test: `tests/unit/test_production_tasks.py`

**Consumes:** Task 1 `ProductionTaskSummary`, `ProductionBinding`, and `ResultTableTarget`.

**Produces:** `ProductionTaskStore` with `claim()`, `get_by_run()`, `list_active()`, `upsert_result_target()`, `get_result_target()`, `reserve_delivery()`, and `complete_delivery()`.

- [ ] **Step 1: Write failing persistence tests**

```python
async def test_claim_is_unique_per_source_record_and_persists_snapshot(tmp_path):
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    binding = await store.claim(location, task, run_id="run-1", thread_id="thread-1")

    assert binding.snapshot.requirement_name == "需求 A"
    with pytest.raises(ProductionTaskAlreadyClaimed):
        await store.claim(location, task, run_id="run-2", thread_id="thread-2")


async def test_result_target_and_delivery_row_survive_reopen(tmp_path):
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    await store.upsert_result_target(target)
    await store.complete_delivery("run-1", result_record_id="rec-result")
    await store.close()

    reopened = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    assert (await reopened.get_result_target("ou-maker")).table_id == "tbl-result"
    assert (await reopened.get_delivery("run-1")).result_record_id == "rec-result"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_production_tasks.py`

Expected: FAIL because `ProductionTaskStore` does not exist.

- [ ] **Step 3: Implement the isolated SQLite store**

Create `production_tasks`, `maker_result_tables`, and `production_deliveries` tables. Make `(source_app_token, source_table_id, source_record_id)` unique in `production_tasks`; make `maker_open_id` unique in `maker_result_tables`; make `run_id` unique in `production_deliveries`.

```python
async def reserve_delivery(self, run_id: str) -> ProductionDelivery:
    """Create or return the local delivery row before remote result-row work."""

async def complete_delivery(self, run_id: str, *, result_record_id: str) -> ProductionDelivery:
    """Atomically persist the only destination row for a successful run."""
```

Validate identifiers and source snapshots with the same byte limits and redaction rules used by `BitableTaskStore`; store snapshot JSON with `model_dump(mode="json")` and reconstruct with `model_validate()`.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_production_tasks.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/storage/production_tasks.py \
  tests/unit/test_production_tasks.py
git commit -m "feat(agent): persist production task routing"
```

## Task 4: Add Feishu result-table and permission primitives

**Files:**
- Modify: `src/feishu_generation_agent/integrations/feishu_client.py`
- Test: `tests/unit/test_feishu_bitable.py`

**Consumes:** Existing authenticated `request_json()` error handling and Feishu tenant token.

**Produces:** typed `create_bitable_app()`, `list_bitable_fields()`, `update_bitable_field()`, `create_bitable_field()`, `create_bitable_record()`, `update_bitable_record()`, and `grant_file_editor()` methods.

- [ ] **Step 1: Write failing HTTP request-shape tests**

```python
async def test_creates_result_bitable_in_explicit_folder():
    created = await client.create_bitable_app("AI生成结果－小王", "fldResults")
    assert created.app_token == "app-result"
    assert requests[-1] == (
        "POST", "/open-apis/bitable/v1/apps",
        {"name": "AI生成结果－小王", "folder_token": "fldResults", "time_zone": "Asia/Shanghai"},
    )


async def test_grants_openid_editor_on_created_bitable():
    await client.grant_file_editor("app-result", "ou-maker")
    assert requests[-1] == (
        "POST", "/open-apis/drive/v1/permissions/app-result/members",
        {"type": "bitable", "need_notification": "false"},
        {"member_type": "openid", "member_id": "ou-maker", "perm": "edit", "type": "user"},
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_feishu_bitable.py -k "result_bitable or editor"`

Expected: FAIL because the client methods do not exist.

- [ ] **Step 3: Implement only the required Feishu API wrappers**

```python
async def create_bitable_app(self, name: str, folder_token: str) -> CreatedBitableApp:
    payload = await self.request_json(
        "POST", "/open-apis/bitable/v1/apps",
        json_body={"name": name, "folder_token": folder_token, "time_zone": "Asia/Shanghai"},
    )
    return _created_bitable_app(payload)

async def grant_file_editor(self, app_token: str, open_id: str) -> None:
    await self.request_json(
        "POST", f"/open-apis/drive/v1/permissions/{app_token}/members",
        params={"type": "bitable", "need_notification": "false"},
        json_body={"member_type": "openid", "member_id": open_id, "perm": "edit", "type": "user"},
    )
```

For a new app, list the default table and fields, rename its text primary field to `需求名称`, then add exactly five fields in order: text `需求附件`, multi-select `项目名称`, person `发起人`, person `需求制作人`, attachment `结果`. Do not delete the sole default table. Expose `create_bitable_record()` and `update_bitable_record()` using the resulting table id.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_feishu_bitable.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/feishu_client.py \
  tests/unit/test_feishu_bitable.py
git commit -m "feat(agent): add result bitable api primitives"
```

## Task 5: Implement production delivery and approval gate

**Files:**
- Create: `src/feishu_generation_agent/bitable/production_service.py`
- Create: `src/feishu_generation_agent/integrations/production_delivery.py`
- Create: `src/feishu_generation_agent/integrations/production_routing.py`
- Modify: `src/feishu_generation_agent/domain/artifact.py`
- Modify: `src/feishu_generation_agent/graph/runtime.py`
- Test: `tests/unit/test_production_delivery.py`
- Test: `tests/unit/test_production_bitable.py`
- Test: `tests/graph/test_approval_graph.py`

**Consumes:** Tasks 1–4, current `GraphRuntime`, `Artifact`, `TaskPlan`, and `DeliveryRecord`.

**Produces:** `ProductionBitableService`, `ProductionResultWriter`, and `ProductionRoutingDeliveryWriter`.

- [ ] **Step 1: Write failing service and delivery tests**

```python
async def test_production_service_allows_claim_but_blocks_approval_when_maker_missing():
    run_id = await service.claim("rec-no-maker")
    with pytest.raises(RunValidationError, match="缺少需求制作人"):
        await service.validate_approval(run_id)


async def test_delivery_creates_one_result_table_and_updates_one_result_row_on_retry():
    first = await writer.deliver("run-1", document, plan, [artifact])
    second = await writer.retry_delivery("run-1")

    assert first.table_id == second.table_id == "tbl-result"
    assert len(fake_feishu.created_apps) == 1
    assert len(fake_feishu.created_records) == 1
    assert fake_feishu.updated_records[-1]["fields"]["结果"]


async def test_delivery_never_runs_when_result_folder_provisioning_fails():
    fake_feishu.create_app_error = permission_error
    with pytest.raises(AgentError, match="结果文件夹"):
        await writer.deliver("run-1", document, plan, [artifact])
    assert fake_feishu.created_records == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_production_bitable.py tests/unit/test_production_delivery.py tests/graph/test_approval_graph.py -k "production or approval"`

Expected: FAIL because the production service and writer do not exist.

- [ ] **Step 3: Implement the service and writer**

```python
class ProductionBitableService:
    async def scan(self) -> list[ProductionTaskSummary]:
        location, schema = await self._prepared()
        return await self._client.list_tasks(
            location, schema, include_completed=self._include_completed_for_test
        )

    async def claim(self, record_id: str) -> str:
        task = await self._require_scanned_task(record_id)
        binding = await self._store.claim(
            self._location, task, run_id=str(uuid4()), thread_id=str(uuid4())
        )
        return await self._runtime.start_run(
            RequirementRequest(source_url=binding.source_url, trigger_type="production_bitable"),
            run_id=binding.run_id, thread_id=binding.thread_id,
        )
    async def validate_approval(self, run_id: str) -> None:
        binding = await self._store.get_by_run(run_id)
        if binding is not None and binding.maker_open_id is None:
            raise RunValidationError("缺少需求制作人；请先在生产表补齐后再批准")
```

```python
class ProductionResultWriter:
    async def deliver(self, run_id, document, plan, artifacts) -> DeliveryRecord:
        binding = await self._bindings.require_by_run(run_id)
        target = await self._provisioner.ensure_target(binding.maker_open_id, binding.maker_name)
        uploaded = [await self._ensure_uploaded(run_id, target.app_token, artifact) for artifact in artifacts]
        delivery = await self._store.reserve_delivery(run_id)
        fields = self._result_fields(binding.snapshot, uploaded)
        record_id = await self._write_one_result_row(target, delivery, fields)
        await self._store.complete_delivery(run_id, result_record_id=record_id)
        return DeliveryRecord(status="succeeded", target_type="production_result_record", app_token=target.app_token, table_id=target.table_id, record_id=record_id, result_table_url=target.url, uploaded_artifact_ids=[a.artifact_id for a in uploaded])
```

`_result_fields()` must emit the exact six keys, preserve source person and multi-select shapes, and put only `[{"file_token": token}]` values in `结果`. Reuse the existing verified file upload logic and `bitable_file` media parent behavior. Provisioning must create the app only under the configured folder, configure the six columns, grant the maker `edit`, then persist the target. Any failure stops delivery without changing the source record.

Extend `DeliveryRecord` without changing existing output semantics:

```python
target_type: Literal["docx", "bitable_record", "production_result_record"] = "docx"
result_table_url: str | None = None

@model_validator(mode="after")
def validate_delivery_target(self) -> "DeliveryRecord":
    if self.target_type == "docx":
        if not self.document_id or not self.document_url:
            raise ValueError("docx delivery requires document identity")
    elif not self.app_token or not self.table_id or not self.record_id:
        raise ValueError("bitable delivery requires app/table/record identity")
    elif self.target_type == "production_result_record" and not self.result_table_url:
        raise ValueError("production result delivery requires result table URL")
    return self
```

`ProductionRoutingDeliveryWriter` must choose this writer only for a run found in `ProductionTaskStore`; all other runs continue through `FeishuDeliveryWriter` or the existing `RoutingDeliveryWriter`.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_production_bitable.py tests/unit/test_production_delivery.py tests/graph/test_approval_graph.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/bitable/production_service.py \
  src/feishu_generation_agent/integrations/production_delivery.py \
  src/feishu_generation_agent/integrations/production_routing.py \
  src/feishu_generation_agent/domain/artifact.py src/feishu_generation_agent/graph/runtime.py \
  tests/unit/test_production_bitable.py tests/unit/test_production_delivery.py \
  tests/graph/test_approval_graph.py
git commit -m "feat(agent): route approved production tasks to maker tables"
```

## Task 6: Wire production mode into startup, API, and read-only preflight

**Files:**
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Modify: `src/feishu_generation_agent/web/app.py`
- Modify: `src/feishu_generation_agent/cli/config_probe.py`
- Modify: `src/feishu_generation_agent/cli/smoke.py`
- Test: `tests/integration/test_production_bitable_api.py`
- Test: `tests/unit/test_config_probe.py`

**Consumes:** Tasks 1–5.

**Produces:** Production mode startup, `/api/bitable/*` compatibility endpoints, explicit approval rejection, and a safe preflight command.

- [ ] **Step 1: Write failing integration and probe tests**

```python
async def test_scan_claim_and_missing_maker_approval_gate(client):
    scanned = await client.get("/api/bitable/tasks")
    assert scanned.json()[0]["progress"] == "未开始"
    assert scanned.json()[0]["deliverable"] is False

    claimed = await client.post("/api/bitable/tasks/rec-no-maker/claim")
    rejected = await client.post(
        f"/api/runs/{claimed.json()['run_id']}/decision",
        json={"action": "approve", "tasks": []},
    )
    assert rejected.status_code == 422
    assert "缺少需求制作人" in rejected.json()["detail"]


async def test_production_probe_uses_only_get_requests():
    report = await probe(production_settings)
    assert report["capabilities"]["production_bitable_read"]["permission_ok"] is True
    assert all(method == "GET" for method, _path in fake_feishu.requests)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/integration/test_production_bitable_api.py tests/unit/test_config_probe.py -k production`

Expected: FAIL because production mode is not wired into bootstrap or the API.

- [ ] **Step 3: Wire the mode without breaking old MVP mode**

```python
# bootstrap.py
if settings.production_bitable_configured:
    production_store = await ProductionTaskStore.open(settings.data_dir / "production-bitable.sqlite3")
    production_location = parse_bitable_url(
        settings.lark_production_bitable_url or "",
        settings.lark_production_table_id or "",
        settings.lark_production_view_id or "",
    )
    production_factory = ProductionServiceFactory(
        client=ProductionBitableClient(feishu), store=production_store,
        location=production_location,
        include_completed_for_test=settings.lark_include_completed_for_test,
    )
    delivery_writer = ProductionRoutingDeliveryWriter(production_store, production_writer, legacy=legacy_writer)
elif bitable_configured:
    # preserve current BitableServiceFactory and RoutingDeliveryWriter path
```

In `web/app.py`, use a small `TableService` protocol instead of a concrete `BitableMvpService` annotation. Keep existing endpoint URLs. Before `runtime.resume_run()` for an approve decision, call `await active_bitable.validate_approval(run_id)` when a table service exists. Translate this validation to HTTP 422 with the Chinese actionable message. Update health and config probe capability names to report production read, result-folder configuration, and result-table write prerequisites separately. The read-only probe must use only GET requests: it can prove source reads and folder visibility, but it must label create-table and add-collaborator scopes as “configured but not write-verified”, never claim that a GET proved those mutation permissions.

Add `--production-bitable-read-only` to `agent-smoke`; it must resolve schema, scan, and ingest at most one document without claiming, generating, writing, or creating tables.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/integration/test_production_bitable_api.py tests/unit/test_config_probe.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/bootstrap.py src/feishu_generation_agent/web/app.py \
  src/feishu_generation_agent/cli/config_probe.py src/feishu_generation_agent/cli/smoke.py \
  tests/integration/test_production_bitable_api.py tests/unit/test_config_probe.py
git commit -m "feat(agent): expose production bitable workflow"
```

## Task 7: Update the local task-selection UI

**Files:**
- Modify: `src/feishu_generation_agent/web/static/index.html`
- Modify: `src/feishu_generation_agent/web/static/app.js`
- Modify: `src/feishu_generation_agent/web/static/bitable-state.js`
- Modify: `src/feishu_generation_agent/web/static/styles.css`
- Test: `tests/frontend/bitable_state.test.cjs`

**Consumes:** Production task JSON from Task 6: `progress`, `maker_name`, `deliverable`, `delivery_block_reason`, and later run delivery target metadata.

**Produces:** Clear production-task cards and result-table link feedback without browser-side mutation of Feishu data.

- [ ] **Step 1: Write failing frontend state tests**

```javascript
test("production task keeps delivery block state through a scan", () => {
  let state = api.createState();
  state = api.scanSucceeded(state, [{
    record_id: "rec-no-maker", display_text: "需求 A", progress: "制作中",
    maker_name: null, deliverable: false, delivery_block_reason: "缺少需求制作人",
  }]);

  assert.equal(state.tasks[0].deliverable, false);
  assert.equal(state.tasks[0].delivery_block_reason, "缺少需求制作人");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/frontend/bitable_state.test.cjs`

Expected: FAIL because the task fixture/state contract is not rendered by the UI.

- [ ] **Step 3: Render production metadata and delivery destination**

Update the task card construction to render:

```javascript
element("p", "bitable-task-meta", `进度：${task.progress || "—"}`),
element("p", "bitable-task-meta", `制作人：${task.maker_name || "未填写"}`),
task.deliverable
  ? null
  : element("p", "bitable-task-warning", task.delivery_block_reason),
```

Keep “开始分析” available for a task without a maker, because planning is allowed. In the approval panel, surface the 422 response from the server unchanged; do not guess or disable based solely on stale browser state. When `view.delivery.target_type === "production_result_record"`, render a link named “打开结果表” using the server-provided, already validated result-table URL.

Update introductory copy from “结果为空” to “附件可读且进度符合当前扫描规则”; do not expose app tokens, table ids, open ids, or raw snapshots in the DOM.

- [ ] **Step 4: Run frontend tests to verify they pass**

Run: `node --test tests/frontend/*.test.cjs`

Expected: all frontend tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/web/static/index.html \
  src/feishu_generation_agent/web/static/app.js \
  src/feishu_generation_agent/web/static/bitable-state.js \
  src/feishu_generation_agent/web/static/styles.css \
  tests/frontend/bitable_state.test.cjs
git commit -m "feat(agent): show production task routing state"
```

## Task 8: Verify, document activation, and perform safe live preflight

**Files:**
- Create: `docs/production-bitable-activation.md`
- Modify: `.env.example`
- Test: all tests above

**Consumes:** Complete Tasks 1–7 and user-provided local `.env` values.

**Produces:** A repeatable activation checklist and evidence that production reads succeed without source-table writes.

- [ ] **Step 1: Write the activation checklist and its assertion test**

Document these exact checks in `docs/production-bitable-activation.md`:

```text
1. Add the app to the production Bitable as a document application with read access.
2. Ensure the app can read each requirement document and download its embedded media.
3. Configure LARK_PRODUCTION_BITABLE_URL, LARK_PRODUCTION_TABLE_ID,
   LARK_PRODUCTION_VIEW_ID, and LARK_RESULT_FOLDER_TOKEN only in .env.
4. Run agent-config-probe and agent-smoke --production-bitable-read-only.
5. Confirm the scan count and sampled titles in the local UI.
6. Before first real delivery, explicitly request a one-table provisioning run;
   do not create result tables merely by scanning or planning.
```

- [ ] **Step 2: Run the complete automated suite**

Run: `.venv/bin/pytest -q && node --test tests/frontend/*.test.cjs`

Expected: all Python and frontend tests PASS.

- [ ] **Step 3: Run read-only live verification**

Run: `.venv/bin/agent-config-probe --network && .venv/bin/agent-smoke --production-bitable-read-only`

Expected: production schema and document-read checks pass; command output must show no POST, PUT, PATCH, or DELETE operation.

- [ ] **Step 4: Verify no unintended source-table mutation occurred**

Run: compare the sampled source record ids and `当前进度` values from before and after the read-only command using GET requests only.

Expected: same source records and values; no new result table or result record exists.

- [ ] **Step 5: Commit**

```bash
git add docs/production-bitable-activation.md .env.example
git commit -m "docs(agent): document production bitable activation"
```
