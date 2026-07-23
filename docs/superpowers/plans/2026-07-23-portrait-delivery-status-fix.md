# Portrait Delivery Status Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct真人任务失败状态、保留素材错误类别，并移除统一结果表的默认空记录。

**Architecture:** 保持现有 LangGraph、生产任务存储和结果写入器边界不变。错误枚举在集成层修正，终态映射在生产服务层修正，空记录清理通过飞书客户端的显式记录接口完成。

**Tech Stack:** Python 3.12、pytest、httpx、FastAPI、SQLite、飞书开放平台 API。

## Global Constraints

- 不自动重跑“茶壶青蛙”。
- 只删除 `fields == {}` 且具有有效 `record_id` 的结果表记录。
- 不修改生产需求源表。
- 不引入新依赖。

---

### Task 1: 修复真人素材临时错误类别

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/volcengine_portrait.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/seedance.py`
- Test: `feishu-generation-agent/tests/unit/test_volcengine_portrait.py`
- Test: `feishu-generation-agent/tests/unit/test_seedance.py`

**Interfaces:**
- Consumes: `ErrorCategory.TRANSIENT`
- Produces: 素材临时错误统一抛出 `AgentError(detail.category == ErrorCategory.TRANSIENT)`

- [ ] **Step 1: Write failing tests**

新增两个测试，分别让真人图片托管和 Seedance 音视频托管抛出 `PublicMediaUploadError`，断言最终 `AgentError.detail.category is ErrorCategory.TRANSIENT`。

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/unit/test_volcengine_portrait.py tests/unit/test_seedance.py -q`

Expected: FAIL，错误指出 `ErrorCategory.PROVIDER_TRANSIENT` 不存在。

- [ ] **Step 3: Implement minimal fix**

将两个 `ErrorCategory.PROVIDER_TRANSIENT` 改为 `ErrorCategory.TRANSIENT`。

- [ ] **Step 4: Verify tests pass**

Run: `python -m pytest tests/unit/test_volcengine_portrait.py tests/unit/test_seedance.py -q`

Expected: PASS。

### Task 2: 修复生产任务终态映射

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bitable/production_service.py`
- Test: `feishu-generation-agent/tests/unit/test_production_service.py`

**Interfaces:**
- Consumes: `GraphRuntime.get_run_view()["status"]`
- Produces: `completed_with_errors -> TableTaskStatus.FAILED`

- [ ] **Step 1: Write failing test**

创建已领取的生产任务，模拟运行状态为 `completed_with_errors`，调用 `sync_once` 后断言任务状态为失败且锁已释放。

- [ ] **Step 2: Verify test fails**

Run: `python -m pytest tests/unit/test_production_service.py -q`

Expected: FAIL，实际状态为已完成。

- [ ] **Step 3: Implement minimal fix**

把 `_RELEASED_STATUSES["completed_with_errors"]` 改为 `TableTaskStatus.FAILED`。

- [ ] **Step 4: Verify test passes**

Run: `python -m pytest tests/unit/test_production_service.py -q`

Expected: PASS。

### Task 3: 清理结果表默认空记录

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/production_delivery.py`
- Test: `feishu-generation-agent/tests/unit/test_production_delivery.py`

**Interfaces:**
- Produces: `FeishuClient.list_bitable_records(app_token, table_id) -> list[dict]`
- Produces: `FeishuClient.delete_bitable_record(app_token, table_id, record_id) -> None`
- Produces: `ProductionResultWriter.cleanup_empty_records() -> int`

- [ ] **Step 1: Write failing tests**

模拟新建结果表返回两个 `fields == {}` 的默认记录和一个含字段记录，断言只删除两个空记录；单独验证清理方法返回删除数量。

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/unit/test_production_delivery.py -q`

Expected: FAIL，客户端或写入器尚无清理接口。

- [ ] **Step 3: Implement minimal fix**

增加记录读取、删除接口；结果表建好字段后调用严格空记录清理。保留所有非空记录。

- [ ] **Step 4: Verify tests pass**

Run: `python -m pytest tests/unit/test_production_delivery.py -q`

Expected: PASS。

### Task 4: 验证、合并和部署

**Files:**
- Verify: `feishu-generation-agent/`

- [ ] **Step 1: Run full backend suite**

Run: `python -m pytest -q`

Expected: 882 个基线测试加新增测试全部通过。

- [ ] **Step 2: Run frontend suite**

Run: `node --test tests/frontend/*.test.mjs`

Expected: 全部通过。

- [ ] **Step 3: Commit and merge**

提交修复分支，合并回 `main`，再在 `main` 运行完整测试。

- [ ] **Step 4: Clean existing empty records**

只读确认统一结果表记录；调用 `cleanup_empty_records()`，确认恰好删除 10 条 `fields == {}` 的记录，并再次只读确认剩余记录非空。

- [ ] **Step 5: Restart safely**

确认没有生成中的任务后重启 `com.feishu-generation-agent`，验证 8765 监听、运行 API 和生产任务扫描。
