# Production Task Category Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add animation and real-person task tabs that automatically scan only their configured Feishu view once per page session while preserving manual refresh, locking, approval, generation, rerun, and delivery behavior.

**Architecture:** The backend owns a fixed category-to-source mapping and accepts only `animation` or `portrait`; each source contains a configured Feishu view and its expected Chinese task type. The frontend keeps independent scan state for both tabs, automatically loads an idle tab on first entry, and sends the active category during scan and claim. Claims remain authoritative because the server rescans the selected category, validates the record type, and acquires the existing table/record lock.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, existing async Feishu client, vanilla JavaScript, Node.js built-in test runner, pytest.

## Global Constraints

- The production source table is read-only; do not create, update, or delete source records.
- `animation` maps to view `vewFRWT9ra` and exact task type `动画类`.
- `portrait` maps to view `vewvudainJ` and exact task type `真人类`.
- Automatic scanning happens only the first time each tab is entered during the current page session.
- Tab switching must not poll Feishu or trigger document ingestion, asset upload, image generation, or video generation.
- “刷新任务” reloads only the active category.
- Recent runs remain a single global list.
- Existing lock, approval, rerun, unified result-table delivery, and active-run recovery behavior must remain unchanged.
- Browser code remains dependency-free and requires no build step.
- Tests and live verification must not claim a production record or trigger a paid provider request.

---

### Task 1: Configure and model category-specific production sources

**Files:**
- Modify: `src/feishu_generation_agent/config.py`
- Modify: `src/feishu_generation_agent/integrations/bitable_url.py`
- Modify: `src/feishu_generation_agent/bitable/production_service.py`
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_production_service.py`
- Create: `tests/unit/test_bitable_url.py`

**Interfaces:**
- Produces: `Settings.lark_production_portrait_view_id: str | None`.
- Produces: `with_bitable_view(location: BitableLocation, view_id: str) -> BitableLocation`.
- Produces: `ProductionTaskSource(location: BitableLocation, expected_task_type: str)`.
- Produces: `ProductionBitableService.scan(category: str = "animation") -> list[ProductionTaskSummary]`.
- Produces: `ProductionBitableService.claim(record_id: str, category: str = "animation") -> str`.
- Preserves: the animation source as the default for existing callers that omit `category`.

- [ ] **Step 1: Write failing settings and URL-copy tests**

In `tests/unit/test_config.py`, prove the new setting is independently configurable:

```python
def test_production_portrait_view_is_separately_configurable() -> None:
    settings = Settings(
        _env_file=None,
        lark_production_portrait_view_id="vewPortrait",
    )
    assert settings.lark_production_portrait_view_id == "vewPortrait"
```

Create `tests/unit/test_bitable_url.py` and prove that changing a view preserves the wiki token, table, and other query parameters:

```python
from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.integrations.bitable_url import (
    with_bitable_view,
)


def test_with_bitable_view_preserves_table_and_replaces_view() -> None:
    source = BitableLocation(
        wiki_token="wikiProd",
        table_id="tblProd",
        view_id="vewAnimation",
        source_url=(
            "https://tenant.feishu.cn/wiki/wikiProd"
            "?table=tblProd&view=vewAnimation"
        ),
    )

    portrait = with_bitable_view(source, "vewPortrait")

    assert portrait.wiki_token == "wikiProd"
    assert portrait.table_id == "tblProd"
    assert portrait.view_id == "vewPortrait"
    assert "table=tblProd" in portrait.source_url
    assert "view=vewPortrait" in portrait.source_url
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_config.py::test_production_portrait_view_is_separately_configurable \
  tests/unit/test_bitable_url.py::test_with_bitable_view_preserves_table_and_replaces_view -q
```

Expected: failures because the setting and helper do not exist.

- [ ] **Step 3: Add the setting and view-copy helper**

Add the setting without making it part of the existing production capability requirement:

```python
lark_production_view_id: str | None = None
lark_production_portrait_view_id: str | None = None
```

Add a URL helper that returns a copy and never mutates the original:

```python
from urllib.parse import parse_qsl, urlencode


def with_bitable_view(
    location: BitableLocation, view_id: str
) -> BitableLocation:
    if not isinstance(view_id, str) or not view_id.strip():
        raise ValueError("多维表格 view_id 不能为空")
    parsed = urlsplit(location.source_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["table"] = location.table_id
    query["view"] = view_id.strip()
    source_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )
    return location.model_copy(
        update={"view_id": view_id.strip(), "source_url": source_url}
    )
```

- [ ] **Step 4: Run the settings and URL tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_config.py tests/unit/test_bitable_url.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Write failing service tests for isolated scans and claims**

Add these complete fixtures above the new tests:

```python
from feishu_generation_agent.bitable.production_service import (
    ProductionBitableService,
    ProductionTaskSource,
)
from feishu_generation_agent.storage.production_tasks import (
    ProductionTaskStore,
)


def _portrait_task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="rec-portrait",
        display_text="真人需求",
        source_url="https://tenant.feishu.cn/docx/docPortrait",
        progress="未开始",
        task_type="真人类",
        snapshot=ProductionSourceSnapshot(
            requirement_name="真人需求",
            task_type="真人类",
            requirement_attachment="https://tenant.feishu.cn/docx/docPortrait",
        ),
    )


def _category_sources() -> dict[str, ProductionTaskSource]:
    return {
        "animation": ProductionTaskSource(
            _location().model_copy(update={"view_id": "vewAnimation"}),
            "动画类",
        ),
        "portrait": ProductionTaskSource(
            _location().model_copy(update={"view_id": "vewPortrait"}),
            "真人类",
        ),
    }


class _MixedCategoryBitable:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_schema(self, location):
        return object()

    async def list_tasks(
        self, location, schema, *, include_completed
    ):
        self.calls.append(location.view_id)
        return [_task(), _portrait_task()]


class _Runtime:
    async def start_run(
        self, request, *, run_id=None, thread_id=None
    ):
        return run_id


async def _production_service(
    tmp_path,
    *,
    bitable,
    sources,
    enabled_task_types=frozenset({"动画类", "真人类"}),
):
    store = await ProductionTaskStore.open(
        tmp_path / "production.sqlite3"
    )
    service = ProductionBitableService(
        bitable=bitable,
        store=store,
        runtime=_Runtime(),
        sources=sources,
        include_completed_for_test=False,
        enabled_task_types=enabled_task_types,
    )
    return service, store
```

Add a test that records the requested view, returns mixed task types, and verifies exact filtering:

```python
async def test_service_scans_only_the_requested_category_and_exact_type(
    tmp_path,
) -> None:
    bitable = _MixedCategoryBitable()
    service, store = await _production_service(
        tmp_path,
        bitable=bitable,
        sources=_category_sources(),
    )
    try:
        animation_tasks = await service.scan("animation")
        portrait_tasks = await service.scan("portrait")
    finally:
        await store.close()

    assert [task.task_type for task in animation_tasks] == ["动画类"]
    assert [task.task_type for task in portrait_tasks] == ["真人类"]
    assert bitable.calls == ["vewAnimation", "vewPortrait"]
```

Add a claim test that verifies the stored source location is the selected portrait view:

```python
async def test_portrait_claim_uses_the_portrait_source_location(tmp_path) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources=_category_sources(),
        enabled_task_types=frozenset({"动画类", "真人类"}),
    )
    try:
        run_id = await service.claim("rec-portrait", "portrait")
        binding = await store.get_by_run(run_id)
    finally:
        await store.close()

    assert binding is not None
    assert binding.source_location.view_id == "vewPortrait"
    assert binding.snapshot.task_type == "真人类"
```

- [ ] **Step 6: Add missing-category failure coverage**

```python
async def test_service_reports_an_unconfigured_portrait_source(
    tmp_path,
) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources={"animation": _category_sources()["animation"]},
    )
    try:
        with pytest.raises(
            RunValidationError,
            match="真人类视图尚未配置",
        ):
            await service.scan("portrait")
    finally:
        await store.close()
```

- [ ] **Step 7: Run the service tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_production_service.py -q
```

Expected: failures because the service still accepts one location and has no category source model.

- [ ] **Step 8: Implement the source map and exact server-side filtering**

Add the focused source value object:

```python
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class ProductionTaskSource:
    location: BitableLocation
    expected_task_type: str
```

Replace the service’s single `location` with a copied source map and schema cache:

```python
self._sources = dict(sources)
self._schemas: dict[tuple[str, str], Any] = {}
```

Implement category-aware scan and claim:

```python
async def scan(self, category: str = "animation"):
    source = await self._prepared_source(category)
    schema_key = (
        source.location.app_token or "",
        source.location.table_id,
    )
    schema = self._schemas.get(schema_key)
    if schema is None:
        schema = await self._bitable.ensure_schema(source.location)
        self._schemas[schema_key] = schema
    tasks = await self._bitable.list_tasks(
        source.location,
        schema,
        include_completed=self._include_completed_for_test,
    )
    active_record_ids = {
        binding.record_id
        for binding in await self._store.list_active(*schema_key)
    }
    return [
        task
        for task in tasks
        if task.task_type == source.expected_task_type
        and task.record_id not in active_record_ids
    ]


async def claim(
    self, record_id: str, category: str = "animation"
) -> str:
    source = await self._prepared_source(category)
    task = next(
        (
            item
            for item in await self.scan(category)
            if item.record_id == record_id
        ),
        None,
    )
    if task is None:
        raise RunConflict("该生产表记录当前不可领取")
    if task.task_type not in self._enabled_task_types:
        raise RunConflict(f"{task.task_type or '未分类'}任务暂未启用")
    binding = await self._store.claim(
        source.location,
        task,
        run_id=str(uuid4()),
        thread_id=str(uuid4()),
    )
    return await self._runtime.start_run(
        RequirementRequest(
            source_url=binding.source_url,
            trigger_type="production_bitable",
        ),
        run_id=binding.run_id,
        thread_id=binding.thread_id,
    )
```

Resolve only known sources and use animation as the shared table identity for active/recent queries:

```python
async def _prepared_source(self, category: str) -> ProductionTaskSource:
    source = self._sources.get(category)
    if source is None:
        label = "真人类" if category == "portrait" else "动画类"
        raise RunValidationError(f"{label}视图尚未配置")
    if source.location.app_token is None:
        resolved = await self._bitable.resolve_location(source.location)
        source = ProductionTaskSource(resolved, source.expected_task_type)
        self._sources[category] = source
    return source
```

Use the animation source only as the shared app/table identity for global run lists:

```python
async def _table_location(self) -> BitableLocation:
    return (await self._prepared_source("animation")).location


async def active_runs(self):
    location = await self._table_location()
    return await self._store.list_active(
        location.app_token or "", location.table_id
    )


async def recent_runs(self):
    location = await self._table_location()
    return await self._store.list_recent(
        location.app_token or "", location.table_id
    )
```

When rerunning, call `store.claim(source.source_location, task, ...)` so a portrait binding retains its original view. Remove the obsolete `_prepared_location()` and single `_location` state only after all call sites use `_prepared_source()` or `_table_location()`.

- [ ] **Step 9: Build both sources in bootstrap**

Change `ProductionBitableServiceFactory` to carry:

```python
sources: dict[str, ProductionTaskSource]
```

Its `create()` method must forward the same map:

```python
return ProductionBitableService(
    bitable=self.bitable,
    store=self.store,
    runtime=runtime,
    sources=self.sources,
    include_completed_for_test=self.include_completed_for_test,
    enabled_task_types=self.enabled_task_types,
)
```

Build animation unconditionally and portrait only when configured:

```python
production_sources = {
    "animation": ProductionTaskSource(
        production_location,
        expected_task_type="动画类",
    )
}
if settings.lark_production_portrait_view_id:
    production_sources["portrait"] = ProductionTaskSource(
        with_bitable_view(
            production_location,
            settings.lark_production_portrait_view_id,
        ),
        expected_task_type="真人类",
    )
```

Pass `production_sources` through the factory into `ProductionBitableService`.

- [ ] **Step 10: Run focused backend tests and commit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_config.py \
  tests/unit/test_bitable_url.py \
  tests/unit/test_production_service.py -q
```

Expected: all focused tests pass.

Commit:

```bash
git add \
  src/feishu_generation_agent/config.py \
  src/feishu_generation_agent/integrations/bitable_url.py \
  src/feishu_generation_agent/bitable/production_service.py \
  src/feishu_generation_agent/bootstrap.py \
  tests/unit/test_config.py \
  tests/unit/test_bitable_url.py \
  tests/unit/test_production_service.py
git commit -m "feat(agent): isolate production task category sources"
```

---

### Task 2: Route scan and claim requests by a validated category

**Files:**
- Modify: `src/feishu_generation_agent/web/app.py`
- Modify: `src/feishu_generation_agent/bitable/mvp_service.py`
- Modify: `tests/integration/test_production_bitable_api.py`
- Modify: `tests/integration/test_bitable_mvp_api.py`

**Interfaces:**
- Consumes: `ProductionBitableService.scan(category)` and `claim(record_id, category)`.
- Produces: `GET /api/bitable/tasks?category=animation|portrait`.
- Produces: `POST /api/bitable/tasks/{record_id}/claim?category=animation|portrait`.
- Preserves: missing `category` defaults to `animation`.

- [ ] **Step 1: Write failing API routing tests**

Update the production fake service to record categories:

```python
self.scan_categories: list[str] = []
self.claim_categories: list[tuple[str, str]] = []

async def scan(self, category: str = "animation"):
    self.scan_categories.append(category)
    return self.tasks_by_category[category]

async def claim(
    self, record_id: str, category: str = "animation"
) -> str:
    self.claim_categories.append((record_id, category))
    return "run-no-maker"
```

Add API assertions:

```python
async def test_api_routes_scan_and_claim_to_portrait_category(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    service = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get(
            "/api/bitable/tasks", params={"category": "portrait"}
        )
        claimed = await client.post(
            "/api/bitable/tasks/rec-portrait/claim",
            params={"category": "portrait"},
        )

    assert scanned.status_code == 200
    assert scanned.json()[0]["task_type"] == "真人类"
    assert claimed.status_code == 202
    assert service.scan_categories == ["portrait"]
    assert service.claim_categories == [("rec-portrait", "portrait")]
```

Add rejection coverage:

```python
async def test_api_rejects_unknown_production_category(tmp_path) -> None:
    app = create_app(
        runtime=_Runtime(tmp_path),
        bitable_service=_ProductionService(),
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/tasks", params={"category": "other"}
        )

    assert response.status_code == 422
```

- [ ] **Step 2: Run the API tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/integration/test_production_bitable_api.py -q
```

Expected: the endpoint ignores `category`, so portrait routing assertions fail.

- [ ] **Step 3: Add validated query parameters**

Import `Literal` and update the two routes:

```python
ProductionCategory = Literal["animation", "portrait"]


@app.get("/api/bitable/tasks")
async def scan_bitable_tasks(
    request: Request,
    category: ProductionCategory = "animation",
) -> list[dict]:
    active = get_bitable_service(request)
    try:
        tasks = await active.scan(category)
    except Exception as exc:
        raise_bitable_error(exc)
    return [_task_payload(task) for task in tasks]
```

```python
async def claim_bitable_task(
    record_id: str,
    request: Request,
    category: ProductionCategory = "animation",
) -> BitableClaimResponse:
    active = get_bitable_service(request)
    try:
        run_id = await active.claim(record_id, category)
    except Exception as exc:
        raise_bitable_error(exc)
    return BitableClaimResponse(run_id=run_id)
```

Update `BitableMvpService.scan` and `claim` to accept the optional category argument while retaining their current behavior:

```python
async def scan(self, category: str = "animation"):
    del category
    # existing scan body


async def claim(
    self, record_id: str, category: str = "animation"
) -> str:
    del category
    # existing claim body
```

Update integration fakes to accept the defaulted parameter so legacy API tests continue exercising the same behavior.

Map a missing configured category to a safe, explicit validation response before the generic read error:

```python
if isinstance(exc, RunValidationError):
    raise HTTPException(status_code=422, detail=str(exc)) from None
```

- [ ] **Step 4: Run API regression tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/integration/test_production_bitable_api.py \
  tests/integration/test_bitable_mvp_api.py -q
```

Expected: all tests pass, including calls without `category`.

- [ ] **Step 5: Commit the API routing**

```bash
git add \
  src/feishu_generation_agent/web/app.py \
  src/feishu_generation_agent/bitable/mvp_service.py \
  tests/integration/test_production_bitable_api.py \
  tests/integration/test_bitable_mvp_api.py
git commit -m "feat(agent): route production scans by category"
```

---

### Task 3: Add independently cached animation and portrait tabs

**Files:**
- Modify: `src/feishu_generation_agent/web/static/bitable-state.js`
- Modify: `src/feishu_generation_agent/web/static/index.html`
- Modify: `src/feishu_generation_agent/web/static/styles.css`
- Modify: `src/feishu_generation_agent/web/static/app.js`
- Modify: `tests/frontend/bitable_state.test.cjs`

**Interfaces:**
- Produces: `BitableState.selectCategory(state, category)`.
- Produces: `BitableState.activeCategoryState(state)`.
- Changes: scan transitions take `category`.
- Changes: claim state records the category used for removal.
- Consumes: scan and claim query parameters from Task 2.

- [ ] **Step 1: Write failing state tests for tab isolation**

Add:

```javascript
test("category tabs keep independent scan results", () => {
  let state = BitableState.createState();
  state = BitableState.scanStarted(state, "animation");
  state = BitableState.scanSucceeded(state, "animation", [
    { record_id: "rec-animation", task_type: "动画类" },
  ]);
  state = BitableState.selectCategory(state, "portrait");

  assert.equal(state.activeCategory, "portrait");
  assert.equal(BitableState.activeCategoryState(state).scan.phase, "idle");

  state = BitableState.scanStarted(state, "portrait");
  state = BitableState.scanSucceeded(state, "portrait", [
    { record_id: "rec-portrait", task_type: "真人类" },
  ]);
  state = BitableState.selectCategory(state, "animation");

  assert.deepEqual(
    BitableState.activeCategoryState(state).tasks.map((task) => task.record_id),
    ["rec-animation"],
  );
});

test("claim success removes a task only from its category", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(
    state,
    "portrait",
    [{ record_id: "rec-portrait" }],
  );
  state = BitableState.claimStarted(state, "rec-portrait", "portrait");
  state = BitableState.claimSucceeded(state, "run-portrait");

  assert.deepEqual(state.categories.portrait.tasks, []);
  assert.deepEqual(state.categories.animation.tasks, []);
  assert.equal(state.claim.category, "portrait");
});

test("a portrait scan failure does not clear animation results", () => {
  let state = BitableState.createState();
  state = BitableState.scanSucceeded(
    state,
    "animation",
    [{ record_id: "rec-animation" }],
  );
  state = BitableState.scanFailed(state, "portrait", "真人视图读取失败");

  assert.deepEqual(
    state.categories.animation.tasks,
    [{ record_id: "rec-animation" }],
  );
  assert.equal(state.categories.animation.scan.phase, "ready");
  assert.equal(state.categories.portrait.scan.phase, "error");
  assert.equal(
    state.categories.portrait.scan.error,
    "真人视图读取失败",
  );
});
```

- [ ] **Step 2: Run the state tests and verify they fail**

Run:

```bash
node --test tests/frontend/bitable_state.test.cjs
```

Expected: failures because the state currently contains only one task list.

- [ ] **Step 3: Implement category state**

Use this shape:

```javascript
const CATEGORY_NAMES = new Set(["animation", "portrait"]);

function createCategoryState() {
  return {
    tasks: [],
    scan: { phase: "idle", error: "" },
  };
}

function createState() {
  return {
    activeCategory: "animation",
    categories: {
      animation: createCategoryState(),
      portrait: createCategoryState(),
    },
    claim: {
      phase: "idle",
      recordId: null,
      runId: null,
      category: null,
      error: "",
    },
    deliveryRetry: { phase: "idle", runId: null, error: "" },
    recentRuns: [],
  };
}

function selectCategory(state, category) {
  if (!CATEGORY_NAMES.has(category)) throw new Error("未知任务类别");
  return { ...state, activeCategory: category };
}

function activeCategoryState(state) {
  return state.categories[state.activeCategory];
}
```

Make scan transitions replace only `categories[category]`. Make `claimStarted` store `category`, and make `claimSucceeded` filter only that category’s task list. Keep recent runs, delivery retry, and reset-run behavior global.

- [ ] **Step 4: Add semantic tab controls**

Place this between the panel heading and status:

```html
<div class="task-category-tabs" role="tablist" aria-label="任务类别">
  <button
    id="animation-category-tab"
    class="task-category-tab is-active"
    type="button"
    role="tab"
    aria-selected="true"
    data-category="animation"
  >动画类</button>
  <button
    id="portrait-category-tab"
    class="task-category-tab"
    type="button"
    role="tab"
    aria-selected="false"
    data-category="portrait"
  >真人类</button>
</div>
```

Rename the scan button text to `刷新任务`. Add lightweight active, focus, and disabled styles using the existing color variables:

```css
.task-category-tabs {
  display: flex;
  gap: .4rem;
  margin-top: .9rem;
  border-bottom: 1px solid var(--line);
}
.task-category-tab {
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  padding: .55rem .75rem;
  background: transparent;
  color: var(--muted);
}
.task-category-tab.is-active {
  border-bottom-color: var(--brand);
  color: var(--brand);
}
```

- [ ] **Step 5: Route frontend rendering, scan, and claim through the active tab**

Read active state once per render:

```javascript
const categoryState = BitableState.activeCategoryState(state.bitable);
const scan = categoryState.scan;
const tasks = categoryState.tasks;
```

Snapshot the category before an async scan so a response cannot overwrite a tab selected later:

```javascript
async function scanBitableTasks() {
  if (state.busy || !state.modes.bitable) return;
  const category = state.bitable.activeCategory;
  state.bitable = BitableState.scanStarted(state.bitable, category);
  renderBitableTasks();
  setBusy(true);
  clearError();
  try {
    const tasks = await api(
      `/api/bitable/tasks?category=${encodeURIComponent(category)}`
    );
    state.bitable = BitableState.scanSucceeded(
      state.bitable, category, tasks
    );
  } catch (error) {
    state.bitable = BitableState.scanFailed(
      state.bitable, category, error.message
    );
    showError(error);
  } finally {
    setBusy(false);
    renderBitableTasks();
  }
}
```

Send the same category when claiming:

```javascript
const category = state.bitable.activeCategory;
state.bitable = BitableState.claimStarted(
  state.bitable, recordId, category
);
const created = await api(
  `/api/bitable/tasks/${encodeURIComponent(recordId)}/claim`
    + `?category=${encodeURIComponent(category)}`,
  { method: "POST" },
);
```

Add tab selection with first-entry auto-read:

```javascript
async function selectBitableCategory(category) {
  if (state.busy || category === state.bitable.activeCategory) return;
  state.bitable = BitableState.selectCategory(state.bitable, category);
  renderBitableTasks();
  if (BitableState.activeCategoryState(state.bitable).scan.phase === "idle") {
    await scanBitableTasks();
  }
}
```

Update `aria-selected` and `is-active` during render. Bind both tab buttons. At the end of successful `configureModes()`, call `scanBitableTasks()` if the default animation tab is still idle. Keep `resetForNextTask()` refreshing only the currently active tab.

- [ ] **Step 6: Update static markup assertions and run frontend tests**

Extend the existing production-only HTML test:

```javascript
assert.equal(html.includes('id="animation-category-tab"'), true);
assert.equal(html.includes('id="portrait-category-tab"'), true);
assert.equal(html.includes(">刷新任务<"), true);
```

Run:

```bash
node --test tests/frontend/*.test.cjs
```

Expected: all frontend tests pass.

- [ ] **Step 7: Commit the tab UI**

```bash
git add \
  src/feishu_generation_agent/web/static/bitable-state.js \
  src/feishu_generation_agent/web/static/index.html \
  src/feishu_generation_agent/web/static/styles.css \
  src/feishu_generation_agent/web/static/app.js \
  tests/frontend/bitable_state.test.cjs
git commit -m "feat(agent): add production category task tabs"
```

---

### Task 4: Document, configure, and verify the production behavior

**Files:**
- Modify: `.env.example`
- Modify: `docs/production-bitable-activation.md`
- Local-only modify: `.env`

**Interfaces:**
- Consumes: `LARK_PRODUCTION_PORTRAIT_VIEW_ID`.
- Verifies: both category endpoints are read-only and return exact matching task types.

- [ ] **Step 1: Document both views and current read behavior**

Add:

```dotenv
LARK_PRODUCTION_VIEW_ID=
LARK_PRODUCTION_PORTRAIT_VIEW_ID=
```

Update the activation guide to state:

```markdown
- `LARK_PRODUCTION_VIEW_ID` 配置动画类视图。
- `LARK_PRODUCTION_PORTRAIT_VIEW_ID` 配置真人类视图。
- 页面首次进入某个类别时自动读取一次；来回切换使用页面缓存，点击“刷新任务”才重新读取当前类别。
- 自动读取不领取任务、不读取需求文档，也不触发生成。
```

- [ ] **Step 2: Run the full automated test suite**

Run:

```bash
.venv/bin/python -m pytest -q
node --test tests/frontend/*.test.cjs
```

Expected: all Python and frontend tests pass.

- [ ] **Step 3: Add the verified portrait view to local configuration**

Add this non-secret local setting to `.env` without printing or changing any credentials:

```dotenv
LARK_PRODUCTION_PORTRAIT_VIEW_ID=vewvudainJ
```

Confirm `.env` remains ignored:

```bash
git check-ignore .env
```

Expected: `.env`.

- [ ] **Step 4: Restart the local service without starting paid work**

Before restart, query active runs and record their IDs. Restart the launchd service:

```bash
launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent
```

Verify port `8765` is listening and health returns successfully. Confirm any prior active run is recovered before continuing.

- [ ] **Step 5: Perform read-only production endpoint verification**

Run:

```bash
curl -fsS "http://127.0.0.1:8765/api/bitable/tasks?category=animation"
curl -fsS "http://127.0.0.1:8765/api/bitable/tasks?category=portrait"
```

Verify:

- every animation response item has `task_type` equal to `动画类`;
- every portrait response item has `task_type` equal to `真人类`;
- neither request creates a new active run;
- neither request writes to the source or result table.

- [ ] **Step 6: Verify browser interaction**

Open `http://127.0.0.1:8765/` and confirm:

- animation loads automatically on first page entry;
- first switch to portrait loads portrait tasks;
- switching back and forth preserves both lists without another request;
- “刷新任务” reloads only the active tab;
- starting a task is not performed during this verification.

- [ ] **Step 7: Commit documentation and inspect final scope**

```bash
git add .env.example docs/production-bitable-activation.md
git commit -m "docs(agent): document production category views"
git status --short
git log -4 --oneline
```

Expected: only pre-existing unrelated untracked files remain; `.env` is not staged.
