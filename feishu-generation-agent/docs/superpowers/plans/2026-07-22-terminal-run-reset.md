# 终态任务复位与重跑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多维表格任务终态可明确收尾、刷新后可查看最近结果，并可安全重跑为新的飞书结果表记录。

**Architecture:** 后端以 production binding 的本地 SQLite 数据为最近终态记录来源，并通过复制原审批状态创建新的独立 run。前端为单个活动 run 管理一个可停止的轮询器，在终态显示复位和重跑操作；最近记录只读、可恢复详情。

**Tech Stack:** Python 3.12、FastAPI、aiosqlite、现有 LangGraph runtime、无构建的浏览器 JavaScript、pytest、node:test。

## Global Constraints

- 生产需求表只读；结果只写入现有每位制作人的飞书结果表。
- 客户端仅调用同源相对 API 路径；禁止写入本机路径、IP、密钥或服务端数据库位置。
- SQLite 历史表通过幂等建表/迁移创建，沿用配置化数据目录，支持迁移到另一台服务机。
- 重跑必须新建 `run_id`、`thread_id` 和结果表行，不能覆盖旧产物或旧记录。
- `production_task_history` 保存被新版本替换的终态绑定；当前表与历史表共同提供运行详情和最近记录。
- 重跑必须再次经过人工批准，批准前不得调用生成供应商。
- 终态为 `succeeded`、`completed_with_errors`、`failed`、`cancelled`、`delivery_failed`。
- 新增后端行为先写失败测试，再写最小实现。

---

### Task 1: 生产运行的最近记录与重跑服务

**Files:**

- Modify: `src/feishu_generation_agent/storage/production_tasks.py`
- Modify: `src/feishu_generation_agent/bitable/production_service.py`
- Test: `tests/unit/test_production_service.py`

**Interfaces:**

- Produces `ProductionTaskStore.list_recent(app_token, table_id, limit=10) -> list[ProductionBinding]`。
- Produces `ProductionBitableService.recent_runs()` 和 `ProductionBitableService.rerun(run_id) -> str`。

- [ ] **Step 1: 写入失败测试**

```python
async def test_service_lists_terminal_production_runs_newest_first(tmp_path) -> None:
    # 创建同一 source location 的两个终态 binding 和另一个 location 的 binding。
    # 断言 recent_runs 只返回前者，且按 updated_at 倒序。

async def test_service_rerun_creates_a_new_waiting_approval_run(tmp_path) -> None:
    # 原运行终态且具备已审批计划。
    # 断言新 run_id/thread_id 与原运行不同，原交付记录不变。
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/unit/test_production_service.py -q`

Expected: FAIL，因为 `recent_runs` 和 `rerun` 尚不存在。

- [ ] **Step 3: 最小实现**

```python
async def list_recent(self, app_token: str, table_id: str, *, limit: int = 10):
    # UNION 当前表和 production_task_history 的终态绑定，按 updated_at DESC LIMIT ?

async def rerun(self, run_id: str) -> str:
    # 归档原 binding 后创建新 binding；复制原审批计划为新 run；不得调用 resume_run。
```

- [ ] **Step 4: 验证通过并提交**

Run: `pytest tests/unit/test_production_service.py -q`

Expected: PASS。

Commit:

```bash
git add src/feishu_generation_agent/storage/production_tasks.py src/feishu_generation_agent/bitable/production_service.py tests/unit/test_production_service.py
git commit -m "feat(agent): add recent production runs and rerun service"
```

### Task 2: 多维表格 API 契约

**Files:**

- Modify: `src/feishu_generation_agent/web/app.py`
- Test: `tests/integration/test_production_bitable_api.py`

**Interfaces:**

- Produces `GET /api/bitable/recent-runs`。
- Produces `POST /api/bitable/runs/{run_id}/rerun`，返回 `{"run_id": "<new-id>"}`。

- [ ] **Step 1: 写入失败 API 测试**

```python
async def test_recent_runs_and_rerun_endpoints(tmp_path) -> None:
    recent = await client.get("/api/bitable/recent-runs")
    assert recent.json()[0]["rerunnable"] is True
    rerun = await client.post("/api/bitable/runs/run-old/rerun")
    assert rerun.status_code == 202
    assert rerun.json()["run_id"] == "run-new"
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/integration/test_production_bitable_api.py -q`

Expected: FAIL，端点尚未注册。

- [ ] **Step 3: 最小实现并验证**

```python
@app.get("/api/bitable/recent-runs")
async def list_recent_bitable_runs(request: Request) -> list[dict]: ...

@app.post("/api/bitable/runs/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
async def rerun_bitable_run(run_id: str, request: Request) -> BitableClaimResponse: ...
```

每条最近记录返回 `run_id`、`display_text`、`status`、`updated_at`、`result_table_url`、`rerunnable`。运行冲突继续由现有 409/422 映射处理。

Run: `pytest tests/integration/test_production_bitable_api.py -q`

Expected: PASS。

### Task 3: 前端终态收尾、复位与最近记录

**Files:**

- Modify: `src/feishu_generation_agent/web/static/bitable-state.js`
- Modify: `src/feishu_generation_agent/web/static/index.html`
- Modify: `src/feishu_generation_agent/web/static/app.js`
- Modify: `src/feishu_generation_agent/web/static/styles.css`
- Test: `tests/frontend/bitable_state.test.cjs`

**Interfaces:**

- Consumes `GET /api/bitable/recent-runs` 和 `POST /api/bitable/runs/{run_id}/rerun`。
- Produces `BitableState.recentSucceeded` 与 `BitableState.resetRunContext`。

- [ ] **Step 1: 写入失败前端状态测试**

```javascript
test("recent run state resets active context while retaining terminal history", () => {
  let state = BitableState.recentSucceeded(BitableState.createState(), [
    { run_id: "run-1", status: "succeeded" },
  ]);
  state = BitableState.resetRunContext(state);
  assert.equal(state.claim.runId, null);
  assert.equal(state.recentRuns[0].run_id, "run-1");
});
```

- [ ] **Step 2: 验证测试失败**

Run: `node --test tests/frontend/bitable_state.test.cjs`

Expected: FAIL，状态转换尚未导出。

- [ ] **Step 3: 最小实现**

```javascript
const TERMINAL_RUN_STATUSES = new Set([
  "succeeded", "completed_with_errors", "failed", "cancelled", "delivery_failed",
]);

function stopPolling() { clearInterval(state.pollTimer); state.pollTimer = null; }
function resetForNextTask() { stopPolling(); /* 清空 runId/view/review，保留 recentRuns */ }
```

终态显示“开始下一任务”；可重跑时显示“重跑此任务”。`poll()` 收到终态后停止轮询；最近记录可打开只读详情或创建新的等待审批运行。

- [ ] **Step 4: 验证通过并提交**

Run: `node --test tests/frontend/bitable_state.test.cjs`

Expected: PASS。

Commit:

```bash
git add src/feishu_generation_agent/web/static tests/frontend/bitable_state.test.cjs
git commit -m "feat(agent): reset terminal runs and expose rerun UI"
```

### Task 4: 全量验证

**Files:**

- Modify: `docs/superpowers/plans/2026-07-22-terminal-run-reset.md`

- [ ] **Step 1: 运行全量测试**

Run: `pytest -q && node --test tests/frontend/*.test.cjs`

Expected: 全部通过。

- [ ] **Step 2: 本地 HTTP 冒烟验证**

Run: `feishu-generation-agent --port 8766`，检查 `/health` 与 `/api/bitable/recent-runs`。

Expected: `ready: true`，最近记录端点返回数组，生产需求表无写入。
