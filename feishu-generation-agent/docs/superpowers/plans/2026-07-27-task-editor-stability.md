# Task Editor Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让审批页的提示词和负面约束编辑框在每秒轮询期间保持滚动位置、焦点和用户拖拽高度，并提供更合适的默认尺寸。

**Architecture:** 保留现有轮询频率，将任务编辑区是否重建变为由审批计划标识决定。`ReviewState` 提供纯函数判断是否需要刷新编辑区，`app.js` 仅在首次渲染或服务端计划真正改变时替换任务节点；CSS 为两个编辑框提供独立尺寸与原生滚动/拖拽能力。

**Tech Stack:** 原生 JavaScript、原生 CSS、Node.js `node:test`

## Global Constraints

- 不改变后端接口、审批数据格式、轮询频率或多用户隔离逻辑。
- 相同审批计划的普通轮询不得替换现有编辑框。
- 服务端计划真正变化时仍须刷新；本地草稿冲突时不得覆盖编辑区。
- 客户端继续保持零构建依赖。

---

### Task 1: 阻止相同计划轮询重建编辑区

**Files:**
- Modify: `src/feishu_generation_agent/web/static/review-state.js`
- Modify: `src/feishu_generation_agent/web/static/app.js`
- Test: `tests/frontend/review_state.test.cjs`

**Interfaces:**
- Consumes: `ReviewState.mergeServerView(state, view)`
- Produces: `ReviewState.shouldRefreshTaskEditor(previousState, nextState, hasRenderedTasks): boolean`
- Produces: `render(view, options)`，其中 `options.refreshTasks` 默认为 `true`

- [ ] **Step 1: Write the failing tests**

在 `review_state.test.cjs` 中验证：

```js
test("task editor refreshes only when the effective server plan changes", () => {
  const empty = ReviewState.createReviewState();
  const initial = ReviewState.mergeServerView(empty, view());
  const same = ReviewState.mergeServerView(initial, view());
  const changed = ReviewState.mergeServerView(
    same,
    view({ revision: 8, taskOnePrompt: "new server prompt" }),
  );

  assert.equal(
    ReviewState.shouldRefreshTaskEditor(empty, initial, false),
    true,
  );
  assert.equal(
    ReviewState.shouldRefreshTaskEditor(initial, same, true),
    false,
  );
  assert.equal(
    ReviewState.shouldRefreshTaskEditor(same, changed, true),
    true,
  );
});

test("dirty conflict does not replace the current task editor", () => {
  let current = ReviewState.mergeServerView(
    ReviewState.createReviewState(),
    view(),
  );
  current = ReviewState.patchTask(current, "task-1", { prompt: "local edit" });
  const conflicted = ReviewState.mergeServerView(
    current,
    view({ revision: 8, taskOnePrompt: "server edit" }),
  );

  assert.equal(
    ReviewState.shouldRefreshTaskEditor(current, conflicted, true),
    false,
  );
});
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
node --test tests/frontend/review_state.test.cjs
```

Expected: FAIL because `shouldRefreshTaskEditor` is not defined.

- [ ] **Step 3: Implement the refresh decision**

在 `review-state.js` 中实现并导出：

```js
function shouldRefreshTaskEditor(previousState, nextState, hasRenderedTasks) {
  if (!hasRenderedTasks) return true;
  return previousState?.serverIdentity !== nextState?.serverIdentity;
}
```

在 `app.js` 中：

- `render(view, { refreshTasks = true } = {})` 仅在 `refreshTasks` 为真时调用
  `taskList.replaceChildren(...)`。
- `poll()` 在合并服务端状态前保存旧 `ReviewState`，合并后调用
  `shouldRefreshTaskEditor`，并把结果传给 `render()`。
- `resetDraft=true` 时强制刷新任务编辑区。

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
node --test tests/frontend/review_state.test.cjs
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/web/static/review-state.js \
  src/feishu_generation_agent/web/static/app.js \
  tests/frontend/review_state.test.cjs
git commit -m "fix(agent): preserve task editors during polling"
```

### Task 2: 放大编辑框并恢复原生滚动与拖拽

**Files:**
- Modify: `src/feishu_generation_agent/web/static/app.js`
- Modify: `src/feishu_generation_agent/web/static/styles.css`
- Create: `tests/frontend/task_editor_styles.test.cjs`

**Interfaces:**
- Consumes: `textArea(value, onInput, rows, className)`
- Produces: `.task-prompt-editor` 与 `.task-negative-editor`

- [ ] **Step 1: Write the failing style test**

新建测试读取实际 CSS 和 JavaScript，验证两个编辑框使用不同类，并存在明确的滚动、
拖拽和最小高度规则：

```js
test("task editors have stable resizable scrollable styles", () => {
  assert.match(appSource, /task-prompt-editor/);
  assert.match(appSource, /task-negative-editor/);
  assert.match(styles, /\.task-grid textarea\s*\{[^}]*overflow-y:\s*auto/);
  assert.match(styles, /\.task-grid textarea\s*\{[^}]*resize:\s*vertical/);
  assert.match(styles, /\.task-prompt-editor\s*\{[^}]*min-height:\s*18rem/);
  assert.match(styles, /\.task-negative-editor\s*\{[^}]*min-height:\s*10rem/);
});
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
node --test tests/frontend/task_editor_styles.test.cjs
```

Expected: FAIL because专用类和样式尚不存在。

- [ ] **Step 3: Implement the minimal markup and CSS**

扩展 `textArea`：

```js
function textArea(value, onInput, rows = 3, className = "") {
  const control = document.createElement("textarea");
  control.rows = rows;
  control.className = className;
  control.value = value || "";
  control.addEventListener("input", () => onInput(control.value));
  return control;
}
```

提示词传入 `task-prompt-editor`，负面约束传入 `task-negative-editor`。添加：

```css
.task-grid textarea {
  overflow-y: auto;
  resize: vertical;
  line-height: 1.5;
}
.task-prompt-editor { min-height: 18rem; }
.task-negative-editor { min-height: 10rem; }
```

- [ ] **Step 4: Run focused and full frontend tests**

Run:

```bash
node --test tests/frontend/task_editor_styles.test.cjs
node --test tests/frontend/*.test.cjs
```

Expected: all tests PASS.

- [ ] **Step 5: Run backend regression and syntax checks**

Run:

```bash
.venv/bin/python -m pytest -q
git diff --check
```

Expected: all backend tests PASS and `git diff --check` has no output.

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/web/static/app.js \
  src/feishu_generation_agent/web/static/styles.css \
  tests/frontend/task_editor_styles.test.cjs
git commit -m "fix(agent): enlarge resizable approval editors"
```

### Task 3: 浏览器验收

**Files:**
- No source changes expected

**Interfaces:**
- Consumes: production frontend at `http://127.0.0.1:8765/`
- Produces: evidence that scrolling and resizing persist across at least two poll cycles

- [ ] **Step 1: Refresh the frontend**

前端静态文件无需重启后端。刷新页面并打开一个待审批任务。

- [ ] **Step 2: Verify scrolling**

把提示词滚动到中部，等待至少 3 秒。确认滚动位置没有跳回顶部。

- [ ] **Step 3: Verify resizing**

向下拖拽提示词和负面约束编辑框，等待至少 3 秒。确认两个高度保持不变。

- [ ] **Step 4: Verify editing**

分别修改提示词和负面约束，确认内容保持、批准按钮仍可使用，且控制台无错误。
