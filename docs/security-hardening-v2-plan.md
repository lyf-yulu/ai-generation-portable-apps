# 安全化改造计划 v2（2026-07-08）

> **目标**：在独立目录 `/Users/260413a/ai-generation-portable-apps-v2` 完成安全修复 + 框架升级，
> 用测试端口验证后直接替换生产，不影响同事局域网访问体验。

## 背景

三方面驱动：
1. **当前已知安全隐患**（review agent 于 2026-07-08 发现）
2. **`cgi` 模块 Python 3.13 起被移除**，迟早要换
3. **项目可能上公网**（让外部客户用域名访问），届时这些隐患是 Blocker

旧约束「No pip dependencies — pure stdlib」是误解：后端只在服务机上跑，客户端只用浏览器，
可以自由引入第三方库。

---

## 阶段 1 — 透明安全修复（不迁框架，优先做）

这批改动对同事完全无感知，只改后端逻辑，不动前端、不动启动脚本、不需要 pip install。

### 1a. 路径穿越校验（Path Traversal）— Blocker

**位置**：`dreamina/app.py:2635-2691`、`seedance/app.py:1827`、`nano-banana/app.py:1466`、`volcengine-portrait/app.py` 的 `serve_file`/`_serve_file`

**问题**：`base_dir / urllib.parse.unquote(rel_path)` 没做 `resolve().is_relative_to(base_dir)` 校验，已登录用户可构造 `../../etc/passwd` 读任意文件。

**修法**：每个 serve_file 在返回文件前加：
```python
real = (base_dir / rel_path).resolve()
if not real.is_relative_to(base_dir.resolve()):
    # 返回 403
```

### 1b. X-Is-Admin 签名（HMAC）— Blocker 上公网

**位置**：`portal/app.py:1708`（注入方）、`seedance:248`、`dreamina:59`、`volcengine:478/1198`（验证方）

**问题**：明文 header，任何请求都可以自称 admin（目前靠 127.0.0.1 bind 兜住）。

**修法**：启动时生成共享随机 secret 写入 `portal/state/internal_secret`，Portal 注入时 HMAC-SHA256 签名
`X-Is-Admin` → `X-Admin-Token: <hmac>`，子应用验证 token。

### 1c. MIME 类型校验 + nosniff — Should-fix

**位置**：所有 `serve_file` 返回响应

**问题**：只按扩展名猜 MIME，用户传 `.jpg` 但内容是含 JS 的 SVG → inline 渲染即 XSS。
缺 `X-Content-Type-Options: nosniff`。

**修法**：
- 所有 `serve_file` 响应加 `X-Content-Type-Options: nosniff`
- 上传端点加 magic bytes 校验（文件头）；暂不引入 `python-magic`，用 `imghdr` 或自己读前 12 字节

### 1d. 上传大小限制 — Should-fix 上公网

**位置**：`portal/app.py:1664-1665`、`dreamina/app.py:1348`、所有 `do_POST` 开头

**问题**：`self.rfile.read(int(Content-Length))` 无上限，公网上可以 OOM 整台机器。

**修法**：`do_POST` 开头检查 Content-Length，超过 `MAX_UPLOAD_BYTES = 200 * 1024 * 1024`（200MB）返 413。

### 1e. CORS 白名单 — Blocker 上公网

**位置**：`portal/app.py:1827-1832` `_cors_headers()`

**问题**：`Origin` 原样回显 + `Allow-Credentials: true` = 跨域 API 读全部会话数据。

**修法**：改成从环境变量读 `ALLOWED_ORIGINS`（默认 `https://192.168.30.5:9090,http://127.0.0.1`）。
局域网同源访问根本不触发 CORS，无影响。

### 1f. dreamina install-cli — Should-fix 上公网

**位置**：`dreamina/app.py:1959-1961`

**问题**：`subprocess.Popen(["bash", "-c", "curl -fsSL https://... | bash"])`，上游被劫持即 RCE。

**修法**：改为 download → 校验 SHA256 → 执行三步流程。

---

## 阶段 2 — FastAPI + 第三方库迁移（pip install 一次，最大杠杆）

pip install 列表：
```
fastapi uvicorn[standard] httpx python-multipart pillow pillow-heif tenacity
```

### 2a. 替换 http.server + cgi 模块

- `cgi.FieldStorage` → Starlette `UploadFile`（spooled to disk，Python 3.13 兼容）
- `do_GET/do_POST if/elif 路由链`（合计 ~1100 行）→ FastAPI 装饰器路由
- `do_OPTIONS` 手写 CORS（4 份）→ `CORSMiddleware`

预期净减代码量：~700 行路由分发 + ~120 行 multipart 辅助函数。

### 2b. Portal 代理层升级

- `http.client.HTTPConnection`（无连接池）→ `httpx.AsyncClient`（连接池 + keep-alive）
- 请求体全量缓冲 → 流式上传透传
- Range 请求完全缺失 → `httpx` 透传 `Range`/`Content-Range`（视频拖进度条不再卡）

### 2c. 图像处理

- `Pillow`：生成 256px WebP 缩略图，节流前端加载流量
- `pillow-heif`：iOS HEIC 上传支持
- EXIF/GPS 剥离：上传时自动 strip（防止人像原图暴露拍摄地）
- `ffmpeg-python`（可选，后续再加）：视频首帧 poster 生成

### 2d. 外部 API 调用规范化

- `urllib.request` → `httpx`（`raise_for_status()` + 连接池）
- `time.sleep` 裸轮询 → `tenacity.retry` 装饰器（退避重试）
- Volcengine SigV4（`volcengine-portrait:128-209` + `550-620`，共 ~150 行）→ 抽成参数化 `sign_v4()` helper

### 2e. 启动方式变化

launchd plist 改 portal 启动命令：
```xml
<string>/opt/homebrew/bin/python3.12</string>
<string>-m</string><string>uvicorn</string>
<string>app:app</string>
<string>--host</string><string>0.0.0.0</string>
<string>--port</string><string>9090</string>
<string>--ssl-keyfile</string><string>portal/state/portal.key</string>
<string>--ssl-certfile</string><string>portal/state/portal.pem</string>
```

---

## 阶段 3 — 持久化 + 公网准备（上公网前做）

- **JOBS / DOWNLOAD_MAP 换 SQLite**（重启后任务历史/下载 token 不丢）
- **API key 加密存储**（Fernet + macOS Keychain 主密钥）
- **uguu.se 换自建 presigned URL**（volcengine-portrait 人像上传）
- **速率限制**（uvicorn `--limit-concurrency` + Starlette middleware）
- **Cloudflare/Nginx 反代 + 正式证书**（Let's Encrypt）

---

## 工作流程

```
旧项目（生产运行）         新项目（改造+测试）
  ├─ 端口 9090/8787...       ├─ 端口 9190/8788...（Start Test.command）
  └─ launchd 守护             └─ 手动 Start Test.command
                                    ↓ 验证通过
                              launchd kickstart 切到新目录（改 plist）
```

### 复制方法

只复制代码，排除所有生成数据（outputs/state/workspaces/archives/logs）：
```bash
rsync -av --exclude='outputs/' --exclude='state/' --exclude='archives/' \
  --exclude='uploads/' --exclude='logs/' --exclude='__pycache__/' \
  --exclude='*.pem' --exclude='*.key' --exclude='.git/' \
  /Users/260413a/ai-generation-portable-apps/ \
  /Users/260413a/ai-generation-portable-apps-v2/
```

新目录独立 git init，每个阶段一个 commit，方便回滚对比。

---

## 各阶段对同事的影响

| 阶段 | 测试期间 | 切换瞬间 | 切换后 |
|------|---------|---------|------|
| 阶段 1 | 老项目继续跑，同事无感知 | 重启 < 10s | 完全透明，行为不变 |
| 阶段 2 | 同上 | 重启 < 15s | 视频拖进度条更顺畅，图片加载更快 |
| 阶段 3 | 同上 | 重启 < 15s | 重启后任务历史保留（以前会丢）|
