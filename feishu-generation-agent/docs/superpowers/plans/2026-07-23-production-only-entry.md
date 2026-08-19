# 生产表固定入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除手动文档入口，让操作员只从固定生产多维表格扫描并领取任务。

**Architecture:** 页面静态结构不再渲染旧版 URL 表单，浏览器脚本不再初始化或提交该表单。生产表扫描、审批、重跑与后端兼容接口均保持不变。

**Tech Stack:** 原生 HTML、JavaScript、Node.js 测试。

## Global Constraints

- 不修改生产表扫描、审批、重跑或结果表写入逻辑。
- 不删除后端旧文档接口。
- 不引入浏览器端构建依赖。

---

### Task 1: 移除旧版手动文档入口

**Files:**
- Modify: `web/static/index.html:10-24`
- Modify: `web/static/app.js:初始化和 run-form 提交绑定`
- Test: `tests/frontend/bitable_state.test.cjs`

**Interfaces:**
- Consumes: 已存在的 `#scan-bitable-button` 扫描入口与生产表 API。
- Produces: 仅包含生产表操作入口的页面；不再访问 `#run-form`、`#source-url` 或 `#legacy-mode-message`。

- [ ] **Step 1: 写失败测试**

在 `tests/frontend/bitable_state.test.cjs` 增加静态入口断言：

```js
test("production-only page has no legacy document form", () => {
  const html = readFileSync(new URL("../../src/feishu_generation_agent/web/static/index.html", import.meta.url), "utf8");
  assert.equal(html.includes('id="run-form"'), false);
  assert.equal(html.includes('id="source-url"'), false);
  assert.equal(html.includes('id="scan-bitable-button"'), true);
});
```

- [ ] **Step 2: 运行测试确认失败**

运行：`node --test tests/frontend/bitable_state.test.cjs`

预期：失败，因为当前页面仍含 `id="run-form"`。

- [ ] **Step 3: 实现最小改动**

从 `web/static/index.html` 删除整个 `<form id="run-form" class="source-form">…</form>`。在 `web/static/app.js` 删除只服务于旧入口的 `setLegacyMode()` 与 `byId("run-form").addEventListener("submit", …)` 绑定；其余生产表状态、扫描和运行轮询不改动。

- [ ] **Step 4: 运行测试确认通过**

运行：`node --test tests/frontend/bitable_state.test.cjs`

预期：17 条既有前端测试加新增入口测试全部通过。

- [ ] **Step 5: 完整验证并提交**

运行：`./.venv/bin/python -m pytest -q && node --test tests/frontend/*.test.cjs`

预期：Python 与前端测试全部通过。

提交：

```bash
git add web/static/index.html web/static/app.js tests/frontend/bitable_state.test.cjs
git commit -m "feat(agent): use production table as the only entry"
```
