# Feishu Agent Inline Styles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复飞书 Agent 在 Portal iframe 中的原有样式，同时保持现有页面结构、交互、静态 CSS 地址和零构建依赖不变。

**Architecture:** `styles.css` 继续作为唯一样式源；Agent `/` 路由读取 HTML 和 CSS，并将唯一的外链样式标签替换成内嵌 `<style>`。这样首屏样式与 HTML 一次交付，绕过生产 iframe 的独立 CSS 请求边界，而静态 CSS 路由仍保留。

**Tech Stack:** Python 3.12、FastAPI、原生 HTML/CSS/JavaScript、httpx、pytest

## Global Constraints

- 不改变现有 Agent 页面布局、视觉规则、按钮或工作流。
- 不改 Portal iframe 地址、用户身份代理或任务隔离。
- `web/static/styles.css` 必须继续作为唯一的样式源文件。
- `/static/styles.css` 必须继续可访问。
- 不引入第三方依赖或客户端构建步骤。
- 后端重启前必须确认没有运行中的生成任务。
- 使用测试驱动：先观察新断言失败，再写最小实现。

---

### Task 1: Inline the Existing Stylesheet in the Workspace Response

**Files:**

- Modify: `feishu-generation-agent/tests/integration/test_api.py`
- Modify: `feishu-generation-agent/src/feishu_generation_agent/web/app.py`

**Interfaces:**

- Consumes: `static/index.html` 中唯一的 `<link rel="stylesheet" href="static/styles.css">`
- Produces: `GET /` 返回包含 `<style data-agent-inline-styles>` 的 HTML；`GET /static/styles.css` 行为不变

- [ ] **Step 1: Write the failing integration assertions**

在 `test_static_review_workspace_is_served_and_uses_safe_dom_updates` 中增加：

```python
assert '<style data-agent-inline-styles>' in page.text
assert '<link rel="stylesheet" href="static/styles.css">' not in page.text
assert "--paper: #f7f8f4" in page.text
assert styles.headers["content-type"].startswith("text/css")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/integration/test_api.py::test_static_review_workspace_is_served_and_uses_safe_dom_updates -q
```

Expected: FAIL because `/` still contains the external stylesheet link and has no inline style marker.

- [ ] **Step 3: Add a strict workspace renderer**

在 `web/app.py` 中导入 `HTMLResponse`，增加：

```python
_WORKSPACE_STYLESHEET_LINK = (
    '<link rel="stylesheet" href="static/styles.css">'
)


def _render_workspace_html(static_dir: Path) -> str:
    html = (static_dir / "index.html").read_text("utf-8")
    if html.count(_WORKSPACE_STYLESHEET_LINK) != 1:
        raise RuntimeError("workspace stylesheet link must appear exactly once")
    styles = (static_dir / "styles.css").read_text("utf-8")
    inline = f"<style data-agent-inline-styles>\n{styles}\n</style>"
    return html.replace(_WORKSPACE_STYLESHEET_LINK, inline)
```

将 `/` 路由改成：

```python
@app.get("/", include_in_schema=False)
async def workspace() -> HTMLResponse:
    return HTMLResponse(
        _render_workspace_html(static_dir),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
./.venv/bin/python -m pytest \
  tests/integration/test_api.py::test_static_review_workspace_is_served_and_uses_safe_dom_updates -q
```

Expected: `1 passed`.

- [ ] **Step 5: Run Agent and Portal regression tests**

Run:

```bash
./.venv/bin/python -m pytest -q
node --test tests/frontend/*.test.cjs
cd .. && /opt/homebrew/bin/python3.12 -m unittest discover \
  -s tests -p 'test_dispatch_via_spec.py' -v
```

Expected: all selected suites pass.

- [ ] **Step 6: Commit**

```bash
git add \
  feishu-generation-agent/src/feishu_generation_agent/web/app.py \
  feishu-generation-agent/tests/integration/test_api.py
git commit -m "fix(agent-ui): inline workspace styles through portal"
```

### Task 2: Deploy and Verify the Portal UI

**Files:**

- No source changes expected

**Interfaces:**

- Consumes: production Agent launchd service on `127.0.0.1:8765`
- Produces: styled Agent workspace through the current relative Portal iframe route

- [ ] **Step 1: Check active work before restart**

Run:

```bash
curl -fsS http://127.0.0.1:8765/api/bitable/active-runs
```

Expected: no run in a paid execution state. Waiting approval may remain because restart recovery is persisted.

- [ ] **Step 2: Restart only the Agent service**

Run:

```bash
launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent
```

Expected: a new Agent PID listens on `127.0.0.1:8765`; Portal and generation sub-app PIDs stay unchanged.

- [ ] **Step 3: Verify HTTP behavior**

Run:

```bash
curl -fsS http://127.0.0.1:8765/ | rg \
  'data-agent-inline-styles|--paper: #f7f8f4'
curl -fsSI http://127.0.0.1:8765/static/styles.css
```

Expected: the root contains inline styles and the static stylesheet still returns `200 text/css`.

- [ ] **Step 4: Verify the actual page**

Open the direct Agent page and a logged-in Portal iframe. Confirm:

- computed body background is `rgb(247, 248, 244)`;
- header layout is grid rather than browser defaults;
- task cards, recent runs, workflow panel and review panel use the existing styled layout;
- no stylesheet or JavaScript errors appear in the browser console.

- [ ] **Step 5: Verify repository and service state**

Run:

```bash
git diff --check
git status --short --branch
lsof -nP -iTCP -sTCP:LISTEN | rg ':(8765|9090)\b'
```

Expected: no whitespace errors, only known user-owned untracked files remain, and both services listen.
