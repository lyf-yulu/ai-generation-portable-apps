# 生产表动画类路由与统一结果表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扫描所有非“已确认完成”的生产表记录，只允许动画类任务执行，并将所有产物交付到单一结果表。

**Architecture:** 生产表客户端把需求类型保存在任务快照中并按唯一完成状态过滤；生产服务负责执行资格和锁。交付器用固定的全局目标键获取或创建一个结果表，复制来源前六列并写入结果附件。

**Tech Stack:** Python 3.12、Pydantic、FastAPI、pytest、飞书多维表格 API。

---

### Task 1: 修正生产表扫描与任务类型路由

**Files:**
- Modify: `src/feishu_generation_agent/integrations/production_bitable.py`
- Modify: `src/feishu_generation_agent/domain/production_bitable.py`
- Modify: `src/feishu_generation_agent/bitable/production_service.py`
- Modify: `tests/unit/test_production_bitable.py`

- [ ] **Step 1: 写失败测试**

```python
async def test_list_tasks_excludes_only_confirmed_and_keeps_task_type() -> None:
    tasks = await client.list_tasks(location, schema, include_completed=False)
    assert [task.record_id for task in tasks] == ["rec-empty", "rec-working"]
    assert tasks[0].task_type == "动画类"

async def test_claim_rejects_non_animation_task() -> None:
    with pytest.raises(RunConflict, match="真人类"):
        await service.claim("rec-live-action")
```

- [ ] **Step 2: 运行失败测试** — `pytest tests/unit/test_production_bitable.py -k 'excludes_only_confirmed or non_animation' -v`；预期字段不存在或旧状态过滤导致失败。

- [ ] **Step 3: 最小实现** — 在必需字段增加 `需求类型`（单选类型 3）；`ProductionSourceSnapshot`、`ProductionTaskSummary`、`ProductionBinding` 增加 `task_type`。`list_tasks()` 忽略 `include_completed` 参数，过滤条件固定为 `progress != "已确认完成"`。任务摘要提供 `runnable` 和 `execution_block_reason`：仅 `动画类` 可运行。`ProductionBitableService.claim()` 对非动画类抛出 `RunConflict("真人类任务暂未启用")`，同时保留原子锁领取。

- [ ] **Step 4: 验证并提交** — `pytest tests/unit/test_production_bitable.py -v` 预期 PASS；执行 `git add src/feishu_generation_agent/{integrations/production_bitable.py,domain/production_bitable.py,bitable/production_service.py} tests/unit/test_production_bitable.py && git commit -m "feat(agent): route production tasks by type"`。

### Task 2: 改为单一结果表并允许空制作人

**Files:**
- Modify: `src/feishu_generation_agent/integrations/production_delivery.py`
- Modify: `src/feishu_generation_agent/bitable/production_service.py`
- Modify: `tests/unit/test_production_delivery.py`

- [ ] **Step 1: 写失败测试**

```python
async def test_delivery_reuses_single_result_table_without_maker(tmp_path) -> None:
    first = await writer.deliver("run-with-maker", document, plan, [artifact])
    second = await writer.deliver("run-without-maker", document, plan, [artifact])
    assert first.app_token == second.app_token == "app-result"
    assert client.created_apps == 1
    assert list(client.last_fields) == ["需求名称", "需求类型", "需求附件", "项目名称", "发起人", "需求制作人", "结果"]
```

- [ ] **Step 2: 运行失败测试** — `pytest tests/unit/test_production_delivery.py -k 'single_result_table_without_maker' -v`；预期旧代码要求制作人并分别创建结果表。

- [ ] **Step 3: 最小实现** — 用常量 `__shared_production_result__` 作为结果目标存储键；`_ensure_target()` 不接受制作人参数，首次创建名为 `AI生成结果` 的表。字段顺序为需求名称、需求类型、需求附件、项目名称、发起人、需求制作人、结果。删除 `validate_approval()` 的制作人阻断，`_result_fields()` 在没有人时写空 people 列。旧结果目标记录只读取，不删除。

- [ ] **Step 4: 验证并提交** — `pytest tests/unit/test_production_delivery.py -v` 预期 PASS；执行 `git add src/feishu_generation_agent/{integrations/production_delivery.py,bitable/production_service.py} tests/unit/test_production_delivery.py && git commit -m "feat(agent): deliver production results to one table"`。

### Task 3: 关闭测试配置并完成接口回归

**Files:**
- Modify: `feishu-generation-agent/.env`
- Modify: `tests/integration/test_production_bitable_api.py`
- Modify: `src/feishu_generation_agent/web/app.py`（仅在任务响应未包含路由字段时）

- [ ] **Step 1: 写失败 API 测试**

```python
async def test_scan_exposes_task_type_and_blocks_real_person_claim(client) -> None:
    tasks = await client.get("/api/bitable/tasks")
    assert tasks.json()[0]["task_type"] == "动画类"
    response = await client.post("/api/bitable/tasks/rec-live-action/claim")
    assert response.status_code == 409
    assert "暂未启用" in response.json()["detail"]
```

- [ ] **Step 2: 运行失败测试** — `pytest tests/integration/test_production_bitable_api.py -k 'task_type or real_person' -v`；预期响应缺少类型或领取未阻止。

- [ ] **Step 3: 最小实现** — 保证任务 JSON 透传 `task_type`、`runnable`、`execution_block_reason`，前端可沿用不可领取状态。把 `.env` 中 `LARK_INCLUDE_COMPLETED_FOR_TEST` 改为 `false`；生产客户端不依赖该配置决定筛选。

- [ ] **Step 4: 完整验证并提交** — 执行 `pytest -q && node --test tests/frontend/*.test.cjs`，预期全部 PASS；执行 `git add feishu-generation-agent && git commit -m "test(agent): verify production animation routing"`。

- [ ] **Step 5: 本地上线验证** — 合并分支、`launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent`，确认 `/api/bitable/tasks` 不包含“已确认完成”、返回当前非完成动画类记录，并且未写入生产表。
