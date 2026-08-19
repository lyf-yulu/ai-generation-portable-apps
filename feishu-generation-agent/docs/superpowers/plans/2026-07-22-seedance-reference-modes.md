# Seedance 参考图模式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让图生视频任务自动选择合法的 Seedance 首尾帧或多参考图模式，并让用户能在审批页清晰切换与保存该选择。

**Architecture:** 将参考模式显式存入 `GenerationTask.reference_mode`，同时由领域模型在旧规划或模型输出混用了角色时规范化为多参考模式。运行时、规划校验和 Seedance 适配器共享同一模式规则；前端只暴露模式选择，不再让用户随意组合三个底层角色。

**Tech Stack:** Python 3.12、Pydantic v2、FastAPI、LangGraph、Node 内置测试运行器、原生浏览器 JavaScript。

## Global Constraints

- 生产需求表只读；本变更不得写入或修改源表。
- `first_last_frame` 仅允许一张 `first_frame` 加一张 `last_frame`，且不得有普通参考图。
- `multi_reference` 仅允许 `reference_image`；有额外参考图的首尾帧需求默认规范为该模式，并用提示词表达开场/结尾约束。
- 图生图始终使用 `multi_reference`。
- 保持现有 Seedance 官方接口字段和密钥处理方式，不新增依赖或前端构建步骤。

---

### Task 1: 领域模式与自动规范化

**Files:**

- Modify: `src/feishu_generation_agent/domain/plan.py:12-79`
- Test: `tests/unit/test_domain.py`

**Interfaces:**

- Produces: `ReferenceMode = Literal["multi_reference", "first_last_frame"]` 及 `GenerationTask.reference_mode`。
- Produces: 旧的混用角色计划被规范成 `multi_reference`，所有图片改为 `reference_image`，提示词保留开场或结尾意图。

- [x] **Step 1: Write the failing domain tests**

```python
def test_video_task_normalizes_mixed_frames_to_multi_reference():
    task = GenerationTask.model_validate(task_payload("image_to_video") | {
        "reference_images": [
            {"asset_id": "first", "role": "first_frame", "order": 1},
            {"asset_id": "style", "role": "reference_image", "order": 2},
        ],
    })
    assert task.reference_mode == "multi_reference"
    assert [item.role for item in task.reference_images] == ["reference_image", "reference_image"]
    assert "第 1 张参考图" in task.prompt

def test_video_task_keeps_exact_first_and_last_frames():
    task = GenerationTask.model_validate(task_payload("image_to_video") | {
        "reference_images": [
            {"asset_id": "first", "role": "first_frame", "order": 1},
            {"asset_id": "last", "role": "last_frame", "order": 2},
        ],
    })
    assert task.reference_mode == "first_last_frame"
```

- [x] **Step 2: Run RED**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/unit/test_domain.py -q`

Expected: FAIL because `reference_mode` does not exist and mixed roles remain unchanged.

- [x] **Step 3: Implement the minimal domain rule**

```python
ReferenceMode = Literal["multi_reference", "first_last_frame"]

class GenerationTask(BaseModel):
    reference_mode: ReferenceMode | None = None

    @model_validator(mode="after")
    def normalize_reference_mode(self) -> Self:
        # exact [first_frame, last_frame] -> first_last_frame
        # all other image-to-video shapes -> multi_reference
        # mixed legacy input -> all reference_image plus a Chinese prompt constraint
```

- [x] **Step 4: Run GREEN**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/unit/test_domain.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/domain/plan.py tests/unit/test_domain.py
git commit -m "feat(agent): normalize Seedance reference modes"
```

### Task 2: 规划、审批与 Seedance 的统一校验

**Files:**

- Modify: `src/feishu_generation_agent/integrations/planner.py:26-29,248-302`
- Modify: `src/feishu_generation_agent/graph/runtime.py:758-824`
- Modify: `src/feishu_generation_agent/integrations/seedance.py:637-668`
- Test: `tests/unit/test_planner.py`
- Test: `tests/graph/test_execution_graph.py`
- Test: `tests/unit/test_seedance.py`

**Interfaces:**

- Consumes: `GenerationTask.reference_mode` from Task 1.
- Produces: `validate_plan()` 的稳定模式错误；审批 API 保留 `RunValidationError` 的中文原因。

- [x] **Step 1: Write failing validation tests**

```python
def test_validator_rejects_frame_mode_without_two_frame_roles(narrative_document):
    raw_plan = json.loads(_plan_json(_video_task()))
    raw_plan["tasks"][0].update(reference_mode="first_last_frame")
    raw_plan["tasks"][0]["reference_images"] = [
        {"asset_id": "asset-1", "role": "first_frame", "order": 1},
    ]
    assert "首尾帧模式" in " ".join(validate_plan(raw_plan, narrative_document, 4))

def test_runtime_reports_mixed_reference_modes_in_chinese():
    with pytest.raises(RunValidationError, match="普通参考图不能与首尾帧混用"):
        GraphRuntime._validate_references(
            "image_to_video",
            [ImageReference(asset_id="one", role="first_frame", order=1),
             ImageReference(asset_id="two", role="reference_image", order=2)],
            {"one", "two"},
        )
```

- [x] **Step 2: Run RED**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/unit/test_planner.py tests/graph/test_execution_graph.py tests/unit/test_seedance.py -q`

Expected: FAIL because mode is not checked consistently and approval replaces detailed errors with “审批任务无效”.

- [x] **Step 3: Implement the shared rules**

```python
# planner system prompt: exactly two endpoint images and no extras -> first_last_frame;
# extra images -> multi_reference and state opening/ending intent in prompt.
# runtime/adapter: frame mode requires ordered first_frame,last_frame; multi mode
# requires only reference_image; image_to_image requires multi mode.
# _validate_decision: except RunValidationError: raise
```

- [x] **Step 4: Run GREEN**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/unit/test_planner.py tests/graph/test_execution_graph.py tests/unit/test_seedance.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/integrations/planner.py src/feishu_generation_agent/graph/runtime.py src/feishu_generation_agent/integrations/seedance.py tests/unit/test_planner.py tests/graph/test_execution_graph.py tests/unit/test_seedance.py
git commit -m "fix(agent): validate Seedance reference modes"
```

### Task 3: 审批页模式选择与持久化

**Files:**

- Modify: `src/feishu_generation_agent/web/static/review-state.js:1-240`
- Modify: `src/feishu_generation_agent/web/static/app.js:307-465`
- Modify: `src/feishu_generation_agent/web/schemas.py:64-67`
- Test: `tests/frontend/review_state.test.cjs`
- Test: `tests/integration/test_api.py:720-835`

**Interfaces:**

- Produces: `ReviewState.setReferenceMode(state, taskId, mode)`。
- Produces: PATCH `/references` 接收并持久化 `reference_mode`。

- [x] **Step 1: Write failing browser-state and API tests**

```javascript
test("switching to multi-reference converts every image", () => {
  let state = ReviewState.mergeServerView(ReviewState.createReviewState(), view());
  state = ReviewState.patchTask(state, "task-1", {
    reference_images: [
      { asset_id: "asset-1", role: "first_frame", order: 1 },
      { asset_id: "asset-2", role: "last_frame", order: 2 },
    ],
  });
  state = ReviewState.setReferenceMode(state, "task-1", "multi_reference");
  assert.equal(ReviewState.draftView(state).approval.tasks[0].reference_mode, "multi_reference");
  assert.deepEqual(ReviewState.draftView(state).approval.tasks[0].reference_images.map((item) => item.role), ["reference_image", "reference_image"]);
});
```

```python
async def test_reference_patch_persists_multi_reference_mode(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        run_id = await create_waiting_approval_run(client)
        response = await client.patch(
            f"/api/runs/{run_id}/tasks/task-1/references",
            json={"references": [{"asset_id": "asset-1", "role": "reference_image", "order": 1}], "reference_mode": "multi_reference"},
        )
        assert response.status_code == 200
        assert (await client.get(f"/api/runs/{run_id}")).json()["approval"]["tasks"][0]["reference_mode"] == "multi_reference"
```

- [x] **Step 2: Run RED**

Run: `node --test tests/frontend/review_state.test.cjs && /Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/integration/test_api.py -q`

Expected: FAIL because mode selection and request field do not yet exist.

- [x] **Step 3: Implement mode control**

```javascript
// Render a “参考模式” select with “多参考模式” and “首尾帧模式”.
// multi_reference hides individual role edits; frame mode assigns first/last by
// image order and tells the user that exactly two images are required.
// Saving sends both reference_images and reference_mode.
```

```python
class ReferenceListRequest(BaseModel):
    references: list[ImageReference] = Field(min_length=1)
    reference_mode: ReferenceMode | None = None
```

- [x] **Step 4: Run GREEN**

Run: `node --test tests/frontend/review_state.test.cjs && /Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/integration/test_api.py -q`

Expected: PASS; mode changes survive save/reload and invalid frame counts return a concrete 422.

- [x] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/web/static/review-state.js src/feishu_generation_agent/web/static/app.js src/feishu_generation_agent/web/schemas.py src/feishu_generation_agent/web/app.py tests/frontend/review_state.test.cjs tests/integration/test_api.py
git commit -m "feat(agent): add approval reference mode control"
```

### Task 4: 全量验证和本地交接

**Files:**

- Modify: `docs/superpowers/specs/2026-07-22-seedance-reference-modes-design.md` only if delivered behavior differs from the approved specification.

- [x] **Step 1: Run complete automated checks**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest -q && node --test tests/frontend/review_state.test.cjs`

Expected: Python and front-end suites pass.

- [x] **Step 2: Check the local CLI without contacting production**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/agent-smoke --help`

Expected: help output only; do not write to the production Bitable or submit a real generation.

- [x] **Step 3: Commit the completed plan and any necessary documentation update**

```bash
git add docs/superpowers/plans/2026-07-22-seedance-reference-modes.md docs/superpowers/specs/2026-07-22-seedance-reference-modes-design.md
git commit -m "docs(agent): record reference mode verification"
```
