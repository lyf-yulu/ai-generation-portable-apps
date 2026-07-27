# Reference Mutation Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Portal 客户端中的参考素材替换和删除操作可靠触发，并在素材附近持续展示选择、处理中、成功或失败状态。

**Architecture:** 新增一个无依赖的参考素材操作状态模块，按任务和素材保存短期 UI 状态。前端以原生 `label[for]` 激活文件输入框，并用状态模块驱动行内反馈；现有服务端接口继续作为审批计划的唯一事实来源，操作成功后强制重新读取计划。

**Tech Stack:** 原生 JavaScript、原生 CSS、Node.js `node:test`、FastAPI 集成测试

## Global Constraints

- 不改变 Seedance 提交方式、参考模式规则、多用户隔离或 Portal 代理方式。
- 客户端继续保持零构建依赖。
- 替换不再调用隐藏文件输入框的 JavaScript `.click()`。
- 删除和替换期间必须阻止同一素材的重复请求。
- 失败原因必须显示在当前素材行；成功状态必须显示在当前参考素材区。
- 删除后剩余素材顺序必须连续，提示词引用必须同步重映射。
- 不自动批准或启动生成任务。

---

### Task 1: 参考素材操作状态

**Files:**
- Create: `src/feishu_generation_agent/web/static/reference-mutation-state.js`
- Create: `tests/frontend/reference_mutation_state.test.cjs`
- Modify: `src/feishu_generation_agent/web/static/index.html`

**Interfaces:**
- Produces: `ReferenceMutationState.createState(): State`
- Produces: `ReferenceMutationState.start(state, taskId, assetId, action, filename?): State`
- Produces: `ReferenceMutationState.succeed(state, taskId, assetId, message): State`
- Produces: `ReferenceMutationState.fail(state, taskId, assetId, message): State`
- Produces: `ReferenceMutationState.rowFeedback(state, taskId, assetId): Feedback | null`
- Produces: `ReferenceMutationState.taskFeedback(state, taskId): Feedback | null`
- Produces: `ReferenceMutationState.isBusy(state, taskId, assetId): boolean`

- [ ] **Step 1: Write failing state tests**

新测试必须覆盖两个用户可见过程：

```js
test("replacement exposes uploading and success feedback", () => {
  let state = ReferenceMutationState.createState();
  state = ReferenceMutationState.start(
    state, "task-1", "image-2", "replace", "new.png",
  );
  assert.deepEqual(
    ReferenceMutationState.rowFeedback(state, "task-1", "image-2"),
    { phase: "uploading", message: "正在替换 new.png…" },
  );
  assert.equal(
    ReferenceMutationState.isBusy(state, "task-1", "image-2"),
    true,
  );

  state = ReferenceMutationState.succeed(
    state, "task-1", "image-2", "参考素材已替换",
  );
  assert.deepEqual(
    ReferenceMutationState.taskFeedback(state, "task-1"),
    { phase: "success", message: "参考素材已替换" },
  );
});

test("deletion failure restores controls and exposes the local reason", () => {
  let state = ReferenceMutationState.createState();
  state = ReferenceMutationState.start(
    state, "task-1", "image-2", "delete",
  );
  state = ReferenceMutationState.fail(
    state, "task-1", "image-2", "至少保留一张参考素材",
  );

  assert.equal(
    ReferenceMutationState.isBusy(state, "task-1", "image-2"),
    false,
  );
  assert.deepEqual(
    ReferenceMutationState.rowFeedback(state, "task-1", "image-2"),
    { phase: "error", message: "至少保留一张参考素材" },
  );
});
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
node --test tests/frontend/reference_mutation_state.test.cjs
```

Expected: FAIL because `reference-mutation-state.js` does not exist.

- [ ] **Step 3: Implement immutable state transitions**

状态形状固定为：

```js
{
  rows: {
    "task-1::image-2": {
      taskId: "task-1",
      assetId: "image-2",
      action: "replace",
      phase: "uploading",
      message: "正在替换 new.png…",
    },
  },
  tasks: {
    "task-1": { phase: "success", message: "参考素材已替换" },
  },
}
```

`start` 清除当前任务旧的成功提示并设置 `uploading` 或 `deleting`；
`succeed` 清除行状态并设置任务级成功提示；`fail` 保留行并切换为 `error`。

- [ ] **Step 4: Load the state module**

在 `index.html` 中把脚本放在 `review-state.js` 之前：

```html
<script src="static/reference-mutation-state.js" defer></script>
```

直连与 Portal 代理都使用这份 `index.html`；Portal 会按现有规则转发该静态脚本，
无需修改后端页面渲染代码。

- [ ] **Step 5: Run focused tests to verify GREEN**

Run:

```bash
node --test tests/frontend/reference_mutation_state.test.cjs
.venv/bin/python -m pytest tests/unit/test_web_static.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/web/static/reference-mutation-state.js \
  src/feishu_generation_agent/web/static/index.html \
  tests/frontend/reference_mutation_state.test.cjs
git commit -m "feat(agent): track reference mutation feedback"
```

### Task 2: 原生替换控件与行内反馈

**Files:**
- Modify: `src/feishu_generation_agent/web/static/app.js`
- Modify: `src/feishu_generation_agent/web/static/styles.css`
- Create: `tests/frontend/reference_mutation_app.test.cjs`

**Interfaces:**
- Consumes: Task 1 的 `ReferenceMutationState`
- Produces: 每条素材的原生文件选择标签、行内状态和忙碌控制
- Produces: 每个参考素材区的任务级成功状态

- [ ] **Step 1: Write a failing app behavior test**

用现有 `FakeNode` 测试模式加载真实 `app.js`，返回一个 `waiting_approval`
运行。测试必须断言：

```js
assert.equal(replaceLabel.tagName, "LABEL");
assert.equal(replaceLabel.htmlFor, replaceInput.id);
assert.notEqual(replaceInput.hidden, true);

replaceInput.files = [{ name: "replacement.png", type: "image/png" }];
await replaceInput.dispatch("change");
assert.equal(
  requests.some((request) => (
    request.options.method === "POST"
    && request.url.endsWith("/api/runs/run-1/references")
  )),
  true,
);
assert.match(sectionFeedback.textContent, /参考素材已替换/);

await deleteButton.dispatch("click");
assert.equal(deleteButton.disabled, true);
assert.equal(deleteButton.textContent, "删除中…");
assert.equal(
  requests.some((request) => request.options.method === "DELETE"),
  true,
);
```

生产代码缺少原生标签和状态时，该测试必须失败。

- [ ] **Step 2: Run the app test to verify RED**

Run:

```bash
node --test tests/frontend/reference_mutation_app.test.cjs
```

Expected: FAIL because替换仍由按钮调用隐藏输入框，且没有行内操作状态。

- [ ] **Step 3: Implement native replacement selection**

在 `referenceRow()` 中：

- 为文件输入框设置稳定且唯一的 `id`。
- 移除 `replaceInput.hidden = true` 和 `replaceInput.click()`。
- 创建 `<label class="quiet-button reference-replace-label">替换</label>`，
  并把 `htmlFor` 指向文件输入框。
- 文件输入框使用 `reference-file-input` 类进行视觉隐藏，仍保持浏览器原生可激活。
- `change` 后调用 `ReferenceMutationState.start(...)`，立即更新行内状态并上传。

- [ ] **Step 4: Implement delete and shared feedback**

在每条素材行追加：

```html
<p class="reference-mutation-feedback" aria-live="polite"></p>
```

在参考素材区追加：

```html
<p class="reference-section-feedback" aria-live="polite"></p>
```

删除点击后先设置 `deleting` 状态、按钮文案“删除中…”并禁用该行按钮；
成功后设置任务级“已删除并重新编号”，然后强制 `poll(true, true)`；
失败后恢复按钮并显示 `errorMessage.textContent` 或捕获到的具体错误。

`prepareReferenceMutation()` 返回阻止状态时，把同一错误同时写入当前素材行，
而不是只调用页面顶部 `showError()`。

- [ ] **Step 5: Add accessible compact styles**

添加：

```css
.reference-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.reference-replace-label { display: inline-flex; align-items: center; cursor: pointer; }
.reference-replace-label.is-disabled { opacity: .48; pointer-events: none; }
.reference-mutation-feedback,
.reference-section-feedback { margin: .35rem 0 0; font-size: .76rem; }
.reference-mutation-feedback.is-error { color: var(--danger); }
.reference-section-feedback.is-success { color: var(--brand-dark); }
```

- [ ] **Step 6: Run focused tests to verify GREEN**

Run:

```bash
node --test tests/frontend/reference_mutation_app.test.cjs
node --test tests/frontend/reference_mutation_state.test.cjs
```

Expected: all selected tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/feishu_generation_agent/web/static/app.js \
  src/feishu_generation_agent/web/static/styles.css \
  tests/frontend/reference_mutation_app.test.cjs
git commit -m "fix(agent): make reference mutations visible"
```

### Task 3: 连续编号与全量回归

**Files:**
- Verify: `tests/integration/test_api.py`
- Verify: `tests/test_dispatch_via_spec.py`

**Interfaces:**
- Consumes: existing `GraphRuntime.unlink_reference`
- Verifies: `test_unlink_reference_renumbers_survivors_and_prompt`
- Verifies: Portal forwards bodyless `DELETE` and multipart `POST`

- [ ] **Step 1: Run the existing server renumbering regression**

Run:

```bash
.venv/bin/python -m pytest \
  tests/integration/test_api.py::test_unlink_reference_renumbers_survivors_and_prompt \
  -q
```

Expected: PASS with remaining references ordered `1, 2` and prompt changed from
`@图片3` to `@图片2`.

- [ ] **Step 2: Run Portal proxy regression**

Run from repository root:

```bash
/opt/homebrew/bin/python3.12 -m unittest \
  tests.test_dispatch_via_spec.ProxyIdentityHeadersTests \
  tests.test_dispatch_via_spec.ProxyHttpMethodDispatchTests \
  -v
```

Expected: PASS for identity forwarding、带请求体的 `PUT/PATCH/DELETE`、无请求体的
`DELETE` 和对应方法分发。

- [ ] **Step 3: Run all frontend and backend tests**

Run:

```bash
node --test tests/frontend/*.test.cjs
.venv/bin/python -m pytest -q
git diff --check
```

Expected: all tests PASS and `git diff --check` prints nothing.

- [ ] **Step 4: Browser acceptance**

通过 Portal 打开测试用户自己的待审批任务：

1. 点击替换，确认浏览器文件选择器打开。
2. 选择测试图片，确认行内依次显示选择与替换状态，完成后新预览出现。
3. 删除刚替换的测试素材，确认按钮显示“删除中…”，完成后素材行消失。
4. 确认剩余参考图编号连续，且没有批准或触发任何生成任务。

- [ ] **Step 5: Commit any verification-only documentation changes**

若执行过程中没有产生文档变更，此步骤不创建空提交。若记录了验收结果，只提交对应文档：

```bash
git add docs/superpowers/plans/2026-07-27-reference-mutation-feedback.md
git commit -m "docs(agent): record reference mutation verification"
```
