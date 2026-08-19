# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！
！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！
！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！
！！！核心：每次更新都要检查是否会影响统计功能的实现！！！！

！！！优先级：时间 >> token！！！用户要的是尽快修好、一次到位。宁可多花
token 做 live 实测 / 并行探查 / 端到端验证把问题一锤定音，也不要为省 token
靠猜测反复打补丁、反复让用户重启验证。定位生产问题时，能真实复现就真实复现
（哪怕产生少量出图费用），不要用推断代替证据。

## 长任务防卡死：产出优先落盘，别攒在上下文

**症状**：写实施 plan / 长文档 / 多步方案时，会陷入"复述状态→摸信息→再复述"的循环，API
每断一次就丢工作、从头开始，用户看到"一直在工作但没产出"。

**根因**：`superpowers:writing-plans` 这类 skill 强制走 announce → 摸信息 → self-review
→ execution handoff 一串前置流程；内容全在上下文里、没落磁盘，断链就丢。系统 reminder
和 auto-mode 又会不断触发我复述状态而不是往下写。

**修复方式（对任何多轮长产出都适用）**：

1. **绕开 skill 的流程壳，直接 Write 文件**。skill 里的**格式指引**（TDD 五步、每步含
   代码 / 命令 / 期望输出、无 placeholder）值得保留；**流程指引**（announce、摸信息、
   self-review、handoff）跳过——它们不落盘、断链即丢。
2. **文件一落盘 = 一个 checkpoint**。下次续写从磁盘读，不依赖上下文。写完一个文件立刻
   commit 的心态：宁可多份短 plan，不要一份憋大的。
3. **分批产出而不是一次全写**。3 份 plan 就 3 次 Write，不要合成一份大 plan——大 plan
   写到一半断链是灾难，短 plan 断了只丢一份。
4. **信息够就写，不追求穷尽**。plan 的完整性靠"格式对了 + 决策对了"，不靠"摸到所有
   行号"。行号写错执行者能自己修，决策错了返工代价大得多。
5. **不要每轮口头解释"我在做什么"**。用户看到 Write 工具在跑就知道，反复复述是噪音。

## 项目定位

这是一个部署在**服务机**（用户本机 Mac）上的多子应用聚合平台，聚合 Seedance / Nano Banana / Dreamina / Volcengine Portrait 等 AI 生成能力，统一 Portal 前端 + 反向代理暴露给使用者。

**当前部署**：服务机通过局域网 HTTPS（`https://192.168.30.5:9090`，自签证书）向公司同事提供服务，同事只需浏览器即可使用，不需要在自己电脑上安装任何环境。

**后续演进方向**：可能迁移到公网服务器，让外部客户通过域名访问。因此设计上应尽量避免绑死「本机路径 / 本机 IP / 单机 launchd」这类假设——涉及主机名、证书、端口、路径的代码要留出配置化的余地，方便日后切换到域名 + 反向代理 + 正式证书的部署形态。

## Running

```bash
# Start everything (Portal + all sub-apps) on port 9090
./Start\ All.command

# Or manually:
cd portal && python3 app.py
```

Portal binds `0.0.0.0:9090` and auto-launches sub-apps on their fixed ports. Individual sub-apps can also run standalone:

```bash
cd seedance && python3 app.py    # port 8787
cd nano-banana && python3 app.py # port 8797
cd dreamina && python3 app.py    # port 8888
```

当前后端主要使用 stdlib（`http.server`、`threading`、`concurrent.futures`、`subprocess`），但**不是硬约束**。项目部署模型：用户本机（Mac）作为唯一后端服务器，公司其他电脑通过局域网浏览器访问，不分发后端代码给客户端。因此可以按需引入 pip 库；新加依赖时先与用户确认，并装到 launchd 使用的解释器（`/opt/homebrew/bin/python3.12`）下。客户端侧（浏览器里跑的 HTML/JS/CSS）才需要「无环境依赖」——不能引入构建工具链。

## Architecture

```
portal/           → Unified SPA + reverse proxy (port 9090)
├── app.py        → ThreadingHTTPServer: serves static/, proxies /seedance/*, /nano-banana/*, /dreamina/* to sub-apps, tracks usage stats (by_ip), polls job completion
├── static/
│   ├── index.html  → 全部 tab（Seedance / 图像生成 / 飞书 Agent / Dreamina / 人像 / 无限画布 / 密钥库 / 统计）
│   │                 tab 按钮与面板是硬编码的，加子应用要手工改；面板 id 必须是 "tab-" + data-tab
│   ├── app.js      → Single IIFE: tab switching, form submission, provider binding, stats rendering
│   └── styles.css

seedance/         → Video generation (Seedance 2.0 via T8Star or Volcengine Ark)
├── app.py        → Full app: HTTP handler, job runner (ThreadPoolExecutor), file upload/download, archive system
├── providers.json → Provider configs (base_url, models, defaults per provider)
└── static/       → Standalone UI (used when running without Portal)

nano-banana/      → Image generation (T8Star OpenAI-style or Gemini)
├── app.py        → Same pattern as seedance
├── providers.json
└── static/

dreamina/         → Image/video via Dreamina CLI wrapper
├── app.py        → Wraps `dreamina` CLI tool, manages login/env, polls submit_id for results
├── config.json   → Runtime config (port, max_concurrent, poll intervals)
└── static/
```

## Key Patterns

**Sub-app structure**: Each sub-app is a single `app.py` with:
- `FALLBACK_PROVIDERS` dict (seedance/nano-banana) or `DEFAULT_CONFIG` (dreamina)
- `VALUE_FIELDS` set defining which form fields are extracted
- `run_job()` → spawns `run_one()` per concurrency slot via ThreadPoolExecutor
- `JOBS` dict (in-memory) holding all job state; not persisted across restarts
- `Handler` class extending `SimpleHTTPRequestHandler` with REST endpoints
- `/api/config` returns providers, models, key hint
- `/api/jobs` POST creates jobs, GET returns status
- Archives stored as `.seedance`/`.nanobanana`/`.dreamina` zip files in `archives/`

**Portal proxy**: `_proxy()` 全程流式转发（`shutil.copyfileobj`，64KB 块），**不缓冲响应体**。任务 id 由子应用通过 **`X-Job-Id` 响应头**上报，Portal 据此登记用量（`portal/app.py:2089-2097`）。登记的三个必要条件：POST 路径命中 `_is_job_request` 白名单、响应状态 200/201、`X-Job-Id` 非空。

> 早期实现是「读完整 body 提取 job_id」，因长任务阻塞代理线程已于 #15 改掉。新增子应用若不发 `X-Job-Id`，统计会**静默不计数**（功能全正常、数字永远是 0）。

**Provider system** (seedance, nano-banana): `providers.json` defines available providers with `base_url`, `models[]`, `defaults{}`. Frontend `bindProviderSwitch()` rebuilds model dropdown and updates URL on provider change.

**Output naming**: When `output_name` is set, files are named `{name}-{index}.ext` for multi-concurrency or `{name}.ext` for single runs. Empty means timestamp-based auto-naming.

**Environment detection**: Portal sets `CORS=1` env var on sub-apps. Sub-apps check this to skip auto-opening browser and to add CORS headers.

## Important Constraints

- **Never overwrite git history** — always create new commits, never amend/force-push
- **第三方库按需使用** — 后端跑在用户本机，不分发；引入 pip 库前确认并装到 `/opt/homebrew/bin/python3.12`。客户端浏览器代码仍需零构建依赖
- **Jobs are in-memory** — restarting kills running tasks; coordinate with users before restart
- **Frontend changes are instant** — Portal serves with `Cache-Control: no-cache, no-store, must-revalidate`, clients get new version on refresh without restart
- **Backend changes require restart** — which terminates all sub-app processes and running jobs
- **Sub-app ports are fixed** — seedance:8787, nano-banana:8797, dreamina:8888, portal:9090

## File Conventions

- `state/` — runtime JSON (usage, presets, activity logs); gitignored
- `outputs/` — generated files; gitignored
- `archives/` — user-saved presets as zip; gitignored, may contain API keys
- `logs/` — startup/debug logs; gitignored
- `providers.json` — provider/model configuration; committed
- Each app has exactly one `app.py` (no module splitting)

## 当前状态快照（volatile — 修改前 verify）

**verified 2026-07-03**

- **实际启动方式**：`~/Library/LaunchAgents/com.ai-portal.plist`（launchd 守护，`KeepAlive=true`，`RunAtLoad=true`），**不是**双击 `启动器.command`
- **重启命令**：`launchctl kickstart -k gui/$(id -u)/com.ai-portal`（改 plist 后必须重载，`launchctl list | grep com.ai-portal` 看状态）
- **cloudflared 已 unload**（`com.ai-portal-tunnel.plist`），改走局域网 HTTPS
- **访问 URL**：`https://192.168.30.5:9090`（自签证书，首次访问需点「高级 → 继续」）；9089 是 HTTP→HTTPS 跳转
- **Python 路径**：plist 里是 `/opt/homebrew/bin/python3.12`（2026-08-14 实测确认）；**不要**用系统 `/usr/bin/python3`（3.9），它会让所有代理请求静默超时
- **改 plist 后必须 `unload` + `load`**：`launchctl kickstart -k` 只重启进程、**不重读 plist**（2026-08-14 实测：加了 `INFINITE_CANVAS_ENGINE` 后 kickstart 无效，子应用回退到 stdlib 引擎）
- **端口表**：

| App | 生产 | 测试 |
|-----|------|------|
| Portal | 9090 | 9190 |
| Redirect (HTTP→HTTPS) | 9089 | 9189 |
| Seedance | 8787 | 8788 |
| Nano Banana | 8797 | 8798 |
| Dreamina | 8888 | 8890 |
| Volcengine Portrait | 8891 | 8892 |
| Infinite Canvas | 8893 | 8894 |
| Feishu Generation Agent | 8765 | — |

- **证书文件**：`portal/state/portal.pem` + `portal.key`；LAN IP 变化时 `ensure_certs()` 自动重生（`portal/app.py:101-131`）
- **下载映射持久化**：`state/download_files.json`（token→文件路径）
- **数据布局（2026-07-22 起）**：各子应用的 `outputs/`、`state/`、`archives/`、`uploads/`、`accounts/` 以及 `portal/state/` 已从软链改为**主仓库内的真实目录**，不再依赖 `ai-generation-portable-apps-backup-2026-07-14-1653/`（该 backup 目录已删除，主干数据打包留档在 `~/backup-trunk-2026-07-22.zip`）。迁移时**弃掉了草稿缓存** `state/workspaces/` 和 `state/media/`（历史参考图需用户重传）以及 `portal/state/logs/`。`activity_log.json` / `usage.json` / `users.json` / `accounts.json` 等主干与统计数据完整保留。
- **飞书产出搬运**：独立服务 `com.feishu-output-sync`（launchd，**独立于 com.ai-portal**）常驻轮询 `feishu-output-sync/sync.py`，把各子应用 outputs 增量搬进「每人一张多维表格」（组织内可编辑）。日志 `~/Library/Logs/feishu-output-sync.log`；配置 `feishu-output-sync/config.json`（gitignored）。

## 无限画布 2026-08-19 上游同步（新增功能）

从 `~/ai-creation-canvas`（上游 fork）同步了 8/14 之后 72 个提交的功能，**translated** 进 `infinite-canvas/`：

- **画布改进**：preset 参数控件（Ark 尺寸 1K..4K + 自定义宽x高，schema 用 `x-ark-size`）、结果派生引用 `job-result.{jobId}.{index}`（翻译层 `_resolve_asset` 解析，前端 deriveResultAssetId 生成）、nanoid 替代 crypto.randomUUID（plain-HTTP 浏览器修复）、提交超时 600s。
- **Ark 人像素材库**：画布右下角「人像资产库」按钮 → 上传 PNG/JPEG/WebP（10MB 内、**宽高 300-6000px**，方舟硬限制）→ TOS PUT → CreateAsset 进方舟 AIGC 素材库 → 本地副本保留，生成时走本地字节。配置 `infinite-canvas/state/asset-library.json`（gitignored，已从 volcengine-portrait config 派生）；**SK 一律原始值**，勿 base64 解码（控制台复制值恰好是合法 base64，解码 = SignatureDoesNotMatch）。后端模块 `ark_library.py`。
- **ComfyUI 工作流库**：管理员侧边栏「工作流库」→ 导入/预览/导出/启停工作流；`execution_available` 恒 False（执行切片上游也没交付）。服务配置 `state/comfyui-services.json`（可选）；启用但未配置服务时 409。放行策略：**启用即全员可见**（上游按人授权在 Portal 形态不可用）。模块 `comfy_lib.py`（解析/校验）+ `comfy_api.py`（API）。
- **TOS 预签名 GET 的坑**：canonical_headers 必须以 `\n` 结尾、模板再补 `\n`（即空行），否则 SignatureDoesNotMatch —— 与 AWS SigV4 不同，与 volcengine-portrait/app.py:246-254 一致。
- **跳过**：注册/密码管理/凭证池/后台日志等上游服务端功能（Portal 负责身份与统计，见 docs/infinite-canvas/01-前端改造.md 的裁剪原则）。
- 上游同步点：`c3d5aed` → `6d1d2c6`（前端源码 58 文件、后端语义对齐）；前端单测 428 全过。

## 稳定教训（跨版本长期有效）

### 部署与重启

- 改 `启动器.command` 或 shell 里 `export ENV=...` **不生效**：launchd 不读用户 shell 环境，只读 plist 的 `EnvironmentVariables`
- `kill` Portal 进程没用：`KeepAlive=true` 会立刻拉起。要 `launchctl unload` 或 `kickstart -k`
- 「关外网通道」= `launchctl unload com.ai-portal-tunnel.plist`，不是 `pkill cloudflared`（pkill 后 launchd 立刻拉起）
- 用户手动 `Start All.command` 不杀旧进程，会因 `Errno 48 Address already in use` 静默启动失败继续跑旧代码
- **重启后必须 verify**：`ps -p <PID> -o command=` 确认 Python 路径、`lsof -iTCP -sTCP:LISTEN -P -n | grep -E "9090|8787|8797|8888|8891"` 确认端口、对比进程启动时间 vs 代码修改时间
- Portal 是 HTTPS，`curl` 测试必须带 `-k`（Connection reset by peer 不是 bug）

### 前端 iframe 缓存

- 子应用 JS 由 `SimpleHTTPRequestHandler` 直接返回，**不带 Cache-Control**，浏览器按启发式缓存旧 JS 导致修复不生效
- Portal `_proxy()` 已对 `.html/.js/.css/.mjs` 强制加 `Cache-Control: no-cache, no-store, must-revalidate`

### 子应用内多标签（seedance / nano-banana）

- 顶部有页面级 tab 栏，切 tab = 换 `activeTabId` + 保存当前 draft 到 `<app>.workspace.<id>` + 从 localStorage 恢复目标 draft
- **tab 栏必须放在 `<main class="app">` 之外**（`<div id="sd-app">`/`<div id="nb-app">` 的直接子元素），`.app` 是两列 CSS Grid（`360px + 1fr`），tab 栏塞进去会抢 sidebar 格子，`#111827` 深背景撑满 360×100vh → 黑块 bug（`677c088` 修）
- 所有 `api(url, ...)` 请求自动带 `?ws=<activeTabId>`，`window._activeWorkspaceId` 在 init/newTab/switchTab/_forceCloseTab 都要更新；老 `X-Workspace-Id` header 保留兼容
- `pollJob` 是 tab-scoped：startWsId 快照 → 每次 setState 判断当前 activeTabId 是否变了，切走时写 `_tabStateCache[wsId]`（含 `_latestJob` 快照）；切回来 `loadTargetTabState()` 从 cache 恢复 statusText/eventsText/DOM
- `tab.running` 由每 5s 一次的 `loadJobs()` 从 `/api/jobs` 拉，按 `workspace_id === t.id && !TERMINAL_STATUSES.has(status)` 聚合；nano-banana 的 `/api/jobs` list handler 是 Task 0 补的
- `_renderJobToDom(job)` 只写 `#sd-results`/`#nb-results`（结果面板）；events 靠 reactive `eventsText`（seedance 也写 `#sd-events` 是历史行为，nano-banana 不写）
- **老 localStorage 兼容**：首次 init 找不到 `<app>.tabs` 时用旧 `workspace_id` 键作为默认 tab id，历史 draft 不丢
- 最后 1 个 tab 不允许关；关有任务的 tab 弹确认 modal（`_closeConfirmTabId`）；modal-overlay 是 `position:fixed;inset:0;z-index:1000`，v-if 一定要正确控制

### 自签 HTTPS 下载

- **`<a href download>` 直接 click 会失败**：浏览器下载管理器把它当独立请求重新校验证书，自签容忍度比页面上下文严格 → Chrome 报「检查互联网连接」
- **修复模式**：`fetch(url) → resp.blob() → URL.createObjectURL(blob) → <a href="blob:..." download>`（blob: 协议绕过下载管理器）
- 已修位置：`portal/static/app.js`、`seedance/static/app.js`、`nano-banana/static/app.js` 的 `_blobDownload`
- 副作用：整个文件读入内存，长视频（几十-上百 MB）需监控

### Portal 下载代理链路

- 链路：iframe `<a download>` → `GET /<app>/api/download/{token}` → Portal `_proxy()` → 子应用 `FILES.get(token)` → `Content-Disposition: attachment` → 文件字节
- **双层缓冲瓶颈**：`_proxy()` 用 `resp.read()` 读完整个响应体后才转发，子应用 `path.read_bytes()` 也整读；50-200MB 视频会在内存里出现两份
- 2026-06-18 流式代理尝试失败：`http.server` 不是为流式响应设计的（`send_response`/`send_header` 追加 `_headers_buffer`，`wfile` 在 HTTPS 下是 SSL-wrapped，flush 行为不可控）
- 三个文件端点区别：`/api/media/*` 和 `/api/preset-media/*` 无 `Content-Disposition` + `Cache-Control: no-store`；`/api/download/*` 有 `attachment` 但**缺** Cache-Control

### Seedance 素材引用（provider=volcengine）

- **image/***：返回 `data:<mime>;base64,<...>` data URL；**不要**改走 `/files`，会被 generation tasks 以 `content[1].image_url.url is empty` 拒
- **video/*、audio/***：**不能上传本地文件**。Ark Files API `/api/v3/files/{id}/content` 对 Bearer 用户返 404 InvalidAction；`asset://` 需 SigV4 OpenAPI（Bearer key 用不了）
- 唯一可行：让用户在 JSON API 提交 `media.<field>.url`（公网 https），代码通过 `external_urls`（VALUE_FIELDS 里的 JSON 字符串）透传给 `build_payload`
- 本地上传（multipart）走视频/音频会触发 `RuntimeError` 提示切 t8star 或自托管 URL
- t8star 兼容 `/v1/files` 全媒体上传都 work，这条规则只针对 volcengine

### Dreamina 双守卫陷阱

- 有**两层独立 admin 守卫**：后端 `_is_admin(X-Is-Admin)` + 前端 `v-if="isAdmin"`
- 新端点必须两层都过一遍，否则出现「后端开放但前端不显示」或「前端显示但点了 403」
- **admin-only 颗粒度到按钮**，不要包整个区块，普通用户要能看到「能做的事」+「不能做的事变灰」
- 前端所有 `api()` 调用方必须检查 `res?.ok`，不能假设乐观更新成功（否则 UI 显示切换成功但后端没变 → 任务用错账号 → CreditPreDeductNotEnough）
- 当前放权：列账号/切 active/改调度模式/刷新余额/登录/登出**开放**；添加/删除/重命名/更新 CLI/install-cli 仅 admin；目录相关仅本机

### Dreamina 前端格式双兼容

- Dreamina 有**两套前端**：独立前端（`dreamina/static/app.js`）发 JSON，Portal 前端（`portal/static/app.js`）发 FormData（multipart）
- Handler 必须双兼容：参照 `handle_preset_save` 检测 Content-Type 分流
- 字段名也要兼容：Portal FormData 用 `archive_name`，独立前端 JSON 用 `name`
- seedance/nano-banana 无此问题（两套前端都发 FormData）
- **生产用户看到的是 Portal 前端**（Portal 原生 Vue 组件，不是 iframe；seedance/nano-banana 才是 iframe）——改 dreamina UI 前先确认改的是 `portal/static/app.js` 还是 `dreamina/static/app.js`。直连 8888 的独立前端正常没人访问，改错位置会「代码改了但用户没反应」
- **媒体 URL 前缀**：Portal 前端里 dreamina 视频/图片 src 拼成 `/dreamina/outputs/xxx`，走 Portal `_proxy` 转发到 dreamina 8888 的 `serve_file`；独立前端拼 `/outputs/xxx`（走 dreamina 后端直接 dispatch）——两种都过 `serve_file`，Range 支持是必须的（视频 `<video>` 元素需要 Range 拿 metadata 才能画首帧）
- **`<video>` preload 陷阱**：默认 `preload="none"` = 灰底占位，浏览器不会 fetch metadata；缩略图预览要写 `preload="metadata" muted playsinline`；`<img>` 无此问题

### Seedance 提示词优化

- `POST /api/optimize-prompt` 走 DeepSeek `deepseek-chat` + `seedance/SKILL.md`（229 行）作 system prompt
- **DeepSeek API Key 硬编码在 `app.py` 顶层常量 `DEEPSEEK_API_KEY`**，不进 `providers.json`（后者通过 `/api/config` 暴露给前端）
- `SEEDANCE_SKILL` 模块加载时读入内存，启动后不再读文件
- SKILL.md 末尾追加了「非交互模式」指令禁止 DeepSeek 反问
- 前端用正则只提取「优化后提示词」段，丢弃附录
- `.optimizeResult pre` 需显式覆盖全局 `pre { background: #101828 }`，否则黑底黑字

### Volcengine Portrait 子应用要点

- **ProjectName 硬编码 `Seedance2.0`**（所有 Action 无例外），`handle_virtual_groups_post` 移除了从请求体覆盖能力
- **真人认证是控制台流程，没有 API**：真人和虚拟素材最终都是 `asset://` 引用，Real handler 全部委托给 Virtual handler
- **Ark Files API `purpose` 只接受 `user_data` 或 `agent`**（`private-avatar` 会 400；旧文档写错了）
- CreateAsset 需要**公开可访问的 HTTP/HTTPS URL**，Ark v3 上传后返回的 URL 需 Bearer Token → TOS 后端拉不到 → 走 `_upload_to_public_host()` 传 uguu.se
- **Portal 需 do_DELETE + Access-Control-Allow-Methods: DELETE**（SimpleHTTPRequestHandler 默认不支持 DELETE，返 501 HTML 会让前端解析失败）
- Windows `cgi.FieldStorage` 必须显式传入 `CONTENT_LENGTH` 到 environ
- SK 是原始值，**不做 base64 解码**，`_normalize_sk() = return raw_sk`

### 端口冲突（release 打包）

- seedance / nano-banana **release zip 曾共用 8787-8899 窗口**，Windows 用户 seedance 打开命中 nano-banana Tab
- 加新子应用时**务必**给每个 100 端口独立窗口：.bat 的 `for($p=...;$p -le ...)` 与 app.py 的 `os.environ.get("PORT", "...")` 默认值同一窗口且互不重叠
- 改完必须**重新打包 release/*.zip**，否则 Windows 用户拿到的还是旧版

### 存档 CRUD（PetiteVue v-model + v-for select 陷阱）

- 删除存档后 `selectedArchive` 不显式重置，浏览器自动选第一个但 Vue 数据仍指向已删除值 → 「读取」发送不存在的名字 → 400
- 修复模式（4 个函数 × 3 个子应用都改）：`loadArchives()` 校验 selected 是否还在列表；`saveArchive()` 后刷新并显式选中新存档；`loadArchive()` 加空值防御；`deleteArchive()` 加 `confirm()`、删后刷新+重置

### 通用调试直觉

- 「重启后仍报旧 bug」→ 先查旧进程是否被杀、端口是否释放、进程启动时间是否晚于代码修改时间
- 错误日志中的代码**行号和当前代码对不上**，说明在跑旧代码
- Portal 代理返回但端口 PID 早于 Portal 启动时间 → 孤儿子进程，必须 `kill -9` 清端口
- 换 IP / 换 LAN 后 HTTPS 拒连 → 删 `portal/state/portal.pem`+`.key` 让 `ensure_certs()` 重生
- 前端所有 fetch/api 调用检查 `res.ok`，别乐观更新

## 外部 API 参考

### 火山方舟私域虚拟人像 Asset API

- 端点：`https://ark.cn-beijing.volcengineapi.com/?Action={Action}&Version=2024-01-01`
- 鉴权：AK/SK **SigV4**（非 Bearer），Service=`ark`、Version=`2024-01-01`、Region=`cn-beijing`
- 所有请求 POST + `Content-Type: application/json`
- 10 个 Action：CreateAssetGroup / CreateAsset / GetAsset / ListAssets / ListAssetGroups / GetAssetGroup / UpdateAsset / UpdateAssetGroup / DeleteAsset / DeleteAssetGroup
- 素材状态：Processing（继续轮询）/ Active（可用）/ Failed
- ListAssets `Filter` 有效字段：GroupIds、GroupType、Statuses、Name（模糊）— **不含 AssetType**
- 图片限制：jpeg/png/webp/bmp/tiff/gif/heic；宽高比 (0.4, 2.5)；尺寸 (300, 6000)px；<30MB
- 视频生成引用：`asset://<asset_ID>`，多图 content 数组顺序 = text 在前 + image_url 依次 role=`reference_image`；prompt 用「图片1」「图片2」指代
- IAM 权限：`ark:*Asset*`
- 详细 body/response 字段：见项目内 `docs/` 或 `volcengine-portrait/` 实现

### Ark Files API（临时图片）

- `POST https://ark.cn-beijing.volces.com/api/v3/files`，Bearer Token，multipart，`purpose=user_data`
- 返回 `{"id": "file-xxx"}`，URL 形式 `https://ark.cn-beijing.volces.com/api/v3/files/{id}/content`（需 Bearer）
- 仅图片可用；视频/音频端点在 Bearer 下返 404 InvalidAction

## 回答语言

用中文回答用户问题。
