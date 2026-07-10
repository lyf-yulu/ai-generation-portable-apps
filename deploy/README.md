# 部署 v2 到生产 —— 操作手册

本目录包含把 v2 切换到生产的脚本，以及不同粒度的回退方式。

## 目录

- [做什么用](#做什么用)
- [切换前检查](#切换前检查)
- [切换到生产：deploy.sh](#切换到生产deploysh)
- [两种回退方式](#两种回退方式)
- [常见问题](#常见问题)
- [文件对应表](#文件对应表)

---

## 做什么用

**v2 相对老版本的进步**：

| 类别 | 老版本 | v2 |
|------|-------|-----|
| 路径穿越漏洞（能读服务器任意文件） | 有 | 已修 |
| 上传文件大小上限 | 无（可 OOM） | 200 MB |
| SVG-伪装-JPEG XSS | 有风险 | 上传时用文件头校验，nosniff 全站保护 |
| CORS 反射任意 Origin | 是（有 CSRF 风险） | 白名单 |
| dreamina install-cli 供应链攻击 | curl \| bash 无校验 | SHA-256 校验 |
| Portal 和子应用之间的信任 | X-Is-Admin 是明文头 | HMAC 签名 |
| 下载/预览重复请求 | 每次都读全文件到内存 | ETag/304 缓存 |
| 视频拖进度条 | 部分浏览器卡顿 | Range 支持完整 |
| 图片缩略图 | 前端加载全尺寸原图 | Pillow 生成 WebP 缩略（可 70× 压缩流量）|
| HEIC 支持 | 部分浏览器不识别 | 服务端 pillow-heif 解码 |
| EXIF/GPS 隐私 | 上传原图带 GPS 坐标 | Pillow 自动剥离 |
| HTTP 层 | Python stdlib http.server（`cgi` 3.13 被移除） | FastAPI + uvicorn |

## 切换前检查

在跑 `deploy.sh` 前手工确认：

1. **同事此刻是否在用？** 切换有 ~10 秒断服，选个空闲时段（下班后/周末）。
2. **v2 目录已就绪**：`/Users/260413a/ai-generation-portable-apps-v2` 存在且 `.venv/bin/uvicorn` 可执行。
3. **依赖已装**（一次性）：
   ```bash
   cd /Users/260413a/ai-generation-portable-apps-v2
   DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
     .venv/bin/pip install -r requirements.txt
   ```
4. **测试跑过一遍**：
   ```bash
   bash "Start Test.command"   # 起测试环境到 9190/8788/8798/8890/8892
   # 浏览器打开 https://127.0.0.1:9190 手动验证
   bash "Stop Test.command"    # 停
   ```

## 切换到生产：deploy.sh

```bash
bash /Users/260413a/ai-generation-portable-apps-v2/deploy/deploy.sh
# 会问 "输入 'yes' 继续"
```

**做了什么**：

1. `launchctl unload` 停 launchd 守护（否则 KeepAlive 会拉起老进程抢端口）
2. 把老目录 mv 成 `ai-generation-portable-apps-backup-YYYY-MM-DD-HHMM`（**代码备份**）
3. 把 v2 目录 mv 到生产路径 `ai-generation-portable-apps`
4. 老 `state/` `outputs/` `archives/` `uploads/` 用**软链**从备份指回来（**25 GB 用户数据不复制**）
5. 备份原 plist 为 `com.ai-portal.plist.bak-YYYY-MM-DD-HHMM`
6. 生成新 plist：
   - Python 从 `/usr/bin/python3` (3.9) 换成 `/opt/homebrew/bin/python3.12`
   - 加 `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`（解决 Homebrew Python 3.12 的 pyexpat 兼容问题）
   - 加 `NANO_BANANA_ENGINE=fastapi` 和 `SEEDANCE_ENGINE=fastapi`（其他两个应用默认走 stdlib，因为它们同时用 bridge 加载 FastAPI 层——不改 plist 也会 FastAPI 化）
7. `launchctl load` 重启守护
8. 检查 6 个端口有 5 个 LISTEN + 打印子应用版本号

**切换后同事的感知**：

- 浏览器打开 https://192.168.30.5:9090 无变化
- 视频拖进度条更顺（Range 支持）
- 历史列表加载更快（ETag 304 + WebP 缩略图）
- 上传照片时 GPS 坐标被剥离（隐私改善）
- 之前上传的 25 GB 素材都在

## 两种回退方式

### 完整回退：rollback.sh

**用途**：v2 出严重问题，需要完全恢复到切换前状态。

```bash
bash /Users/260413a/ai-generation-portable-apps/deploy/rollback.sh
```

做的事：

1. 找最近一次 `-backup-...` 目录（也可以显式传 `rollback.sh /path/to/backup`）
2. 把当前 v2 mv 到 `ai-generation-portable-apps-v2-rollback-<日期>`（保留，以后可再切回来）
3. mv 备份到生产路径
4. 恢复 plist 备份
5. 重启 launchd

数据不会丢——`state/outputs/archives` 一直是软链到备份目录里的数据。

### 选择性回退：rollback-to-tag.sh

**用途**：v2 里某个 tag（比如 v2.3 dreamina fastapi）出问题，只想退回到 v2.2（volcengine 前的状态），保留其他修复。

```bash
# 列出所有 tag
bash /Users/260413a/ai-generation-portable-apps/deploy/rollback-to-tag.sh

# 切到某个 tag
bash /Users/260413a/ai-generation-portable-apps/deploy/rollback-to-tag.sh v2.2-volcengine-fastapi

# 再切回最新
cd /Users/260413a/ai-generation-portable-apps && git checkout main
```

**tag 一览**：

| Tag | 内容 | 建议何时用 |
|-----|------|----------|
| `v2.0-nano-banana-fastapi` | 阶段 1 六个安全 fix + nano-banana FastAPI 迁完 | 只想要安全 fix，撤所有 FastAPI 迁移 |
| `v2.1-seedance-fastapi` | +seedance FastAPI | dreamina/volcengine 有问题，撤这两 |
| `v2.2-volcengine-fastapi` | +volcengine FastAPI | dreamina 单独有问题 |
| `v2.3-dreamina-fastapi` | +dreamina FastAPI（当前 HEAD） | 全部迁完 |

**注意**：选择性回退用 `git checkout` 切代码，需要生产目录本身是 git 仓库（`deploy.sh` 会保留 v2 的 .git 目录）。

## 常见问题

### 切换后同事登录不了

老 sessions.json 里的 cookie 可能因为 secret 变了失效。让同事重新登录一次即可。数据没丢。

### 磁盘满了

只有代码（~18 MB）和 venv（~64 MB）新增；备份目录是 mv 不是 copy，不占额外空间。25 GB 用户素材原地不动。

### uvicorn 起不来

看 `~/Library/Logs/ai-portal.err` 和 `/Users/260413a/ai-generation-portable-apps/portal/state/logs/*.log`。最常见原因：

- **`ImportError: pyexpat`**：plist 里没设 DYLD_LIBRARY_PATH，或 Homebrew expat 未装（`brew install expat`）
- **`ModuleNotFoundError: No module named 'fastapi'`**：venv 没装依赖，跑一次 `pip install -r requirements.txt`
- **端口被占**：`launchctl unload` 后有僵尸子进程，`lsof -ti:9090 | xargs kill -9`

### 我想改 v2 代码继续开发

不推荐直接改生产目录。改回 v2 目录：

```bash
# 备份目录本身也是 git 仓库（原来的），可以在里面继续开发
cd /Users/260413a/ai-generation-portable-apps-backup-<日期>
# ...

# 或者从生产 clone 出一个新工作区
git clone /Users/260413a/ai-generation-portable-apps ~/ai-dev
```

### 想让 nano-banana 用 stdlib（回退单个应用的 FastAPI 化）

改 plist 里对应的 env：

```bash
# 编辑 plist
sudo plutil -replace EnvironmentVariables.NANO_BANANA_ENGINE -string stdlib \
  ~/Library/LaunchAgents/com.ai-portal.plist
launchctl kickstart -k gui/$(id -u)/com.ai-portal
```

同理 `SEEDANCE_ENGINE` / `DREAMINA_ENGINE` / `VOLCENGINE_PORTRAIT_ENGINE`。

## 文件对应表

生产 `/Users/260413a/ai-generation-portable-apps/`：

```
portal/app.py                       # Portal 反代 + auth，用 stdlib
seedance/app.py                     # 老 stdlib 版本，作为 fallback
seedance/app_fastapi.py             # FastAPI 版，SEEDANCE_ENGINE=fastapi 时启用
nano-banana/app.py                  # 同上
nano-banana/app_fastapi.py          # 同上
dreamina/app.py                     # 同上
dreamina/app_fastapi.py             # 同上
volcengine-portrait/app.py          # 同上
volcengine-portrait/app_fastapi.py  # 同上
.venv/                              # Python 3.12 + fastapi/uvicorn/pillow 等
requirements.txt                    # pip 依赖清单
deploy/                             # 本目录: deploy.sh / rollback.sh / rollback-to-tag.sh / README.md
```

生产 `~/Library/LaunchAgents/com.ai-portal.plist` 决定 Portal 启动方式（Python 3.12 + DYLD + engine env）。
