# 每日 CSV 报表 → DeepSeek 洞察 → 飞书群机器人 卡片推送

> 日期：2026-07-01
> 仓库：`/Users/260413a/ai-generation-portable-apps/`
> 状态：等 review

## Goal

每天 09:05 自动把「昨日 portal 使用情况」生成 CSV 明细 + DeepSeek 生成的自然语言洞察，通过飞书自定义群机器人 Webhook 以交互卡片形式发到运营群。卡片里带一个「下载 CSV」按钮指向 Portal 的鉴权下载端点。同时在 Portal 运维界面加「立即发送 / 预览」按钮，方便手动触发和调参。

## Architecture

三个新组件挂在 portal 进程内，无新增外部依赖（继续 Python stdlib）：

- **`portal/daily_report.py`**：数据聚合 + CSV 生成 + DeepSeek 调用 + 飞书 webhook 发送。纯逻辑模块，可独立 `python3 -m portal.daily_report --date 2026-06-30 --dry-run` 跑。
- **portal/app.py** 新增三个 HTTP 端点 + 一个后台调度线程 + `state/logs/usage-YYYY-MM-DD.jsonl` append-only 落盘钩子。
- **portal/static/index.html + app.js** 运维 tab 加一个「飞书日报」小面板：webhook URL 配置、立即发送、日期选择、预览卡片 JSON。

**关键取舍**：
- 卡片骨架代码写死，LLM 只填「趋势 / 异常 / 建议」三段文字洞察 → 避免 LLM 幻觉破坏卡片 JSON 结构、避免飞书渲染失败。
- 数据真相源改为 `state/logs/usage-YYYY-MM-DD.jsonl` 按日切片（append-only，保留 30 天），不再依赖 `records[]` 滑动窗口。原 `UsageTracker` 逻辑不动，只额外写一份 jsonl。
- 调度不用 launchd 单独挂新 plist——portal 已经 KeepAlive 常驻，进程内起一个 daemon 线程每分钟检查「是否到该发的时点且今日未发」即可。省一个 plist、免维护。

## Tech Stack

- Python stdlib：`csv`, `json`, `urllib.request`, `hmac`, `hashlib`, `base64`, `threading`, `datetime`
- 已有：`DeepSeek Chat API`（`seedance/app.py` 复用调用模板）、`portal.UsageTracker`、`portal.AuthManager`
- 飞书自定义群机器人 Webhook 协议：<https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot>

---

## 组件详细设计

### 1. 数据源：日切 usage 明细

**问题**：`UsageTracker.record()` 现在 `records[]` 上限 2000（超了裁到 1000），一天 1099 条已经踩线，第二天早上跑「昨日报表」时前一天的明细可能已被截断。

**方案**：`UsageTracker.record()` 里额外 append 一行 JSON 到 `state/logs/usage-YYYY-MM-DD.jsonl`（当日日期）。

- append-only，一行一记录，追加不重写，写失败静默降级（不影响主 usage.json 落盘）
- 每次写入前判断当前日期，跨天自然切到新文件
- 保留 30 天：每次跨天时清理 30 天前的旧 jsonl（best-effort，失败不 raise）
- 已有的 `records[]` 内存滑动窗口维持原样（用于运维界面「最近 20 条」实时展示），互不影响
- 主端点从 jsonl 读取，天然按天分片

**文件结构**：
```
portal/state/logs/
  usage-2026-06-30.jsonl   # 一行 = JSON 一条 record（time/app/ip/username/method/path）
  usage-2026-07-01.jsonl
  ...
```

**兜底**：如果 `state/logs/usage-YYYY-MM-DD.jsonl` 不存在（例如系统首次启用新代码时），回落到从 `usage.json.records[]` 过滤指定日期的记录，功能不阻塞，只是明细可能不完整（会在报表 footer 提示「明细可能截断」）。

### 2. CSV 生成

**列**：`timestamp, app, username, ip, method, path, event_type`

- `event_type` 是派生列，值域 `submit_job / poll / download / upload / login / other`。派生逻辑：
  - `POST /api/jobs` 或 `POST /api/virtual/jobs` → `submit_job`
  - `GET /api/jobs/*` 或 `GET /api/virtual/jobs/*` → `poll`
  - 路径含 `/api/download/` → `download`
  - 路径含 `/api/upload` → `upload`
  - `POST /api/login` → `login`
  - 其他 → `other`

**输出**：`portal/state/reports/YYYY-MM-DD.csv`。UTF-8 with BOM（`﻿` 前缀），Excel 中文不乱码。

**规模**：一天 1099 条 → CSV 约 100KB，飞书链接下载零压力。

### 3. 数据聚合（写死代码，不走 LLM）

生成用于卡片渲染 + LLM 洞察输入的 dict：

```python
{
    "date": "2026-06-30",
    "total_events": 1099,
    "by_app": {
        "nano-banana": {"requests": 552, "submits": 11, "downloads": 46, "users": 4},
        "seedance":    {"requests": 352, "submits": 0,  "downloads": 11, "users": 3},
        ...
    },
    "by_user": [  # 按 submits 降序，取前 10
        {"username": "高大王", "submits": 8, "downloads": 13, "apps": ["nano-banana","volcengine-portrait"]},
        ...
    ],
    "hourly": [0]*24,  # 24 长度数组，按小时的事件总数
    "peak_hour": 14,
    "unique_users": 7,
}
```

### 4. DeepSeek 洞察

**入参**：把上面 dict 精简后（去掉 IP、去掉 path 明细，仅保留统计块）拼进 prompt。

**system prompt**：
```
你是一个数据分析助理。给你一份 AI 生成工具的日使用统计（含各子应用请求量、用户活跃度、时段分布），
请输出严格 JSON，结构：
{
  "trend": "一句话，描述整体情况（20-40 字）",
  "highlight": "一条最值得注意的现象（30-50 字，正面/负面均可）",
  "suggestion": "一条给运营的建议（30-50 字，可执行）"
}
只输出 JSON，不要 markdown 代码块。所有字段必须存在。
```

**model**：`deepseek-chat`，`response_format={"type":"json_object"}`，`temperature=0.4`。

**失败降级**：DeepSeek 调用失败（网络、限流、JSON 解析失败）→ 用固定占位文本继续发卡片，日志记 WARN。绝不因为 LLM 挂了就不发。

### 5. 飞书卡片 JSON（骨架写死）

**卡片布局**：
```
┌────────────────────────────────────────┐
│ AI 工具日报 · 2026-06-30       [标题条] │
├────────────────────────────────────────┤
│ 总请求 1,099    活跃用户 7             │
│ 提交任务 11     下载 60                │
├────────────────────────────────────────┤
│ 各应用                                  │
│   nano-banana    552 请求 · 4 用户     │
│   seedance       352 请求 · 3 用户     │
│   volcengine-p.  108 请求 · 2 用户     │
│   dreamina        87 请求 · 1 用户     │
├────────────────────────────────────────┤
│ Top 用户（按提交数）                    │
│   高大王   8 提交 · 13 下载            │
│   苏湘     2 提交 ·  3 下载            │
│   ...                                   │
├────────────────────────────────────────┤
│ 💡 洞察                                 │
│ 趋势：<LLM trend>                      │
│ 关注：<LLM highlight>                  │
│ 建议：<LLM suggestion>                 │
├────────────────────────────────────────┤
│ [ 📥 下载 CSV 明细 ]                   │
│ 由 Portal 自动生成 · 09:05 UTC+8       │
└────────────────────────────────────────┘
```

用飞书 `interactive card v2` schema（`schema: "2.0"`）。按钮 `action.url` 指向 `https://<lan-ip>:9091/api/reports/daily/2026-06-30.csv`。

- `<lan-ip>` 从 `get_lan_ip()` 拿；配置里可覆盖（`state/feishu.json.portal_base_url`）
- 用户点按钮 → 浏览器打开 → Portal 检查 session cookie → 无 cookie 跳登录页 → 登录后自动下载
- 卡片对 admin 有意义，普通同事点按钮会 403（这一层用 admin_only）

### 6. Portal 新增端点

| 路径 | 方法 | 权限 | 用途 |
|---|---|---|---|
| `/api/reports/daily/{date}.csv` | GET | 已登录（admin only） | 下载指定日期 CSV。生成后缓存到 `state/reports/`，命中即返回。 |
| `/api/reports/send` | POST | admin only | body: `{"date": "2026-06-30"}`。立即发送指定日期日报到飞书。返回 `{ok, feishu_status}`。 |
| `/api/reports/preview` | POST | admin only | body: `{"date": "..."}`。返回卡片 JSON + 洞察 JSON，不真发。前端预览用。 |
| `/api/feishu/config` | GET/PUT | admin only | 读写 `state/feishu.json`：`{webhook_url, sign_secret, schedule_time, portal_base_url}`。secret 返回时掩码。 |

### 7. 调度：进程内 daemon 线程

`daily_report.py` 起线程，每 60 秒检查：

```python
def scheduler_loop():
    while True:
        cfg = load_config()  # 每次读取，改配置即时生效
        if not cfg.get("enabled"): time.sleep(60); continue
        now = datetime.now()
        # 判断当前时点是否 == schedule_time（默认 09:05），且今日未发
        if now.strftime("%H:%M") == cfg.get("schedule_time", "09:05"):
            today_str = now.strftime("%Y-%m-%d")
            marker = STATE_DIR / f"reports/.sent-{today_str}"
            if not marker.exists():
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                try:
                    send_daily_report(yesterday)
                    marker.touch()
                except Exception as e:
                    log_error(e)
                    # 不 touch marker，下次循环还会重试 → 加节流：3 次失败后 touch marker 停止今日重试
        time.sleep(60)
```

`.sent-YYYY-MM-DD` 标记文件保证同日不重发。手动通过 `/api/reports/send` 触发时**不**建 marker，允许当天多次手发调试。

### 8. 飞书 Webhook 签名（可选，如果 URL 开了签名校验）

按飞书文档：
```python
def sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")
```

请求 body 顶层加 `timestamp` 和 `sign` 字段。`sign_secret` 空字符串则跳过签名。

### 9. 前端运维面板

在 portal 现有 admin tab（运维监控）里加折叠面板「飞书日报」：

- Webhook URL input（保存到 `state/feishu.json`，掩码显示 `https://open.feishu.cn/****xxxx`）
- 签名 secret input（可选，掩码）
- 触发时间 input（HH:MM，默认 09:05）
- Portal 基础 URL input（默认自动 `https://<lan-ip>:9091`）
- `[启用定时]` toggle
- `[今日预览]` 按钮 → 弹卡片 JSON preview + 洞察文本
- `[立即发送昨日]` 按钮 → 发起 `/api/reports/send`，返回 toast

## 数据流

```
09:05 ─┐
       │
       ▼
scheduler_loop → send_daily_report("2026-06-30")
                    │
                    ├─→ 读 state/logs/usage-2026-06-30.jsonl
                    ├─→ 聚合 → agg dict
                    ├─→ 写 state/reports/2026-06-30.csv
                    ├─→ 调 DeepSeek → insight JSON（失败降级）
                    ├─→ 组装 card JSON（骨架 + 数据 + insight）
                    ├─→ 计算签名（如启用）
                    ├─→ POST → webhook URL
                    └─→ 落 marker
```

## 错误处理

| 场景 | 行为 |
|---|---|
| `state/logs/usage-YYYY-MM-DD.jsonl` 不存在 | 回落到 usage.json.records[] 过滤，footer 加提示 |
| DeepSeek 失败 | 用占位洞察发卡片，日志 WARN |
| Webhook 请求失败 | 记 ERROR，不 touch marker（下轮重试；连败 3 次触发熔断，当日不再重试） |
| CSV 端点访问：非 admin | 403 + 明确错误消息 |
| CSV 端点访问：文件不存在 | 现场生成（同 send 时的逻辑），生成后返回；日期在未来 → 400 |
| 卡片 JSON 太大 | 用户列 top 10、path 明细完全不进卡片（都在 CSV 里）→ 卡片 body 稳定在几 KB 内 |

## 安全考虑

- Webhook URL 和 sign_secret 存 `state/feishu.json`（已经在 gitignore 白名单外，不入库）
- CSV 端点必须 admin 校验：包含 IP、username 明细，普通同事不该看
- 卡片按钮 URL 用 https + LAN IP：外网访问不到，即使 webhook URL 泄露，攻击者点开也拿不到 CSV
- 飞书返回的 error code 完整记 log 便于排查（0 = 成功，19024 = 签名错误等）

## 受影响文件

| 文件 | 改动 |
|---|---|
| `portal/daily_report.py` | **新建**：~300 行，聚合 + LLM + 飞书发送 + 调度线程 |
| `portal/app.py` | +80 行：4 个新端点、`UsageTracker.record` 增加 jsonl append、启动时 spawn `daily_report.scheduler_loop` |
| `portal/static/index.html` | +40 行：运维 tab 新面板 |
| `portal/static/app.js` | +80 行：新面板逻辑 |
| `portal/state/feishu.json` | **新建**（首次访问 `/api/feishu/config` 时创建默认） |
| `portal/state/logs/usage-*.jsonl` | **新建**（运行时产生） |
| `portal/state/reports/*.csv` | **新建**（运行时产生） |

不动：其他子应用（seedance/nano-banana/dreamina/volcengine-portrait）、其他 portal 现有功能。

## 稳定性保护

- daemon 线程 exception 全 catch，不让崩溃传播到 portal 主进程
- feishu.json 缺失 / 格式坏 → 视作 `enabled=false`，静默不发；界面提示「未配置」
- jsonl 写入 IOError → 静默 skip（主 usage.json 保存不受影响）
- 端点上必须 admin 检查（沿用 `role != "admin" → 403` 模式）

## 验证步骤

1. 部署 `daily_report.py` + portal.app.py 改动 + 前端改动
2. kill portal pid → launchd 重拉
3. 访问 `https://<lan>:9091` → 运维 tab → 飞书日报面板
4. 填 webhook URL（先创建一个测试群机器人）→ 保存
5. 点「今日预览」→ 校验卡片 JSON 结构 + 洞察文本 OK
6. 点「立即发送昨日」→ 群内看到卡片 → 点「下载 CSV」按钮 → 浏览器登录 → 拿到 csv
7. 用 Excel 打开 CSV：中文正常、列齐全、events 数量对得上
8. 手动改 `state/feishu.json.schedule_time = "HH:MM+2min"` 等到时点，看是否自动发
9. 验证 sent-marker：同日再等一分钟不重发；下一天到点重新发

## 提交策略

一个 commit 涵盖所有改动：
`feat(portal): daily usage CSV + LLM insight card to Feishu bot`

如果新建文件太多需要拆，可分两个：
- `feat(portal): add daily_report module (aggregation + LLM + Feishu webhook)`
- `feat(portal): expose /api/reports and /api/feishu endpoints + admin UI panel`

## Open questions（留给 review 时确认）

1. `schedule_time` 默认 09:05 是否合适？（用户没指定，凭 09:00 会撞一堆整点触发的 job）
2. 卡片按钮点开是 Portal HTTPS，同事第一次访问会遇到自签证书警告——是否需要卡片文案里加一句「浏览器提示不安全时点继续访问」？
3. 是否需要在报表里加「与前一日对比」的百分比数字？（我倾向不加：首日无对比数据、复杂度上升；LLM 的 highlight 可以自然覆盖）
4. 是否需要 dry-run 模式的 CLI？（我倾向加一个 `python3 -m daily_report --date X --dry-run` 便于本地调 prompt，10 行代码的事）
