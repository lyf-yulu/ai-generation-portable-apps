# Agent Reference-Aware Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep approval reference numbering contiguous after every edit and require DeepSeek plans to bind understood reference content to every Seedance storyboard shot.

**Architecture:** Add a small pure reference-contract module that owns canonical ordering, official Seedance tokens, safe token remapping, and deterministic prompt checks. The runtime applies canonical ordering before persisting approval drafts; the planner supplies the model with the official prompt contract and feeds deterministic failures into its existing three-attempt repair loop.

**Tech Stack:** Python 3.12, Pydantic 2, FastAPI, LangChain/DeepSeek structured output, LangGraph checkpoints, pytest, Node test runner.

## Global Constraints

- Do not add dependencies.
- Keep Portal personal planning prompts subordinate to the immutable application contract.
- Use `@图片N`, `@视频N`, and `@音频N`; never expose internal Asset IDs in generated prompts.
- Multi-shot plans use `镜头 1/2/3`, not absolute second ranges.
- Every referenced asset must have a concrete Chinese semantic description and appear in at least one relevant shot.
- Every multi-shot segment must contain an explicit reference token.
- Keep Banana image-to-image behavior and Seedance first/last-frame behavior compatible.
- Do not restart the Agent while a paid generation is active.

---

### Task 1: Canonical reference ordering and safe prompt remapping

**Files:**
- Create: `feishu-generation-agent/src/feishu_generation_agent/domain/reference_contract.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py`
- Test: `feishu-generation-agent/tests/unit/test_reference_contract.py`
- Test: `feishu-generation-agent/tests/integration/test_api.py`

**Interfaces:**
- Produces: `canonicalize_references(references: list[ImageReference]) -> list[ImageReference]`
- Produces: `reference_tokens(references: list[ImageReference], mime_types: Mapping[str, str]) -> dict[str, str]`
- Produces: `remap_prompt_references(prompt: str, old_references: list[ImageReference], new_references: list[ImageReference], mime_types: Mapping[str, str]) -> str`
- Consumes: `GenerationTask.prompt`, `ImageReference`, and current `MediaAsset.mime_type`.

- [ ] **Step 1: Write failing pure-function tests**

```python
def test_canonicalize_references_closes_middle_gap():
    result = canonicalize_references([
        ImageReference(asset_id="a", role="reference_image", order=1),
        ImageReference(asset_id="c", role="reference_image", order=3),
    ])
    assert [(item.asset_id, item.order) for item in result] == [("a", 1), ("c", 2)]


def test_remap_prompt_references_preserves_asset_identity():
    old = [_ref("a", 1), _ref("b", 2), _ref("c", 3)]
    new = [_ref("a", 1), _ref("c", 2)]
    prompt = "@图片1 中的锅，@图片2 中的碗，@图片3 中的桌面"
    assert remap_prompt_references(prompt, old, new, _IMAGE_MIMES) == (
        "@图片1 中的锅，碗，@图片2 中的桌面"
    )


def test_reference_tokens_number_each_media_type_separately():
    references = [_image("a", 1), _video("v", 2), _image("b", 3), _audio("x", 4)]
    assert reference_tokens(references, MIME_TYPES) == {
        "a": "@图片1", "v": "@视频1", "b": "@图片2", "x": "@音频1"
    }
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd feishu-generation-agent
./.venv/bin/python -m pytest -q tests/unit/test_reference_contract.py
```

Expected: collection/import failure because `reference_contract` does not exist.

- [ ] **Step 3: Implement the pure contract**

Implement:

```python
def canonicalize_references(references):
    ordered = sorted(references, key=lambda item: item.order)
    if len({item.order for item in ordered}) != len(ordered):
        raise ValueError("reference orders must be unique")
    return [
        item.model_copy(update={"order": index})
        for index, item in enumerate(ordered, start=1)
    ]
```

`reference_tokens` walks canonical total order but maintains separate counters for
image, video, and audio MIME types. `remap_prompt_references` first replaces old
tokens with collision-proof placeholders keyed by `asset_id`, removes only the
official reference prefix for deleted assets, then substitutes surviving placeholders
with their new tokens.

- [ ] **Step 4: Verify pure tests GREEN**

Run:

```bash
./.venv/bin/python -m pytest -q tests/unit/test_reference_contract.py
```

Expected: all tests pass.

- [ ] **Step 5: Write failing API regression for middle deletion**

Extend the existing reference mutation integration setup with three images, prompt
`@图片1 中的锅；@图片2 中的碗；@图片3 中的桌面`, delete the second asset, then assert:

```python
assert approval_task["reference_images"] == [
    {"asset_id": "asset-1", "role": "reference_image", "order": 1},
    {"asset_id": "asset-3", "role": "reference_image", "order": 2},
]
assert approval_task["prompt"] == "@图片1 中的锅；碗；@图片2 中的桌面"
```

- [ ] **Step 6: Run the API test and verify RED**

Run:

```bash
./.venv/bin/python -m pytest -q \
  tests/integration/test_api.py::test_unlink_reference_renumbers_survivors_and_prompt
```

Expected: FAIL showing survivor order `3` and stale `@图片3`.

- [ ] **Step 7: Apply canonicalization to every mutation**

In `GraphRuntime.add_reference`, `set_references`, and `unlink_reference`:

1. retain the pre-edit references;
2. reject duplicate/non-positive submitted orders through existing validation;
3. canonicalize the edited list;
4. compute current MIME types from `media_assets`;
5. update the task prompt through `remap_prompt_references`;
6. validate and persist the updated task atomically.

- [ ] **Step 8: Run reference mutation regression tests**

Run:

```bash
./.venv/bin/python -m pytest -q \
  tests/unit/test_reference_contract.py \
  tests/integration/test_api.py -k 'reference'
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/reference_contract.py \
  feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py \
  feishu-generation-agent/tests/unit/test_reference_contract.py \
  feishu-generation-agent/tests/integration/test_api.py
git commit -m "fix(agent): keep edited reference order canonical"
```

### Task 2: Deterministic Seedance prompt contract

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/domain/reference_contract.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Test: `feishu-generation-agent/tests/unit/test_reference_contract.py`
- Test: `feishu-generation-agent/tests/unit/test_planner.py`

**Interfaces:**
- Consumes: `reference_tokens(...)` from Task 1.
- Produces: `validate_seedance_prompt(task: Mapping[str, Any], assets: Mapping[str, MediaAsset], *, require_storyboard: bool) -> list[str]`
- Produces: deterministic error strings consumed unchanged by `DeepSeekPlanner._invoke_with_repair`.

- [ ] **Step 1: Write failing contract tests**

Add tests proving:

```python
issues = validate_seedance_prompt(
    hotpot_style_task(prompt=(
        "0-3秒：展示空锅。3-8秒：食材入锅。8-12秒：俯拍成品。"
    )),
    assets,
    require_storyboard=True,
)
assert any("@图片1" in issue for issue in issues)
assert any("镜头 1" in issue for issue in issues)
assert any("绝对秒数" in issue for issue in issues)
```

Also verify that a prompt which only lists all tokens in an opening paragraph but
does not use a token in each shot fails, while this passes:

```text
参考 @图片1 中的黄铜毛毡空锅，参考 @图片2 中的食材盘。
镜头 1：固定镜头，展示 @图片1 中的黄铜毛毡空锅。
镜头 2：近景，@图片2 中的毛毡食材依次落入 @图片1 中的锅。
高清，人物与物体稳定不变形，不要生成水印，不要生成 Logo。
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest -q \
  tests/unit/test_reference_contract.py \
  tests/unit/test_planner.py -k 'seedance_prompt or reference_order'
```

Expected: FAIL because semantic/shot validation is absent.

- [ ] **Step 3: Implement deterministic validation**

`validate_seedance_prompt` must:

1. sort and verify total orders are exactly `1…N`;
2. create MIME-specific official tokens;
3. reject `asset-*`, `[asset-*]`, or raw referenced asset IDs in prompt text;
4. require every token to be followed by a concrete phrase such as
   `中的黄铜毛毡空锅`, not merely appear in a mapping list;
5. when `require_storyboard=True`, split `镜头 N` sections and require each section
   to contain at least one official token;
6. require every referenced token to occur in at least one shot;
7. reject `0-3秒`, `3–8 秒`, and equivalent absolute ranges;
8. require stability wording plus `水印` and `Logo` constraints for multi-shot video.

- [ ] **Step 4: Connect validation to `validate_plan`**

When a detected storyboard table maps to one `image_to_video` task, call:

```python
issues.extend(
    validate_seedance_prompt(
        raw_task,
        assets,
        require_storyboard=True,
    )
)
```

For other `image_to_video` tasks call it with `require_storyboard=False`, requiring
semantic token coverage without imposing multi-shot structure. Do not call it for
`image_to_image`.

- [ ] **Step 5: Verify planner contract tests GREEN**

Run:

```bash
./.venv/bin/python -m pytest -q \
  tests/unit/test_reference_contract.py \
  tests/unit/test_planner.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/reference_contract.py \
  feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py \
  feishu-generation-agent/tests/unit/test_reference_contract.py \
  feishu-generation-agent/tests/unit/test_planner.py
git commit -m "feat(agent): validate Seedance reference-aware prompts"
```

### Task 3: Teach DeepSeek to understand and assign visual references

**Files:**
- Modify: `feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py`
- Test: `feishu-generation-agent/tests/unit/test_planner.py`

**Interfaces:**
- Consumes: current `NormalizedDocument`, all `VisionDescription` objects, and deterministic errors from Task 2.
- Produces: an immutable planning contract and user message that explain official Seedance syntax and semantic assignment.

- [ ] **Step 1: Write failing planner-message tests**

Assert the planning request contains all of:

```python
assert "@图片N" in prompt
assert "逐张读取" in prompt
assert "每个镜头" in prompt
assert "不得机械平均分配" in prompt
assert "镜头 1" in prompt
assert "禁止绝对秒数" in prompt
assert "画质" in prompt and "水印" in prompt and "Logo" in prompt
```

Add a repair-loop test where the first response is the existing invalid “火锅” Plan
and the second response has explicit understood bindings; assert the planner makes
two model calls and returns the repaired plan.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
./.venv/bin/python -m pytest -q tests/unit/test_planner.py \
  -k 'seedance_skill_contract or repairs_hotpot_reference_bindings'
```

Expected: FAIL because current prompts contain none of the official contract.

- [ ] **Step 3: Extend the immutable Portal planning contract**

Add concise rules derived from the supplied official SKILL:

```text
图生视频必须逐张读取视觉描述，把每个实际使用素材写成
“@图片N 中的具体主体/场景”“@视频N 中的动作/运镜”
或“@音频N 中的音色/声音”；禁止泛写“参考图片风格”。
多分镜使用镜头 1/2/3；每个镜头直接写出相关素材 token，
不得只在开头或末尾罗列素材；禁止绝对秒数。
```

Include the engineering quality/stability/no-watermark constraints, while preserving
the existing Chinese-output, audio-intent, first/last-frame, and personal-prompt
precedence rules.

- [ ] **Step 4: Enrich the planning user message**

For each vision entry include a compact Chinese-labelled payload containing:
`asset_id`, subjects, scene, style, composition, actions, probable role, and source
block. State the assignment priority:

```text
同一分镜行/Block > 同一章节路径 > 主体与动作语义匹配 > 场景和风格匹配
```

Require exclusions rather than fake use when no shot matches.

- [ ] **Step 5: Verify planner and repair tests GREEN**

Run:

```bash
./.venv/bin/python -m pytest -q tests/unit/test_planner.py
```

Expected: all tests pass, including two-call repair.

- [ ] **Step 6: Commit**

```bash
git add feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py \
  feishu-generation-agent/tests/unit/test_planner.py
git commit -m "feat(agent): plan from understood visual references"
```

### Task 4: Real-data regression, full verification, and safe deployment

**Files:**
- Modify only if a failing regression exposes a root cause in files already listed.
- Test data: read current “火锅” run through the local API; do not approve or trigger paid generation.

**Interfaces:**
- Consumes: deployed Agent API and current production checkpoint.
- Produces: evidence that canonical numbering and reference-aware repair work without paid generation.

- [ ] **Step 1: Run all Agent and Portal tests**

Run:

```bash
cd feishu-generation-agent
./.venv/bin/python -m pytest -q
node --test tests/frontend/*.test.cjs
cd ..
for pattern in test_app_spec_loader.py test_dispatch_via_spec.py test_portal_startup.py; do
  /opt/homebrew/bin/python3.12 -m unittest discover -s tests -p "$pattern" -q
done
git diff --check
```

Expected: all suites pass with no warnings attributable to this change.

- [ ] **Step 2: Run a read-only “火锅” fixture regression**

Feed the captured task shape and current vision descriptions into the deterministic
validator. Confirm the old prompt fails for missing reference tokens, per-shot binding,
absolute seconds, and stability constraints. Feed the corrected fixture and confirm
zero issues. This does not call Seedance.

- [ ] **Step 3: Check production safety**

Call `/api/bitable/active-runs` and inspect statuses. Do not restart if any run is in
provider submission, polling, artifact verification, or delivery.

- [ ] **Step 4: Restart only the Agent**

Run:

```bash
launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent
```

Verify the Agent PID changes, Portal PID does not, `/api/health` returns 200, and task
scanning still returns production tasks.

- [ ] **Step 5: Re-plan a safe real task without approval**

Use “火锅” rerun/analysis only if it does not trigger paid generation. Confirm in the
approval response:

- references are `1…N`;
- every effective image has a concrete Chinese semantic binding;
- every `镜头 N` contains at least one official token;
- no absolute second ranges or internal Asset IDs occur;
- prompt remains editable in the UI.

If any condition fails, keep the deployment in waiting approval, capture the exact
deterministic error, fix through a new failing test, and repeat Tasks 2–5.

- [ ] **Step 6: Commit any regression-only correction**

Only when Step 5 required a correction:

```bash
git add feishu-generation-agent/src/feishu_generation_agent/domain/reference_contract.py \
  feishu-generation-agent/src/feishu_generation_agent/graph/runtime.py \
  feishu-generation-agent/src/feishu_generation_agent/integrations/planner.py \
  feishu-generation-agent/tests/unit/test_reference_contract.py \
  feishu-generation-agent/tests/unit/test_planner.py \
  feishu-generation-agent/tests/integration/test_api.py
git commit -m "fix(agent): close real reference-planning regression"
```
