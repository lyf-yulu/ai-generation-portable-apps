# Feishu Agent Portal User Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有飞书生成 Agent 以外部守护应用方式接入 Portal，为每个 Portal 用户提供独立且可修改的中文计划提示词，同时完整读取飞书需求文档内嵌电子表格图片、显式核对素材覆盖，并保持本机 Prime 版本和共享生产任务锁不变。

**Architecture:** Portal 只负责认证、稳定用户身份和反向代理，不接管 8765 的 Agent 进程。Agent 以 `portal_user_id` 隔离提示词与运行记录，以同一生产任务数据库维持跨用户共享锁；领取任务时固化提示词快照。Block 30 通过现有飞书 Drive 导出接口取得 XLSX，再用 Python 标准库解析文字、图片关系和锚点。不可编辑执行契约约束 JSON、中文和素材覆盖，用户只能编辑自己的业务规划偏好。

**Tech Stack:** Python 3.12、FastAPI、LangGraph、LangChain、Pydantic v2、aiosqlite、httpx、Python stdlib `zipfile`/`xml.etree.ElementTree`、Portal stdlib `http.server`、原生 HTML/CSS/JavaScript、Node.js 内置测试运行器、pytest。

## Global Constraints

- 不改动或重排当前未提交的 `seedance/`、`.codex/`、`portal/state/`、`tools/pack_user_data.py` 和根目录 `AGENTS.md`。
- 不把 Portal 地址、局域网 IP、协议或域名写死；浏览器始终使用当前页面的相对挂载路径，服务端使用现有 Host/Forwarded 头。
- 不把飞书、DeepSeek、Seedance、Chiyun、Volcengine 的密钥写进源码、前端响应、测试夹具或日志。
- Agent 继续由 `com.feishu-generation-agent` 守护；Portal 对它只探活和代理，绝不启动、清端口、杀死或重启。
- 本机直接访问 8765 时使用逐字不变的 Prime 提示词；Portal 用户配置不得写回 Prime。
- Portal 身份只能来自服务端会话，代理必须覆盖浏览器伪造的同名头。
- 所有结构迁移必须可重复执行；现有运行归属 `prime-local`，现有生产任务锁仍全局共享。
- 任何中文、素材覆盖或结构校验失败都必须停在付费生成之前。
- 前端保持零构建依赖；新增 JavaScript 逻辑拆成可由 Node.js 直接测试的 UMD 模块。
- 后端重启前先确认没有运行中生成任务；前端静态改动可直接刷新验证。
- 每个任务只提交该任务涉及的文件；永不 amend、force-push 或覆盖 Git 历史。

---

## File Map

### Portal

- Modify: `portal/app_spec.py`
- Modify: `portal/apps.json`
- Modify: `portal/app.py`
- Modify: `portal/static/index.html`
- Modify: `portal/static/app.js`
- Modify: `tests/test_app_spec_loader.py`
- Modify: `tests/test_portal_startup.py`
- Modify: `tests/test_dispatch_via_spec.py`

### Agent domain, graph, and storage

- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/document.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/plan.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/production_bitable.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/ports.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/state.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/production_tasks.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/planner_prompts.py`

### Agent integrations and bootstrap

- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_source.py`
- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_sheet_export.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bitable/production_service.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py`

### Agent frontend

- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/review-state.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/api-paths.js`
- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/planner-prompt-state.js`

### Tests

- Modify: `feishu-generation-agent/tests/conftest.py`
- Modify: `feishu-generation-agent/tests/unit/test_domain.py`
- Modify: `feishu-generation-agent/tests/unit/test_planner.py`
- Modify: `feishu-generation-agent/tests/unit/test_feishu_source.py`
- Modify: `feishu-generation-agent/tests/unit/test_production_service.py`
- Modify: `feishu-generation-agent/tests/unit/test_production_tasks.py`
- Modify: `feishu-generation-agent/tests/unit/test_storage.py`
- Modify: `feishu-generation-agent/tests/integration/test_api.py`
- Modify: `feishu-generation-agent/tests/integration/test_production_bitable_api.py`
- Modify: `feishu-generation-agent/tests/frontend/review_state.test.cjs`
- Create: `feishu-generation-agent/tests/unit/test_planner_prompts.py`
- Create: `feishu-generation-agent/tests/unit/test_feishu_sheet_export.py`
- Create: `feishu-generation-agent/tests/frontend/api_paths.test.cjs`
- Create: `feishu-generation-agent/tests/frontend/planner_prompt_state.test.cjs`

---

## Task 1: Register the Agent as an Externally Managed Portal App

**Files:**

- Modify: `portal/app_spec.py`
- Modify: `portal/apps.json`
- Modify: `portal/app.py`
- Modify: `tests/test_app_spec_loader.py`
- Modify: `tests/test_portal_startup.py`

- [ ] **Step 1: Write failing AppSpec tests**

In `tests/test_app_spec_loader.py`, update the production app list assertion to include `feishu-generation-agent`, then add assertions:

```python
agent = next(spec for spec in self.specs if spec.name == "feishu-generation-agent")
self.assertFalse(agent.managed)
self.assertEqual(agent.mount, "iframe")
self.assertEqual(agent.iframe_url, "/feishu-generation-agent/")
self.assertEqual(agent.port, 8765)
```

Add a temporary-spec assertion proving omitted `managed` defaults to `True`.

- [ ] **Step 2: Write failing lifecycle tests**

In `tests/test_portal_startup.py`, construct one managed and one unmanaged app config. Patch `start_app`, `_kill_port_squatter`, and `_tcp_probe`, then verify:

- `start_all()` calls `start_app` only for the managed app;
- the unmanaged app never reaches `_kill_port_squatter`;
- an unhealthy unmanaged app is reported unavailable but is never restarted;
- stopping Portal does not send a signal to the unmanaged process.

- [ ] **Step 3: Run the focused tests and confirm failure**

Run:

```bash
python3 -m unittest tests.test_app_spec_loader tests.test_portal_startup
```

Expected: failures mention missing `AppSpec.managed`, missing fifth app, and unmanaged lifecycle behavior.

- [ ] **Step 4: Add `managed` to the app specification**

In `portal/app_spec.py`:

```python
@dataclass(frozen=True)
class AppSpec:
    # existing fields
    managed: bool = True
```

Parse it with:

```python
managed=bool(d.get("managed", True))
```

Append this `portal/apps.json` entry without changing existing app entries:

```json
{
  "name": "feishu-generation-agent",
  "display_name": "飞书任务 Agent",
  "dir": "feishu-generation-agent",
  "port_env": "FEISHU_GENERATION_AGENT_PORT",
  "port_default": 8765,
  "mount": "iframe",
  "iframe_url": "/feishu-generation-agent/",
  "managed": false,
  "credential_scheme": "none",
  "job_type": "dynamic",
  "metrics": ["images", "seconds"],
  "unit_label": "项"
}
```

- [ ] **Step 5: Make `AppManager` honor external ownership**

In `portal/app.py`:

- `start_all()` calls `start_app()` only when `config["spec"].managed` is true;
- initialize unmanaged status from `_tcp_probe(port)` as `running` or `unavailable`, without a PID;
- `_health_loop()` probes unmanaged ports but never invokes `start_app()` or `_kill_port_squatter()`;
- shutdown only iterates `self.processes`, which must never contain the unmanaged Agent.

Do not add a fallback that launches `feishu-generation-agent` when 8765 is unavailable.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
python3 -m unittest tests.test_app_spec_loader tests.test_portal_startup tests.test_dispatch_via_spec
```

Expected: all tests pass and the golden production registry contains five apps.

- [ ] **Step 7: Commit**

```bash
git add portal/app_spec.py portal/apps.json portal/app.py tests/test_app_spec_loader.py tests/test_portal_startup.py
git commit -m "feat(portal): register externally managed feishu agent"
```

---

## Task 2: Add the Portal Tab and Trusted User Identity Header

**Files:**

- Modify: `portal/app.py`
- Modify: `portal/static/index.html`
- Modify: `portal/static/app.js`
- Modify: `tests/test_dispatch_via_spec.py`

- [ ] **Step 1: Write a failing proxy identity test**

In `tests/test_dispatch_via_spec.py`, proxy a request with:

```python
user = {"user_id": "user-a-immutable", "username": "测试用户", "role": "user"}
```

Also send a forged browser header `X-Portal-User-Id: attacker`. Assert the request received by the fake upstream contains:

```python
assert upstream_headers["X-Portal-User-Id"] == "user-a-immutable"
assert upstream_headers["X-Username"] == "%E6%B5%8B%E8%AF%95%E7%94%A8%E6%88%B7"
assert upstream_headers["X-Portal-User-Id"] != "attacker"
```

Keep existing signature assertions unchanged.

- [ ] **Step 2: Write a failing navigation test**

Add an HTML/JavaScript source assertion that the Portal renders the registered iframe URL rather than a literal host such as `192.168.30.5`, `localhost:8765`, or `127.0.0.1:8765`.

- [ ] **Step 3: Run and confirm failure**

Run:

```bash
python3 -m unittest tests.test_dispatch_via_spec
```

Expected: the upstream request lacks `X-Portal-User-Id` and the new navigation assertion fails.

- [ ] **Step 4: Overwrite the identity header in `_proxy()`**

In `portal/app.py`, do not copy `X-Portal-User-Id` from `self.headers`. After the Portal session user is resolved, set:

```python
headers["X-Portal-User-Id"] = str(user["user_id"])
```

Continue percent-encoding `X-Username`. Keep `X-Portal-Ts` and `X-Portal-Sig` intact.

- [ ] **Step 5: Render a configuration-driven iframe tab**

In `portal/static/index.html` and `portal/static/app.js`, add the “飞书任务 Agent” tab using `/feishu-generation-agent/`. The iframe URL must be relative to the current Portal origin. Reuse the existing Portal tab visibility and `use_apps` permission behavior.

- [ ] **Step 6: Run tests**

Run:

```bash
python3 -m unittest tests.test_dispatch_via_spec tests.test_app_spec_loader
```

Expected: all tests pass; no production URL or IP is hardcoded.

- [ ] **Step 7: Commit**

```bash
git add portal/app.py portal/static/index.html portal/static/app.js tests/test_dispatch_via_spec.py
git commit -m "feat(portal): proxy feishu agent with stable user identity"
```

---

## Task 3: Make the Agent Frontend Work Both Directly and Under a Portal Prefix

**Files:**

- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/api-paths.js`
- Create: `feishu-generation-agent/tests/frontend/api_paths.test.cjs`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`

- [ ] **Step 1: Write prefix-path tests**

Create `tests/frontend/api_paths.test.cjs` using `node:test`. Test:

```javascript
assert.equal(paths.basePath("/"), "");
assert.equal(paths.basePath("/feishu-generation-agent/"), "/feishu-generation-agent");
assert.equal(
  paths.apiUrl("/feishu-generation-agent/", "/api/health"),
  "/feishu-generation-agent/api/health"
);
assert.equal(paths.assetUrl("/", "/static/styles.css"), "/static/styles.css");
```

Also assert an unrelated path such as `/portal/` does not become a guessed Agent prefix.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
node --test tests/frontend/api_paths.test.cjs
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement a small UMD path helper**

Create `web/static/api-paths.js` exporting:

```javascript
basePath(pathname)
apiUrl(pathname, absolutePath)
assetUrl(pathname, absolutePath)
```

Recognize only the explicit `/feishu-generation-agent` mount. Normalize leading slashes and never inspect the hostname.

- [ ] **Step 4: Convert frontend URLs**

In `index.html`, change the stylesheet and script references to relative paths such as `static/styles.css` and `static/app.js`, and load `api-paths.js` before `app.js`.

In `app.js`, route every Agent API, static-resource, preview, media, and download URL through the helper. Do not alter external `https://` result URLs or blob URLs.

In `web/app.py`, keep cache-control detection valid after Portal has stripped the mount prefix and forwarded requests such as `/static/app.js`.

- [ ] **Step 5: Run frontend and API tests**

Run:

```bash
cd feishu-generation-agent
node --test tests/frontend/*.test.cjs
uv run pytest tests/integration/test_api.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/web/static/api-paths.js feishu-generation-agent/src/feishu_generation_agent/web/static/index.html feishu-generation-agent/src/feishu_generation_agent/web/static/app.js feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/tests/frontend/api_paths.test.cjs
git commit -m "fix(agent): support direct and portal-prefixed frontend paths"
```

---

## Task 4: Persist Per-User Planner Prompt Profiles

**Files:**

- Create: `feishu-generation-agent/src/feishu_generation_agent/storage/planner_prompts.py`
- Create: `feishu-generation-agent/tests/unit/test_planner_prompts.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`

- [ ] **Step 1: Write storage tests**

Create `test_planner_prompts.py` covering:

- `PlannerPromptStore.open(path)` creates the table;
- missing user returns `None`;
- first save creates version 1;
- second save for the same user creates version 2;
- user A and B values are independent;
- `delete("user-a")` removes only A;
- blank text and text longer than 20,000 Unicode characters are rejected;
- reopening the same database preserves the row.

Use exact public records:

```python
profile = await store.save(
    portal_user_id="user-a",
    username="甲",
    prompt_text="优先保持人物造型一致，并按镜头拆分任务。",
)
assert profile.version == 1
assert profile.portal_user_id == "user-a"
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_planner_prompts.py -q
```

Expected: import failure for the new store.

- [ ] **Step 3: Implement the store**

Create immutable `PlannerPromptProfile` and `PlannerPromptStore` with:

```python
@classmethod
async def open(cls, path: Path) -> "PlannerPromptStore"

async def get(self, portal_user_id: str) -> PlannerPromptProfile | None

async def save(
    self,
    *,
    portal_user_id: str,
    username: str,
    prompt_text: str,
) -> PlannerPromptProfile

async def delete(self, portal_user_id: str) -> bool

async def close(self) -> None
```

Use a table:

```sql
CREATE TABLE IF NOT EXISTS planner_prompt_profiles (
  portal_user_id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Use `BEGIN IMMEDIATE` around version increments. Store timestamps in UTC ISO 8601. Never log `prompt_text`.

- [ ] **Step 4: Wire lifecycle ownership**

Open the prompt store from `bootstrap.py` with its own SQLite connection to `settings.business_db_path`, and close it with the application services. Add `planner_prompt_store: PlannerPromptStore` to `ApplicationServices`.

Extend the application factory with an injectable test dependency:

```python
def create_app(
    *,
    runtime: GraphRuntime | None = None,
    services: GraphServices | None = None,
    settings: Settings | None = None,
    bitable_service: BitableMvpService | Any | None = None,
    planner_prompt_store: PlannerPromptStore | None = None,
) -> FastAPI:
```

Store the active instance at `app.state.planner_prompt_store`. The bootstrap-owned store closes in `_open_application_services`; an injected test store remains owned by its fixture. Do not open one connection per request.

- [ ] **Step 5: Run tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_planner_prompts.py tests/unit/test_config.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/storage/planner_prompts.py feishu-generation-agent/src/feishu_generation_agent/bootstrap.py feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/tests/unit/test_planner_prompts.py
git commit -m "feat(agent): persist per-user planner prompt profiles"
```

---

## Task 5: Expose Current-User Prompt APIs Without Changing Prime

**Files:**

- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/schemas.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Modify: `feishu-generation-agent/tests/integration/test_api.py`
- Modify: `feishu-generation-agent/tests/unit/test_planner.py`

- [ ] **Step 1: Freeze the existing Prime text**

In `test_planner.py`, import the accessor used by production code and assert:

```python
assert hashlib.sha256(prime.encode("utf-8")).hexdigest() == (
    "5dd2463a9bfddb3bc9e55c3a93148f7316f1259a6eaf70644a610b386a9c6ce4"
)
```

This test must use the exact pre-feature `_PLAN_SYSTEM_PROMPT`, not a normalized or stripped value.

- [ ] **Step 2: Write prompt API tests**

In `test_api.py`, create an app with a temporary prompt store and test:

- direct local `GET /api/planner-prompt` returns `mode="prime"`, `editable=false`, Prime text, and no Portal ID;
- direct local `PUT` and `DELETE` return 403;
- Portal user A `GET` initially inherits Prime;
- A `PUT` saves version 1 and then version 2;
- user B remains on Prime;
- A `DELETE` returns to Prime without changing B or the Prime hash;
- a forged Portal ID in the JSON body is ignored because identity comes only from the request header;
- whitespace and over-limit values return 422.

Use request headers:

```python
{"X-Portal-User-Id": "user-a", "X-Username": "%E7%94%B2"}
```

- [ ] **Step 3: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_planner.py tests/integration/test_api.py -q
```

Expected: prompt endpoints and Prime accessor are missing.

- [ ] **Step 4: Add identity and response models**

In `web/app.py`, add immutable request identity:

```python
@dataclass(frozen=True, slots=True)
class RequestIdentity:
    owner_user_id: str
    portal_user_id: str | None
    username: str
    is_portal: bool
```

`current_identity(request)` must:

- use `X-Portal-User-Id` when present;
- URL-decode `X-Username`;
- otherwise return `owner_user_id="prime-local"` and `is_portal=False`;
- reject malformed or overlong identity values with 400;
- never accept user ID from query parameters or JSON.

In `web/schemas.py`, add `PlannerPromptUpdate` with `prompt_text` length 1–20,000.

- [ ] **Step 5: Add prompt endpoints**

Implement:

```python
GET /api/planner-prompt
PUT /api/planner-prompt
DELETE /api/planner-prompt
```

Response fields:

```json
{
  "mode": "prime",
  "editable": true,
  "prompt_text": "完整有效提示词",
  "version": 0,
  "source": "prime"
}
```

For a personal copy use `mode="personal"`, `source="personal"`, and the saved version. Direct mode sets `editable=false`. Do not return another user’s identifier or prompt.

- [ ] **Step 6: Run tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_planner.py tests/unit/test_planner_prompts.py tests/integration/test_api.py -q
```

Expected: all tests pass and the Prime hash remains exact.

- [ ] **Step 7: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/src/feishu_generation_agent/web/schemas.py feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py feishu-generation-agent/tests/integration/test_api.py feishu-generation-agent/tests/unit/test_planner.py
git commit -m "feat(agent): expose isolated planner prompt settings"
```

---

## Task 6: Add the Planner Prompt Settings UI

**Files:**

- Create: `feishu-generation-agent/src/feishu_generation_agent/web/static/planner-prompt-state.js`
- Create: `feishu-generation-agent/tests/frontend/planner_prompt_state.test.cjs`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css`

- [ ] **Step 1: Write state-machine tests**

Create `planner_prompt_state.test.cjs` and test:

- Prime response renders “使用 Prime”;
- personal version 3 renders “使用个人版本 v3”;
- direct `editable=false` hides/disables the entry;
- saving disables duplicate clicks and surfaces success;
- a 422 response leaves the editor open and shows the server error;
- reset restores returned Prime content;
- dirty text is not silently discarded when the modal closes.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
node --test tests/frontend/planner_prompt_state.test.cjs
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement the UMD state module**

Export pure functions for:

```javascript
createPlannerPromptState()
applyPlannerPromptResponse(state, payload)
beginPromptSave(state)
finishPromptSave(state, payload)
failPromptSave(state, message)
markPromptDirty(state, promptText)
```

Do not perform network calls inside this module.

- [ ] **Step 4: Add the modal**

In `index.html` add:

- a “计划提示词设置” button shown only when `editable`;
- mode/version label;
- a textarea initialized from the effective prompt;
- “保存个人版本” and “恢复 Prime” buttons;
- the note “修改只影响之后领取的新任务”;
- live success/error status with `aria-live="polite"`.

In `app.js`, use the prefix-safe `apiUrl()` helper for GET/PUT/DELETE. Fetch the state during initialization. Show an explicit loading response immediately after clicks.

- [ ] **Step 5: Add styles without changing the production workflow layout**

In `styles.css`, reuse existing modal tokens and keep the textarea usable at common laptop widths. Avoid adding a new framework or build step.

- [ ] **Step 6: Run frontend tests**

Run:

```bash
cd feishu-generation-agent
node --test tests/frontend/*.test.cjs
```

Expected: all frontend tests pass.

- [ ] **Step 7: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/web/static/planner-prompt-state.js feishu-generation-agent/src/feishu_generation_agent/web/static/index.html feishu-generation-agent/src/feishu_generation_agent/web/static/app.js feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css feishu-generation-agent/tests/frontend/planner_prompt_state.test.cjs
git commit -m "feat(agent-ui): add personal planner prompt editor"
```

---

## Task 7: Add Run Ownership While Keeping the Production Lock Shared

**Files:**

- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/repository.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/storage/production_tasks.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/production_bitable.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bitable/production_service.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/tests/unit/test_storage.py`
- Modify: `feishu-generation-agent/tests/unit/test_production_tasks.py`
- Modify: `feishu-generation-agent/tests/unit/test_production_service.py`
- Modify: `feishu-generation-agent/tests/integration/test_api.py`
- Modify: `feishu-generation-agent/tests/integration/test_production_bitable_api.py`

- [ ] **Step 1: Write migration and repository tests**

Create a legacy SQLite database with the current `runs` schema, open it through `Repository.open()`, and assert:

```python
run = await repository.get_run("legacy-run", owner_user_id="prime-local")
assert run["owner_user_id"] == "prime-local"
assert await repository.get_run("legacy-run", owner_user_id="user-a") is None
```

Add tests for:

Exercise the exact repository calls `create_run("run-a", "thread-a", source_url, owner_user_id="user-a")`, `get_run("run-a", owner_user_id="user-a")`, `list_runs(owner_user_id="user-a", statuses={"waiting_approval"})`, and `delete_run("run-a", owner_user_id="user-a")`.

Assert B cannot read, update, or delete A’s run.

- [ ] **Step 2: Write production lock tests**

In `test_production_tasks.py`, prove:

- A claims a source record and gets a binding owned by A;
- B cannot claim the same record while A holds it;
- A’s `list_active(owner_user_id="user-a")` returns it;
- B’s active/recent lists do not;
- release, cancellation, timeout, and completion retain existing global lock semantics.

Add `owner_user_id` to `ProductionBinding`.

- [ ] **Step 3: Write API ownership tests**

In integration tests, create runs for A and B and assert all of these return 404 for the wrong owner:

- run detail;
- decision;
- cancel/delete;
- retry delivery;
- rerun;
- add/list/remove/replace references.

Scan remains shared. Active and recent run lists are owner-filtered.

- [ ] **Step 4: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_storage.py tests/unit/test_production_tasks.py tests/unit/test_production_service.py tests/integration/test_api.py tests/integration/test_production_bitable_api.py -q
```

Expected: missing columns, signatures, and ownership checks fail.

- [ ] **Step 5: Implement idempotent migrations**

In `repository.py`, add:

```sql
owner_user_id TEXT NOT NULL DEFAULT 'prime-local'
```

Use `PRAGMA table_info(runs)` before `ALTER TABLE`. Change public methods to require `owner_user_id` for user-visible reads and writes. Internal recovery methods may explicitly request all owners.

In `production_tasks.py`, add the same non-null column to both `production_tasks` and `production_task_history`. Because history currently uses `INSERT ... SELECT *`, replace it with explicit column lists so future migrations cannot reorder data.

- [ ] **Step 6: Thread ownership through services and routes**

Pass `current_identity(request).owner_user_id` into claim, rerun, resume, active, recent, detail, decision, reference mutation, delete, and retry methods. Keep scan public to all authenticated Portal users and keep claim uniqueness based on source app/table/record rather than owner.

Return 404, not 403, for a run owned by someone else.

- [ ] **Step 7: Run focused tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_storage.py tests/unit/test_production_tasks.py tests/unit/test_production_service.py tests/integration/test_api.py tests/integration/test_production_bitable_api.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/storage/repository.py feishu-generation-agent/src/feishu_generation_agent/storage/production_tasks.py feishu-generation-agent/src/feishu_generation_agent/domain/production_bitable.py feishu-generation-agent/src/feishu_generation_agent/bitable/production_service.py feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/tests/unit/test_storage.py feishu-generation-agent/tests/unit/test_production_tasks.py feishu-generation-agent/tests/unit/test_production_service.py feishu-generation-agent/tests/integration/test_api.py feishu-generation-agent/tests/integration/test_production_bitable_api.py
git commit -m "feat(agent): isolate runs by portal user"
```

---

## Task 8: Snapshot the Effective Prompt and Preserve It Through LangGraph

**Files:**

- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/document.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/ports.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/state.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bitable/production_service.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/tests/conftest.py`
- Modify: `feishu-generation-agent/tests/graph/test_approval_graph.py`
- Modify: `feishu-generation-agent/tests/unit/test_planner.py`
- Modify: `feishu-generation-agent/tests/integration/test_production_bitable_api.py`

- [ ] **Step 1: Write prompt snapshot tests**

Add:

```python
class PlanningPromptSnapshot(BaseModel):
    owner_user_id: str
    source: Literal["prime", "personal"]
    version: int = Field(ge=0)
    prompt_text: str
    prompt_sha256: str
```

Test that:

- A claim with personal v2 stores the full snapshot in initial graph state;
- changing A to v3 after claim does not affect re-planning of that run;
- a new run uses v3;
- direct local creates source `prime`, version 0, and the fixed Prime hash;
- approved-task rerun reuses the approved plan and does not reload the profile.

- [ ] **Step 2: Write planner composition tests**

Test `DeepSeekPlanner.plan(document, visions, feedback, system_prompt=portal_prompt)` sends:

- the exact original Prime string when `system_prompt` is absent;
- immutable Portal contract plus the snapshot user prompt when present;
- contract text before user text;
- no full prompt in logs or raised error details.

- [ ] **Step 3: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/graph/test_approval_graph.py tests/unit/test_planner.py tests/integration/test_production_bitable_api.py -q
```

Expected: snapshot model/state and planner override are missing.

- [ ] **Step 4: Implement the snapshot model and request propagation**

Add `planning_prompt` to `RequirementRequest` and `AgentState`. Ensure `_document_for_checkpoint()` does not mutate it. Serialize it with existing Pydantic JSON handling so LangGraph checkpoints can recover it after restart.

Extend `RequirementPlanner.plan` and `DeepSeekPlanner.plan` with:

```python
system_prompt: str | None = None
```

The default path must use the exact Prime constant. Portal path receives a composed system prompt made from the immutable contract and snapshot business prompt.

- [ ] **Step 5: Resolve the effective profile at claim time**

In `web/app.py` or `production_service.py`, load the current user profile before creating `RequirementRequest`. Build and persist the snapshot once. Do not re-query the prompt store inside `plan_requirements`.

Log only:

- owner user ID;
- source;
- version;
- SHA-256.

- [ ] **Step 6: Run tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/graph/test_approval_graph.py tests/unit/test_planner.py tests/integration/test_production_bitable_api.py tests/integration/test_restart_recovery.py -q
```

Expected: all tests pass and checkpoint recovery retains the original snapshot.

- [ ] **Step 7: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/document.py feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py feishu-generation-agent/src/feishu_generation_agent/ports.py feishu-generation-agent/src/feishu_generation_agent/graph/state.py feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py feishu-generation-agent/src/feishu_generation_agent/bitable/production_service.py feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/tests/conftest.py feishu-generation-agent/tests/graph/test_approval_graph.py feishu-generation-agent/tests/unit/test_planner.py feishu-generation-agent/tests/integration/test_production_bitable_api.py
git commit -m "feat(agent): snapshot effective planner prompt per run"
```

---

## Task 9: Enforce Chinese Planning Before Paid Execution

**Files:**

- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Modify: `feishu-generation-agent/tests/unit/test_planner.py`
- Modify: `feishu-generation-agent/tests/graph/test_approval_graph.py`

- [ ] **Step 1: Write language validation tests**

Add tests proving:

- English-only `document_summary` produces a validation issue;
- English-only `task.user_intent` produces a validation issue;
- English-only `task.prompt` produces a validation issue;
- Chinese execution instructions containing an English dialogue such as `角色说：“Don't move.”` pass;
- brand names and literal UI text do not need translation;
- three failed structured-output attempts raise a validation error before generator methods are called.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_planner.py tests/graph/test_approval_graph.py -q
```

Expected: English-only plans currently validate.

- [ ] **Step 3: Add the immutable Chinese contract**

Define a separate constant for Portal execution constraints. It must state:

- summary, intent, prompt, negative constraints, assumptions, warnings, and exclusion reasons are Chinese-first;
- explicitly requested English dialogue, text, and brand names remain unchanged;
- output remains valid TaskPlan JSON;
- the user section cannot override the contract.

Do not concatenate this contract into the direct Prime path.

- [ ] **Step 4: Add deterministic CJK checks**

Implement:

```python
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

def _contains_cjk(value: str) -> bool:
    return bool(_CJK.search(value))
```

Extend `validate_plan()` to return field-specific Chinese issues for `document_summary`, `user_intent`, and `prompt`. The existing repair loop should receive those issues and request a narrow language correction.

- [ ] **Step 5: Verify the paid boundary**

In the graph test, use fake generators that count calls. Assert count remains zero when language validation exhausts all attempts.

- [ ] **Step 6: Run tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_planner.py tests/graph/test_approval_graph.py tests/graph/test_execution_graph.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py feishu-generation-agent/tests/unit/test_planner.py feishu-generation-agent/tests/graph/test_approval_graph.py
git commit -m "feat(agent): require chinese-first planning output"
```

---

## Task 10: Export and Parse Embedded Feishu Sheets

**Files:**

- Create: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_sheet_export.py`
- Create: `feishu-generation-agent/tests/unit/test_feishu_sheet_export.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py`

- [ ] **Step 1: Build a synthetic XLSX fixture in the test**

Use `zipfile.ZipFile` in `test_feishu_sheet_export.py` to create an in-memory XLSX containing:

- workbook and relationship files;
- one target worksheet ID `NuBUx5`;
- shared strings `镜头一` and `人物保持一致`;
- two image anchors referring to different media;
- a third anchor referring to the first media again.

Do not commit a 31.9 MB production XLSX or real client material.

- [ ] **Step 2: Write token parser tests**

Test:

```python
ref = parse_sheet_block_token(
    "C7tUs3k3fhoiybtWxzvcqN7Nn3b_NuBUx5"
)
assert ref.spreadsheet_token == "C7tUs3k3fhoiybtWxzvcqN7Nn3b"
assert ref.sheet_id == "NuBUx5"
```

Reject missing delimiter, empty spreadsheet token, empty sheet ID, path separators, and values above the chosen defensive length limit.

- [ ] **Step 3: Write XLSX extraction tests**

Assert:

- target-sheet shared string text is returned in row/column order;
- two unique images are returned;
- three anchors are preserved;
- duplicate media is content-hash deduplicated while source positions remain attached;
- path traversal ZIP members are rejected;
- missing workbook relationship, missing target sheet, malformed XML, oversized archive, too many media entries, and excessive uncompressed size produce safe document errors.

- [ ] **Step 4: Write Drive export polling tests**

Use `httpx.MockTransport` or a fake Feishu client to verify:

- POST `/open-apis/drive/v1/export_tasks` sends `file_extension=xlsx`, the spreadsheet token, and `type=sheet`;
- GET status polling stops at `job_status == 0`;
- `file_token` is downloaded from `/open-apis/drive/v1/export_tasks/file/{file_token}/download`;
- pending status sleeps between attempts;
- failure status and timeout raise bounded, non-secret errors.

- [ ] **Step 5: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_feishu_sheet_export.py -q
```

Expected: import failure for the new integration.

- [ ] **Step 6: Implement the exporter and parser**

Create:

```python
@dataclass(frozen=True, slots=True)
class EmbeddedSheetRef:
    spreadsheet_token: str
    sheet_id: str

@dataclass(frozen=True, slots=True)
class SheetImageAnchor:
    row: int
    column: int
    media_name: str
    sha256: str

@dataclass(frozen=True, slots=True)
class ExtractedSheetImage:
    media_name: str
    content: bytes
    sha256: str
    anchors: tuple[SheetImageAnchor, ...]

@dataclass(frozen=True, slots=True)
class ExtractedSheet:
    text_lines: tuple[str, ...]
    images: tuple[ExtractedSheetImage, ...]

def parse_sheet_block_token(raw: str) -> EmbeddedSheetRef

def extract_sheet_xlsx(
    content: bytes,
    *,
    target_sheet_id: str,
) -> ExtractedSheet

class FeishuSheetExporter:
    async def export(self, ref: EmbeddedSheetRef) -> ExtractedSheet
```

Use only `zipfile`, `posixpath`, `hashlib`, and `xml.etree.ElementTree`. Resolve workbook, worksheet, drawing, and media paths through relationship files instead of assuming `sheet1.xml` or `drawing1.xml`.

Set explicit limits for compressed bytes, total uncompressed bytes, entry count, media count, and polling duration. These constants must be unit-tested.

- [ ] **Step 7: Add binary export download to `FeishuClient`**

Add a method that obtains a tenant token through the existing private flow and downloads the export file as bytes. Reuse the current timeout and safe error mapping. Never write tenant tokens into exception strings.

- [ ] **Step 8: Run tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_feishu_sheet_export.py tests/unit/test_feishu_source.py tests/unit/test_feishu_delivery.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_sheet_export.py feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_client.py feishu-generation-agent/tests/unit/test_feishu_sheet_export.py
git commit -m "feat(agent): export and parse embedded feishu sheets"
```

---

## Task 11: Merge Embedded Sheet Text and Images into the Requirement Document

**Files:**

- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_source.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/bootstrap.py`
- Modify: `feishu-generation-agent/tests/unit/test_feishu_source.py`
- Modify: `feishu-generation-agent/tests/fixtures/feishu_docx_blocks.json`

- [ ] **Step 1: Extend the document fixture**

Add a Block 30 entry with token:

```text
C7tUs3k3fhoiybtWxzvcqN7Nn3b_NuBUx5
```

Keep it synthetic and do not add real production text or images.

- [ ] **Step 2: Write source integration tests**

Using a fake `FeishuSheetExporter`, assert:

- Block 30 becomes a `DocumentBlock` with `block_type="sheet"`;
- extracted sheet text is inserted at that block’s document order;
- each unique extracted image becomes a successful `MediaAsset`;
- `source_block_id` points to the Block 30 ID;
- `origin` distinguishes embedded-sheet assets from normal image Block 27 assets;
- asset IDs remain stable across two reads of the same content;
- duplicate anchors do not create duplicate paid visual-analysis calls;
- anchor coordinates appear in `text_view`;
- a total export failure adds a blocking `ingest_issue`;
- a single malformed image adds a visible issue while other sheet images remain available.

- [ ] **Step 3: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_feishu_source.py -q
```

Expected: Block 30 is ignored.

- [ ] **Step 4: Inject and call the sheet exporter**

Extend the source constructor with an optional `sheet_exporter`. In `bootstrap.py`, create it from the existing `FeishuClient`. For Block 30:

- parse the token;
- export only the referenced sheet;
- save image bytes with the existing `FileStore`;
- create stable IDs from document ID, sheet ID, first anchor, and content SHA-256;
- add all anchors to the readable text context;
- preserve document order.

Do not make a Sheets API request and do not add a new OAuth scope.

- [ ] **Step 5: Run source and vision tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_feishu_source.py tests/unit/test_vision.py tests/unit/test_planner.py -q
```

Expected: all tests pass; successful sheet images enter the same vision pipeline as normal images.

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/feishu_source.py feishu-generation-agent/src/feishu_generation_agent/bootstrap.py feishu-generation-agent/tests/unit/test_feishu_source.py feishu-generation-agent/tests/fixtures/feishu_docx_blocks.json
git commit -m "feat(agent): ingest embedded sheet assets from feishu docs"
```

---

## Task 12: Require Explicit Asset Coverage in Every Plan

**Files:**

- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/plan.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/review-state.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/app.js`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/index.html`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css`
- Modify: `feishu-generation-agent/tests/unit/test_domain.py`
- Modify: `feishu-generation-agent/tests/unit/test_planner.py`
- Modify: `feishu-generation-agent/tests/integration/test_api.py`
- Modify: `feishu-generation-agent/tests/frontend/review_state.test.cjs`

- [ ] **Step 1: Write domain tests**

Add:

```python
class ExcludedAsset(BaseModel):
    asset_id: str
    reason: str
```

Add `excluded_assets: list[ExcludedAsset]` to `TaskPlan` and test:

- every successful `MediaAsset` appears in at least one task reference or exclusion;
- referenced and excluded sets do not overlap;
- nonexistent and failed assets cannot be referenced;
- duplicate exclusions fail;
- exclusion reasons must contain Chinese;
- `approved_subset()` preserves the relevant exclusions needed to explain unused document assets.

- [ ] **Step 2: Write planner validation tests**

Build a document with three successful images:

- task references image 1 and 2;
- exclusion names image 3 with `“供应商最多支持两张参考图，保留主体与场景图。”`.

Assert the plan passes. Then separately omit image 3, overlap image 2, and use English-only exclusion text; assert field-specific issues.

- [ ] **Step 3: Write approval-edit API tests**

Test:

- adding an excluded asset to a task removes its exclusion;
- removing a referenced asset creates `“用户在审批中移除”`;
- replacing an asset updates both sets atomically;
- approving a plan with uncovered assets returns 422 and does not execute;
- uploaded local assets are included in the same reconciliation after the user attaches them.

- [ ] **Step 4: Write frontend coverage tests**

In `review_state.test.cjs`, assert:

- coverage label shows `已使用 2 / 共 3 张`;
- exclusions render thumbnail, asset ID, and Chinese reason;
- download/parse failures render a missing count;
- adding/removing references updates the displayed coverage immediately;
- approval is disabled when uncovered count is nonzero.

- [ ] **Step 5: Run and confirm failure**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_domain.py tests/unit/test_planner.py tests/integration/test_api.py -q
node --test tests/frontend/review_state.test.cjs
```

Expected: `excluded_assets` and coverage reconciliation are missing.

- [ ] **Step 6: Implement the domain and validation**

The immutable Portal contract must require `excluded_assets`. `validate_plan()` must derive successful source assets from `download_error is None` and check exact set coverage. Keep failed assets in missing-asset UI data but outside the coverable set.

For direct Prime compatibility, normalize a missing `excluded_assets` field to an empty list, but still validate coverage before approval. This preserves JSON loading while preventing incomplete paid execution.

- [ ] **Step 7: Reconcile user edits**

Centralize edit behavior in one server-side function:

```python
def reconcile_asset_coverage(
    plan: TaskPlan,
    *,
    added_asset_ids: set[str] = frozenset(),
    removed_asset_ids: set[str] = frozenset(),
) -> TaskPlan
```

Call it for add, remove, replace, and uploaded-reference endpoints. Do not rely only on frontend state.

- [ ] **Step 8: Render coverage**

Expose a coverage payload in the run view:

```json
{
  "successful_total": 3,
  "referenced_count": 2,
  "excluded_count": 1,
  "uncovered_count": 0,
  "failed_count": 0
}
```

Render per-task references, excluded assets with reasons, and failed-source count. Keep existing reference upload controls.

- [ ] **Step 9: Run tests**

Run:

```bash
cd feishu-generation-agent
uv run pytest tests/unit/test_domain.py tests/unit/test_planner.py tests/integration/test_api.py tests/graph/test_approval_graph.py -q
node --test tests/frontend/*.test.cjs
```

Expected: all tests pass.

- [ ] **Step 10: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/plan.py feishu-generation-agent/src/feishu_generation_agent/domain/__init__.py feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py feishu-generation-agent/src/feishu_generation_agent/graph/nodes.py feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py feishu-generation-agent/src/feishu_generation_agent/web/app.py feishu-generation-agent/src/feishu_generation_agent/web/static/review-state.js feishu-generation-agent/src/feishu_generation_agent/web/static/app.js feishu-generation-agent/src/feishu_generation_agent/web/static/index.html feishu-generation-agent/src/feishu_generation_agent/web/static/styles.css feishu-generation-agent/tests/unit/test_domain.py feishu-generation-agent/tests/unit/test_planner.py feishu-generation-agent/tests/integration/test_api.py feishu-generation-agent/tests/frontend/review_state.test.cjs
git commit -m "feat(agent): require explicit source asset coverage"
```

---

## Task 13: Full Regression, Real Read-Only Canary, and Configurable Deployment

**Files:**

- Modify only if failures require fixes in files already listed above.
- Record verification evidence in the implementation task response; do not commit credentials, production exports, runtime databases, or generated assets.

- [ ] **Step 1: Run the complete Agent test suite**

Run:

```bash
cd feishu-generation-agent
uv run pytest -q
node --test tests/frontend/*.test.cjs
```

Expected: all Python and frontend tests pass.

- [ ] **Step 2: Run the complete Portal test suite**

Run from repository root:

```bash
python3 -m unittest discover -s tests
```

Expected: all Portal and sub-application regression tests pass.

- [ ] **Step 3: Run static safety checks**

Run:

```bash
git diff --check
rg -n "192\\.168\\.30\\.5|localhost:8765|127\\.0\\.0\\.1:8765" portal feishu-generation-agent/src/feishu_generation_agent/web/static
rg -n "sk-[A-Za-z0-9]|AKLT|SecretAccessKey" portal feishu-generation-agent --glob '!*.sqlite3' --glob '!state/**'
```

Expected:

- `git diff --check` has no output;
- no hardcoded production Portal address appears in changed frontend files;
- no newly introduced credentials appear in tracked source.

- [ ] **Step 4: Start an isolated test deployment**

Use the documented test ports:

- Portal 9190;
- Redirect 9189;
- Agent test instance on an unused, explicitly configured port rather than 8765;
- existing production Agent and Portal remain untouched.

Verify the test Portal’s app registry points its Agent proxy to the configured test port through environment variables, not a code edit.

- [ ] **Step 5: Verify proxy and identity with two test users**

Through the test Portal:

- both users can open the Agent iframe;
- each sees only their own prompt;
- A’s prompt edit does not change B;
- A cannot fetch B’s run ID;
- direct test-Agent access remains Prime;
- restarting the test Portal does not stop the test Agent.

- [ ] **Step 6: Run the real production-table read-only canary**

Use the existing read-only smoke path and the configured production table. Do not claim, update, deliver, or modify production records.

Read the “魂穿” requirement and verify:

- Block 30 is detected;
- spreadsheet token and sheet ID parse successfully;
- XLSX export completes with current service credentials;
- 9 unique embedded images and 10 anchors are observed for the known document revision;
- normal Block 27 images remain present;
- no source image silently disappears;
- generated draft planning data is Chinese-first and shows complete coverage.

If the production document revision changed, record the new revision and compare counts manually; do not force the historical 9/10 counts against changed source content.

- [ ] **Step 7: Verify one non-paid planning run**

Stop before approval and paid generation. Confirm:

- effective prompt source/version/hash are visible in server diagnostics without full prompt text;
- all successful assets are referenced or excluded with Chinese reasons;
- the approval UI shows coverage and can edit references;
- no result is written to the production source table during planning.

- [ ] **Step 8: Check production activity before restart**

Query the existing Agent active runs and provider jobs. If any job is running, queued, polling, delivering, or awaiting a result write, do not restart. Wait for terminal completion or obtain explicit user approval to cancel.

- [ ] **Step 9: Deploy the Agent**

When no jobs are active:

1. restart `com.feishu-generation-agent`;
2. verify its process start time is newer than changed backend files;
3. verify 8765 is listening on loopback;
4. call direct `/api/health`;
5. verify direct UI is still Prime.

Do not modify the launchd plist unless the implementation actually requires a new environment variable. If it does, reload the plist rather than only kicking the old job.

- [ ] **Step 10: Deploy Portal without a fixed address**

Restart Portal through its configured service mechanism. Verify:

- the active Portal process uses the expected Python runtime;
- 9090/9089 or the deployment-configured alternatives are listening;
- the Agent appears as externally managed and healthy;
- the Portal did not kill or replace the Agent PID;
- the iframe works through the currently published Portal URL, whatever host or domain is active.

- [ ] **Step 11: Perform final client acceptance**

From another client browser:

- log in through the current Portal address;
- open the Agent;
- scan animation and portrait tabs;
- select one task and reach Chinese approval;
- inspect all source assets and exclusions;
- save/reset a personal prompt;
- ensure another user remains isolated;
- only after user approval, run one real task and verify the result reaches the shared result table.

- [ ] **Step 12: Final verification commit if fixes were needed**

If deployment verification required code fixes, rerun Tasks 13.1–13.3, inspect `git status --short`, stage only the named source and test files changed by that verified fix, and commit them with message `fix(agent): address portal deployment verification findings`.

If no fixes were needed, do not create an empty commit.

---

## Completion Checklist

- [ ] Portal registry contains five apps and marks only the Agent as unmanaged.
- [ ] Portal never starts, kills, cleans the port of, or restarts the Agent.
- [ ] Portal provides a server-derived stable user ID; browser-forged identity is overwritten.
- [ ] Direct 8765 access still uses the exact Prime hash `5dd2463a9bfddb3bc9e55c3a93148f7316f1259a6eaf70644a610b386a9c6ce4`.
- [ ] User prompt values and versions are isolated and durable.
- [ ] Every run stores an immutable effective-prompt snapshot.
- [ ] User-visible run APIs enforce ownership with 404.
- [ ] Shared production-task locking still prevents duplicate claims across users.
- [ ] Planning output is Chinese-first and preserves explicitly requested English content.
- [ ] Block 30 XLSX export works with existing permissions and no new client dependency.
- [ ] Embedded sheet images have stable IDs, positions, and visible failures.
- [ ] Every successful image is referenced or excluded with a Chinese reason.
- [ ] Portal and Agent URLs remain relative/configurable for future migration.
- [ ] Full automated tests, read-only production canary, and final client acceptance pass.
