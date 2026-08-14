# 04 — Portal 接入与统计

**前置**：`00`~`03` 册
**产出**：画布作为第 6 个 tab 出现在 Portal，且用量正确计入统计
**验证**：跑一次真实生成，统计页数字增加且归属正确

> 本册与 03 册（翻译层）可并行，但**验收必须在 03 之后**。

---

## 1. 注册子应用

`portal/apps.json` —— **数组顺序即 tab 顺序**。插在 `volcengine-portrait` 之后、`feishu-generation-agent` 之前：

```json
  {
    "name": "infinite-canvas",
    "display_name": "无限画布",
    "port_env": "INFINITE_CANVAS_PORT",
    "port_default": 8893,
    "mount": "iframe",
    "iframe_url": "/infinite-canvas/index.html",
    "color": "#ec4899",
    "credential_scheme": "none",
    "job_type": "dynamic",
    "job_type_rules": [
      {"keywords": ["image"], "type": "image"}
    ],
    "metrics": ["images", "seconds"],
    "unit_label": "张+秒",
    "stats_combine": "images_and_seconds"
  },
```

字段说明（对照 `portal/app_spec.py:36-71`）：

| 字段 | 取值理由 |
|---|---|
| `name` | 同时是代理前缀 `/infinite-canvas/*` 和目录名 |
| `credential_scheme: "none"` | Key 在 seedance/nano-banana 那边，画布自己不持有 |
| `job_type: "dynamic"` | 画布同时出图和出视频，不能写死。实际分类由轮询时的 `task_type` 决定（见 §3） |
| `metrics` + `stats_combine` | 与 dreamina 同构（它也是图+视频混合） |
| `color` | `#ec4899` 粉色，与现有 5 个（蓝/绿/紫/橙）不撞 |

**不要**加 `managed: false`（默认 true，让 Portal 托管进程）。

后端零改动 —— `SPECS` 从这个文件加载（`portal/app.py:62-64`），代理、健康检查、`/api/apps` 元数据全部自动跟随。

---

## 2. 前端 tab

### 2.1 按钮

`portal/static/index.html:30` 之后（人像生成之后、密钥库之前）：

```html
      <button class="app-tab" data-tab="infinite-canvas">无限画布</button>
```

### 2.2 面板

`portal/static/index.html:51`（feishu 面板）之后，用 `data-app` 范式让 src 由 `/api/apps` 注入，别写死：

```html
  <!-- INFINITE CANVAS -->
  <div class="tab-panel" id="tab-infinite-canvas">
    <iframe id="iframe-infinite-canvas" data-app="infinite-canvas"
            style="width:100%;height:calc(100vh - 48px);border:none;display:block"
            allow="fullscreen"></iframe>
  </div>
```

⚠️ **`id` 必须是 `tab-` + `data-tab` 的值**。切换逻辑是 `document.getElementById('tab-' + btn.dataset.tab).classList.add('active')`（`portal/static/app.js:80`），拼错会 `null.classList` 抛错，**整个 tab 栏卡死**（不只是这一个 tab）。

`initConfiguredIframes()`（`portal/static/app.js:85-93`）会自动把 `iframe_url` 填进 `data-app` 匹配的 iframe。

### 2.3 统计前端无需改动

`appColor`/`appUnit`/`appLabel`/`pickAppValues`（`portal/static/app.js:1228-1284`）优先读 `/api/apps` 返回的 `appsMeta`，而 `_apps_meta()`（`portal/app.py:1790-1808`）自动包含所有 SPECS。只在请求失败时才回落到硬编码集合。

---

## 3. 统计接入（CRITICAL — 本册核心）

CLAUDE.md 顶部四行强调「每次更新都要检查是否会影响统计功能」。新子应用最容易的失败模式是**静默不计数**：功能全好，统计页永远是 0，而且没有任何报错。

### 3.1 登记路径必须进白名单

`_is_job_request()`（`portal/app.py:1101-1107`）是**硬编码白名单**：

```python
job_patterns = ["/api/jobs", "/api/text2image", ...]
```

画布提交任务的路径是 `/api/v1/jobs`（前端契约固定，见 `web/src/api/jobs.ts:4`）。`"/api/v1/jobs".startswith("/api/jobs")` 是 **False** —— 不改就完全不计数。

**改法**（`portal/app.py:1103` 的列表里追加）：

```diff
         job_patterns = ["/api/jobs", "/api/text2image", "/api/image2image", "/api/text2video",
                         "/api/image2video", "/api/frames2video", "/api/multimodal2video", "/api/multiframe2video",
-                        "/api/virtual/jobs", "/api/real/jobs"]
+                        "/api/virtual/jobs", "/api/real/jobs", "/api/v1/jobs"]
```

⚠️ 注意 `startswith` 语义：`/api/v1/jobs/{id}/cancel` 也是 POST 且会命中前缀 → 被当成创建任务。但取消接口不返回 `X-Job-Id`，第三个条件不满足（`portal/app.py:2093-2097`），所以不会误登记。**取消接口绝不能返回 `X-Job-Id`**，这条写进 03 册的实现要求。

### 3.2 三个登记条件缺一不可

`portal/app.py:2093-2097`：

```python
if is_job and resp.status in (200, 201):
    jid_header = resp.getheader("X-Job-Id", "").strip()
    if jid_header:
        tracker.register_job(app_name, jid_header, user["username"], job_type)
        tracker.inc_daily_jobs(app_name)
```

1. 路径命中白名单（§3.1 已解决）
2. 状态码 **200 或 201** —— 异步任务的直觉是返 202，**返 202 就不计数**
3. 响应头 **`X-Job-Id`** 非空

翻译层的 `POST /api/v1/jobs` 必须同时满足三条。另需 `Access-Control-Expose-Headers: X-Job-Id`（同源 iframe 下非必需，但与现有子应用保持一致）。

### 3.3 轮询接口的路径陷阱

Portal 轮询写死 `GET /api/jobs/{id}`（`portal/app.py:987`），**不带 `/v1`**：

```python
conn.request("GET", f"/api/jobs/{job['job_id']}")
```

而画布前端调的是 `/api/v1/jobs/{id}`。**两个路径都要能用**：

- `/api/v1/jobs/{id}` → 给画布前端，返回 `JobState` 形状（`contracts.ts:23`）
- `/api/jobs/{id}` → 给 Portal 轮询，返回统计所需字段

可以是同一个 handler 挂两个路由，返回**同时包含**两组字段的对象（前端只读它认识的，Portal 只读它认识的，互不干扰）。这比维护两套转换省事。

### 3.4 轮询响应字段

Portal 读取逻辑（`portal/app.py:993-1037`）：

| 字段 | 要求 |
|---|---|
| `status` | 终态必须是 `succeeded` / `failed` / `completed` 之一。**画布前端的 `JobState.status` 恰好也用 `succeeded`/`failed`**（`contracts.ts:23`），天然兼容 |
| `done` | **真实产出件数**。纯失败报 0，部分成功报实际数（3 张成功 1 张失败就报 3） |
| `task_type` | 决定图/视频分类，见下 |
| `duration` 或 `duration_seconds` | 视频**每件**时长（秒）。Portal 算 `done * per_item` |

**`task_type` 是这次接入的关键机制**（`portal/app.py:1017-1022`）：

```python
task_type = (data.get("task_type") or nested.get("task_type") or "").lower()
job_type = job["job_type"]
if "video" in task_type or "frame" in task_type:
    job_type = "video"
elif "text2image" in task_type or "image2image" in task_type:
    job_type = "image"
```

它**覆盖**注册时的分类。所以画布这种图/视频混合应用，只要在轮询响应里如实上报 `task_type`，就能被正确分流计数：

| 画布 operation | 上报 `task_type` | 计入 |
|---|---|---|
| `image.generate` | `text2image` | images（张） |
| `image.edit` | `image2image` | images（张） |
| `video.generate` | `text2video` | seconds（秒） |
| `video.image_to_video` | `image2video` | seconds（秒） |

注意匹配是子串：`text2video` 含 `"video"` → 命中第一个分支判为 video ✓；`text2image` 不含 video/frame，命中第二分支判为 image ✓。四个都对。

### 3.5 严禁的做法

`portal/app.py:1003-1010` 有一段很长的注释警告，照抄要点：

> **不要**把 `done` 用 `max(1, ...)` 兜底。轮询循环只在子应用的 finalize 回调没送达时才看到这个任务，纯失败也会走到这里；兜底成 1 就制造了一张不存在的图。

`done <= 0` 时 Portal 跳过统计写入但仍停止轮询（`portal/app.py:1027`），这是正确行为，不要试图"修"它。

### 3.6 finalize 回调

任务终态时回调 Portal 回滚失败任务的计数：

```
POST https://127.0.0.1:9090/api/internal/jobs/finalize
Header: X-Internal-Token: <PORTAL_INTERNAL_TOKEN>
```

Portal 侧 `_internal_finalize_job()`（`portal/app.py:2187-2207`）只接受来自 `127.*` 且 token 匹配的请求，`finalize_job()`（`:1064-1099`）幂等回滚 `daily.jobs`。

实现照抄 `nano-banana/app.py:214-235` 的 `report_final_to_portal()`，注意三点：Portal 是自签 HTTPS（需要关闭证书校验的 SSL context）、2 秒超时、**异常必须吞掉**（回调失败不能影响主流程，轮询是兜底路径）。

⚠️ `finalize` **不得**把任务移出 Portal 的 `_pending_jobs`（`portal/app.py:1071-1076` 注释明确）。两条路径写的是不相交的计数器：finalize 管 `daily.jobs`，轮询管 `by_user.images/seconds`。这是 Portal 侧行为，我们只是别去干扰。

### 3.7 活动流（可选）

`_platform_activity()`（`portal/app.py:1937-1965`）向每个 `managed` 子应用请求 `GET /api/activity`，取 `records`/`items`/`history` 前 20 条。实现了画布任务就会出现在管理员的活动流里。**可选，不影响统计**。

---

## 4. 启动配置

### 4.1 launchd plist（必改）

画布是 FastAPI，必须显式开引擎开关。编辑 `~/Library/LaunchAgents/com.ai-portal.plist`，在 `EnvironmentVariables` 字典里加：

```xml
  <key>INFINITE_CANVAS_ENGINE</key>
  <string>fastapi</string>
```

env 名推导规则：`name.upper().replace('-', '_') + "_ENGINE"`（`portal/app.py:641`），`infinite-canvas` → `INFINITE_CANVAS_ENGINE`。

前置条件（`portal/app.py:646` 会检查，任一不满足就静默回退到 `app.py`）：
- `infinite-canvas/app_fastapi.py` 存在
- 仓库根 `.venv/bin/uvicorn` 存在 ✓（已确认）

改完必须重载，不然不生效：

```bash
launchctl kickstart -k gui/$(id -u)/com.ai-portal
```

### 4.2 `Start All.command`（两处）

端口列表在**第 7 行和第 29 行**，都要改。顺手补上已经漏掉的 8891：

```diff
-for port in 8787 8797 8888 9089 9090; do
+for port in 8787 8797 8888 8891 8893 9089 9090; do
```

⚠️ **但这还不够**：脚本用 `grep -q "app.py"` 匹配命令行来判断是否本项目进程（`Start All.command:13`），而画布由 uvicorn 启动，命令行是 `.../uvicorn app_fastapi:app ...` —— **匹配不到，不会被杀**，端口占用会导致下次启动失败。

改法（第 13 行和第 35 行附近两处同样的判断）：

```diff
-    if echo "$cmd" | grep -q "app.py"; then
+    if echo "$cmd" | grep -qE "app\.py|app_fastapi:app"; then
```

> 注：生产是 launchd 托管，这个脚本只在手动开发时用。但 CLAUDE.md 记录过「用户手动 `Start All.command` 不杀旧进程会静默启动失败继续跑旧代码」，值得修对。
>
> Portal 自己的 `_kill_port_squatter`（`portal/app.py:612`）会在启动子应用前清占用，是兜底。

---

## 5. 验收

### 5.1 重启并确认进程

```bash
launchctl kickstart -k gui/$(id -u)/com.ai-portal
sleep 8
lsof -iTCP -sTCP:LISTEN -P -n | grep -E "9090|8893"
ps -p $(lsof -ti:8893) -o command=
```

**期望**：8893 在听，且命令行含 `uvicorn app_fastapi:app`。若是 `python app.py` → 引擎开关没生效，回查 §4.1。

```bash
tail -30 portal/state/logs/infinite-canvas.log
```

**期望**：uvicorn 启动行，无 traceback。

### 5.2 链路连通

```bash
curl -k -s -o /dev/null -w "%{http_code}\n" https://127.0.0.1:9090/infinite-canvas/index.html
```

Portal 是 HTTPS 自签，**`curl` 必须带 `-k`**。未登录会 302 到 `/login`，属正常。

浏览器登录 Portal → 点「无限画布」tab：

- [ ] iframe 加载出画布，**没有**二次登录页
- [ ] 侧边栏只有「项目 / 资产 / 任务」
- [ ] 右下角显示的用户名 = Portal 登录用户
- [ ] 新建项目 → 拖节点 → 刷新页面，位置保留（存档链路通）
- [ ] 开浏览器 devtools，Network 里 `/infinite-canvas/api/v1/*` 全是 200，无 404/401

### 5.3 统计验收（最关键，必须真实跑一次）

CLAUDE.md 明确「时间 >> token，能真实复现就真实复现（哪怕产生少量出图费用）」。统计链路**必须实测**，不能推断。

**图片**：画布里生成 1 张图 →

```bash
python3 -c "import json;d=json.load(open('portal/state/usage.json'));print(json.dumps(d.get('by_user',{}),ensure_ascii=False,indent=1)[:800])"
```

- [ ] `by_user[今天][你的用户名]["infinite-canvas"]["images"]` **+1**
- [ ] `daily[今天]["infinite-canvas"]["jobs"]` **+1**
- [ ] Portal「统计」tab 能看到「无限画布」这一行，单位「张」

**视频**：生成 1 个 5 秒视频 →

- [ ] `by_user[...]["seconds"]` **+5**（不是 +1）
- [ ] `images` **不变**（说明 `task_type` 分流正确）

**失败任务**：故意用错参数触发失败 →

- [ ] `images`/`seconds` **不变**（没有幽灵计数）
- [ ] `daily.jobs` 被 finalize 回滚

**归属**：用另一个账号生成 →

- [ ] 计入另一个用户名，不串号

### 5.4 回归（改了 Portal 共享代码，必须回归）

`_is_job_request` 和 `_cors_headers` 是所有子应用共用的。改完必须确认没影响既有应用：

- [ ] seedance 生成 1 个视频 → 统计 seconds 正常增加
- [ ] nano-banana 生成 1 张图 → 统计 images 正常增加

> 本次对 `_is_job_request` 只做了列表**追加**，不改变既有前缀的匹配结果，风险低。但既然 CLAUDE.md 把统计列为最高优先级，实测一遍。

---

## 6. 完成判据

- [ ] `portal/apps.json` 有 infinite-canvas 条目
- [ ] tab 按钮 + 面板已加，`id` 与 `data-tab` 对应正确
- [ ] `_is_job_request` 白名单含 `/api/v1/jobs`
- [ ] plist 有 `INFINITE_CANVAS_ENGINE=fastapi` 且已 kickstart
- [ ] `Start All.command` 端口列表 + uvicorn 进程匹配都已修
- [ ] §5.3 统计四项全部实测通过
- [ ] §5.4 回归通过

## 7. 顺带修正 CLAUDE.md

探查中发现文档与实际不符，本次一并更新（尤其第一条，它会误导任何碰统计的改动）：

| 位置 | 文档写的 | 实际 |
|---|---|---|
| CLAUDE.md:103 | `_proxy()` 读完整 body 提取 job_id | 已改为 `X-Job-Id` 响应头 + 全流式（`portal/app.py:2089-2103`） |
| CLAUDE.md:137 | plist 用 `/usr/bin/python3` | 实测 `/opt/homebrew/bin/python3.12` |
| CLAUDE.md:71 | index.html「All 4 tabs」 | 实际 7 个（本次后 8 个） |
| CLAUDE.md 端口表 | 缺 8891/8765 | 补齐，并加 infinite-canvas 8893/8894 |
