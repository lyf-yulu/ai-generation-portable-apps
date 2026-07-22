# 从 GitHub clone 到别处运行（含搬运器）

本项目原本是单机 launchd 部署，clone 到别的机器/系统运行时，代码本身可移植
（各 app 用 `Path(__file__).parent` + `DATA_DIR` 环境变量定位数据，不绑死路径），
但**依赖、密钥、部署层**需要手动处理。按下面顺序做。

## 1. 装依赖（必须，否则子应用起不来）

FastAPI 引擎由 portal 通过 **`<repo根>/.venv/bin/uvicorn`** 启动，所以 venv
必须建在**仓库根目录**：

```bash
cd <repo-root>
python3 -m venv .venv                      # Python 3.11+（生产是 3.12）
.venv/bin/pip install -r requirements.txt
```

缺 `.venv` 或缺库时，portal 会 fallback 到 stdlib `app.py`，**丢失 FastAPI 引擎
的修复**（X-Job-Id 用量统计、分片上传等）。务必装好。

> **macOS Apple Silicon 专属**：若启动报 `import xml.parsers.expat` 失败，用
> `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib` 启动 uvicorn。**Linux 不需要**
> 这一步，删掉相关 env 即可。

## 2. 重建密钥/配置（gitignored，clone 不会带）

| 文件 | 来源 | 内容 |
|---|---|---|
| `seedance/state/secrets.json` | 抄 `seedance/secrets.example.json` | 火山方舟 `volcengine_api_key` |
| `volcengine-portrait/config.json` | 抄 `volcengine-portrait/config.example.json` | AK/SK、tos_bucket |
| `feishu-output-sync/config.json` | 抄 `feishu-output-sync/config.example.json` | 飞书 app_id/secret/folder_token |
| DeepSeek key | 设环境变量 `DEEPSEEK_API_KEY` 或建 `seedance/state/deepseek.key` | 提示词优化用 |
| `portal/state/users.json` | 首次启动后注册第一个用户即 admin | 无需手建 |

## 3. 启动

**开发/手动**：
```bash
cd portal && python3 app.py          # 绑 0.0.0.0:9090，自动拉起子应用
```

**生产守护**：
- **macOS**：改 `deploy/` 里的 plist 模板 —— 把所有 `/Users/260413a/...` 换成新路径、
  `/opt/homebrew/bin/python3.12` 换成本机 Python 路径，再 `launchctl load`。
- **Linux**：launchd 不存在，改用 **systemd unit** 或 supervisor。命令等价于
  `cd portal && <venv>/bin/python app.py`（或让 portal 用 stdlib，子应用走 uvicorn）。

## 4. 搬运器（feishu-output-sync）一起跑

搬运器**独立于主服务**，可单独部署。它是**纯 stdlib**（urllib + sqlite3），
不需要 requirements.txt 里的库，但要 Python 3.9+。

```bash
cp feishu-output-sync/config.example.json feishu-output-sync/config.json
# 填 app_id / app_secret / folder_token
```

- **飞书后台**：企业自建应用需开 `bitable:app`、`drive` 相关 scope（建表/上传/
  设权限），并发布版本。详见 `feishu-output-sync/README.md`。
- **config 路径可移植**：`outputs_roots` 用相对路径 `../seedance/outputs`，依赖
  搬运器在 `feishu-output-sync/` 目录下运行（plist 的 WorkingDirectory 保证）。
- **守护**：`feishu-output-sync/com.feishu-output-sync.plist` 里 3 处
  `/Users/260413a` + `/opt/homebrew/bin/python3.12` 要改成新路径。Linux 换 systemd。
- 它循环轮询（默认 300s），把各 app `outputs/<用户>/<日期>/` 的新产出增量搬进
  每人一张飞书多维表格（组织内可编辑）。幂等（SQLite 指纹去重）。

## 5. 平台相关硬编码清单（换机/换 OS 要改）

| 位置 | 硬编码 | 处理 |
|---|---|---|
| `*.command` 启动脚本 | `/opt/homebrew/bin/python3.12` 等候选路径 | 脚本已按候选列表回退，Linux 会落到 `/usr/bin/python3` |
| `com.ai-portal.plist` / 搬运器 plist | `/Users/260413a/...`、`/opt/homebrew/...` | 全部改成新路径；Linux 换 systemd |
| 3 个 `app_fastapi.py` 注释 | `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib` | 仅 macOS；Linux 忽略 |
| `portal/app.py` | lsof 候选路径、expat_lib | 已有多路径回退，一般无需改 |

## 6. 数据目录（可选迁移）

各 app 的 `outputs/`、`state/`、`archives/` 等默认在 `<app>/` 下（真实目录，
2026-07-22 起不再用软链）。如需把数据放到别处，设 `DATA_DIR` 环境变量即可
（`_DATA_BASE = os.environ.get("DATA_DIR", ROOT)`），outputs/state 会跟着走。

## 端口表

| App | 端口 |
|---|---|
| Portal | 9090（HTTP→HTTPS 跳转 9089） |
| Seedance | 8787 |
| Nano Banana | 8797 |
| Dreamina | 8888 |
| Volcengine Portrait | 8891 |

端口可用环境变量覆盖（`SEEDANCE_PORT`、`NANO_PORT` 等）。
