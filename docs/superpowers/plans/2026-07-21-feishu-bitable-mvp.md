# 飞书多维表格本地 MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan continuously. Do not pause for per-task review; run verification and fix failures as they appear.

**Goal:** 在本地页面完成“扫描多维表格、手动选择、人工审批、生成并写回结果附件”的可用闭环。

**Architecture:** 复用现有 `GraphRuntime` 和生成适配器，以 `BitableTaskStore` 绑定表格记录与运行；新增窄 Bitable 适配器、结果交付器和本地 MVP 服务。本地 Web 是唯一入口，机器人、卡片、多人鉴权和自动扫描均延后。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、LangGraph、aiosqlite、httpx、原生 HTML/CSS/JavaScript、pytest、Node test runner。

## Global Constraints

- 只使用现有 `文本 / 需求来源 / 执行人 / 结果` 字段，不创建或修改表格字段。
- 扫描所有来源有效且结果为空的记录，不按执行人过滤。
- 每次由本地操作者手动选择一条记录；不自动扫描、不批量领取。
- 付费生成前必须经过现有本地审批门禁。
- 成功时把全部产物写入 `结果`附件列；失败或取消时结果保持为空。
- 回写前重新读取记录，结果非空时绝不覆盖。
- 交付重试不得重新生成或重复上传。
- 真实付费生成必须另行取得用户确认。
- 不实现机器人、卡片、长连接、多人鉴权或执行人写回。

---

### Task 1: Bitable 读取与结果附件交付

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_bitable.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/bitable_delivery.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/routing_delivery.py`
- Create: `feishu-generation-agent/tests/unit/test_feishu_bitable.py`
- Create: `feishu-generation-agent/tests/unit/test_bitable_delivery.py`
- Create: `feishu-generation-agent/tests/fixtures/bitable_fields.json`
- Create: `feishu-generation-agent/tests/fixtures/bitable_records.json`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/artifact.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/ports.py`
- Modify: `feishu-generation-agent/tests/conftest.py`

**Interfaces:**
- Produces `FeishuBitableClient.resolve_location(location)`, `ensure_schema(location)`, `list_tasks(location, schema)`, `get_record(location, record_id)` and `write_result_attachments(...)`.
- Produces `BitableResultWriter` and `RoutingDeliveryWriter`, both implementing `DeliveryWriter`.

- [ ] Write failing adapter tests for wiki-to-app resolution, exact four-field type validation, view pagination, invalid/multiple source links, executor display, result filtering, record refresh and attachment payload.
- [ ] Run `cd feishu-generation-agent && uv run pytest tests/unit/test_feishu_bitable.py -q`; expect import/test failures before implementation.
- [ ] Implement the narrow adapter using `/open-apis/wiki/v2/spaces/get_node`, Bitable `/fields`, `/records` and `PUT /records/{record_id}`. `ensure_schema` must be read-only and must reject incompatible fields.
- [ ] Write failing delivery tests proving all artifacts are uploaded once, retry reuses stored upload tokens, and a non-empty result causes `BitableResultConflict` before upload/update.
- [ ] Extend `FeishuClient` with parameterized upload targets and extend `DeliveryRecord` with `target_type="bitable_record"` plus app/table/record identity while preserving docx compatibility.
- [ ] Implement `BitableResultWriter` and `RoutingDeliveryWriter`; Bitable upload operations use stable `bitable_upload:<artifact_id>` idempotency keys and re-read the record immediately before update.
- [ ] Run `uv run pytest tests/unit/test_feishu_bitable.py tests/unit/test_bitable_delivery.py tests/unit/test_feishu_delivery.py -q` and fix all failures.
- [ ] Commit as `feat(agent): add bitable mvp delivery`.

---

### Task 2: 本地领取、运行协调与重启恢复

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/bitable/__init__.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/bitable/mvp_service.py`
- Create: `feishu-generation-agent/tests/unit/test_bitable_mvp_service.py`
- Create: `feishu-generation-agent/tests/integration/test_bitable_mvp_restart.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/bitable_tasks.py`

**Interfaces:**
- `GraphRuntime.start_run(request, *, run_id: str | None = None, thread_id: str | None = None) -> str` preserves the existing no-ID behavior.
- `BitableMvpService.prepare()`, `scan()`, `claim(record_id)`, `sync_once(run_id)`, `retry_delivery(run_id)`, `resume_incomplete()` and `close()`.

- [ ] Write failing tests proving scan excludes non-empty results and active bindings but ignores executor values; claim order is store reservation then runtime start; two concurrent claims yield exactly one winner.
- [ ] Write failing tests for pre-reserved run/thread IDs and repeat start idempotency.
- [ ] Implement optional IDs in `GraphRuntime.start_run` without changing existing local-link API behavior.
- [ ] Implement `BitableMvpService` with claimant `local-mvp`, `trigger_type="bitable"`, empty safe reply context and local-only status synchronization.
- [ ] Add release semantics: cancel/failed/completed release active claim; `delivery_failed` stays bound and only permits `retry_delivery`; approval fingerprint/version remains persisted for restart.
- [ ] Write and pass restart tests proving waiting approval resumes without generation and provider-submitted runs poll/recover without a second submit.
- [ ] Run `uv run pytest tests/unit/test_bitable_mvp_service.py tests/integration/test_bitable_mvp_restart.py tests/integration/test_restart_recovery.py -q`.
- [ ] Commit as `feat(agent): coordinate bitable mvp runs`.

---

### Task 3: 本地 API、页面与服务组装

**Files:**
- Create: `feishu-generation-agent/tests/integration/test_bitable_mvp_api.py`
- Create: `feishu-generation-agent/tests/frontend/bitable_state.test.cjs`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/cli/config_probe.py`
- Modify: `feishu-generation-agent/tests/unit/test_config_probe.py`

**Interfaces:**
- `GET /api/bitable/tasks` scans and returns eligible records.
- `POST /api/bitable/tasks/{record_id}/claim` returns `202` with `run_id`.
- Existing `GET /api/runs/{run_id}` and `POST /api/runs/{run_id}/decision` remain the only detail/approval APIs.
- `POST /api/bitable/runs/{run_id}/retry-delivery` retries only saved artifacts.

- [ ] Write failing API tests for scan, manual claim, duplicate claim 409, schema/readiness errors, result conflict, and delivery retry.
- [ ] Refactor bootstrap ownership so Bitable mode requires core+generation+bitable but not legacy docx delivery fields; route Bitable runs to `BitableResultWriter` and legacy runs to the optional old writer.
- [ ] Add the four MVP endpoints with safe Chinese error mapping; do not accept actor identity from the browser.
- [ ] Write failing reducer tests for scan start/success/failure, claim success/conflict and retry-delivery states.
- [ ] Add a compact scan panel and task list; show title, source link and executor; after claim navigate into the existing run detail and approval UI.
- [ ] Preserve the existing paste-link flow when legacy delivery is configured; if it is not configured, hide or disable that entry with a clear message.
- [ ] Update config probe to report Bitable auth/schema/read readiness without sending model or generation requests.
- [ ] Run `uv run pytest tests/integration/test_bitable_mvp_api.py tests/integration/test_api.py tests/unit/test_config_probe.py -q && node --test tests/frontend/*.test.cjs`.
- [ ] Commit as `feat(agent): expose local bitable mvp`.

---

### Task 4: 无付费联调、文档与最终验收

**Files:**
- Modify: `feishu-generation-agent/.env.example`
- Modify: `feishu-generation-agent/README.md`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/cli/smoke.py`
- Modify: `feishu-generation-agent/tests/unit/test_config_probe.py`
- Modify: `feishu-generation-agent/tests/integration/test_bitable_mvp_api.py`

**Interfaces:**
- `python -m feishu_generation_agent.cli.config_probe --network` performs read-only Feishu/Bitable checks.
- Smoke workflow stops at `waiting_approval` unless explicit paid confirmation is supplied.

- [ ] Add operation documentation for configuring the table URL, starting the app, scanning, selecting, approving, retrying and diagnosing permissions.
- [ ] Add a zero-generation smoke path that uses the real table for read-only scan and stops at the approval gate.
- [ ] Verify logs, API responses and SQLite contain no configured secrets, raw event payloads or media Base64.
- [ ] Run `cd feishu-generation-agent && uv run pytest -q`; require all Python tests pass.
- [ ] Run `cd feishu-generation-agent && node --test tests/frontend/*.test.cjs`; require all frontend tests pass.
- [ ] Run `git diff --check` and a feature-branch credential scan against merge base `f3881c6`.
- [ ] Start the local app, complete the read-only real-table scan and record the result in the implementation report.
- [ ] Stop before any real Chiyun/Seedance request and ask the user for explicit paid-smoke approval.
- [ ] Commit as `docs(agent): document bitable mvp operations`.
