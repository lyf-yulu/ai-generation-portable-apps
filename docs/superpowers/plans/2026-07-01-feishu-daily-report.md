# 飞书每日报表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每天 09:05 自动把 portal 昨日使用日志聚合成 CSV + DeepSeek 生成洞察，以交互卡片形式经飞书自定义群机器人 Webhook 推送到运营群；卡片按钮跳转到 Portal 鉴权链接下载 CSV；Portal 运维面板可手动预览/立即发送。

**Architecture:** 新建 `portal/daily_report.py` 独立模块（数据聚合、CSV 生成、DeepSeek 调用、飞书卡片组装、Webhook 发送）；`portal/app.py` 里给 `UsageTracker.record` 加 jsonl 日切写入、加 4 个管理端点、启动时挂调度 daemon 线程；前端加折叠面板配置 + 预览 + 立即发送。数据源统一走 `state/logs/usage-YYYY-MM-DD.jsonl`（append-only, 30 天滚动），杜绝 `records[]` 1000 条滑动窗口的截断问题。

**Tech Stack:** Python stdlib（`csv`, `json`, `urllib.request`, `hmac`, `hashlib`, `base64`, `threading`, `datetime`, `pathlib`），已接入的 DeepSeek Chat API，petite-vue 前端。

---

## File Structure

**新建**：
- `portal/daily_report.py` — 数据聚合 + CSV + DeepSeek + 飞书发送 + 调度线程
- `tests/test_daily_report.py` — 单元测试
- `docs/superpowers/plans/2026-07-01-feishu-daily-report.md` — 本文档（已存在）

**修改**：
- `portal/app.py`
  - `UsageTracker.record`（~683 行附近）：额外写 jsonl 日切
  - 新增 `_daily_report_*` handler 方法
  - `do_GET` / `do_POST` 路由分发（1058, 1108 起）：接入 4 个新端点
  - `main()`（1660 起）：启动前 spawn `daily_report.scheduler_loop` daemon
- `portal/static/index.html` — stats tab 增加飞书日报面板（section）
- `portal/static/app.js` — StatsApp 数据/方法扩展

**运行时产物**（不入库）：
- `portal/state/feishu.json`
- `portal/state/logs/usage-YYYY-MM-DD.jsonl`
- `portal/state/reports/YYYY-MM-DD.csv`
- `portal/state/reports/.sent-YYYY-MM-DD`

---

### Task 1: usage.jsonl 日切 append-only 落盘

**Files:**
- Modify: `portal/app.py:678-689`（`UsageTracker.record`）
- Test: `tests/test_daily_report.py`（新建）

- [ ] **Step 1: 写失败测试 — jsonl 日切**

在 `tests/test_daily_report.py`：

```python
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PORTAL_APP = ROOT / "portal" / "app.py"

def load_portal_module(state_dir: Path):
    spec = importlib.util.spec_from_file_location("portal_app_under_test", PORTAL_APP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.STATE_DIR = state_dir
    module.USAGE_PATH = state_dir / "usage.json"
    return module


class UsageJsonlTests(unittest.TestCase):
    def test_record_appends_daily_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module = load_portal_module(base)
            tracker = module.UsageTracker()
            with mock.patch.object(module.time, "strftime", side_effect=lambda fmt: {
                "%Y-%m-%d": "2026-06-30",
                "%Y-%m-%d %H:%M:%S": "2026-06-30 09:00:00",
            }[fmt]):
                tracker.record("seedance", "10.0.0.1", "POST", "/api/jobs", "alice")

            jsonl = base / "logs" / "usage-2026-06-30.jsonl"
            self.assertTrue(jsonl.exists())
            lines = jsonl.read_text("utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row["app"], "seedance")
            self.assertEqual(row["username"], "alice")
            self.assertEqual(row["path"], "/api/jobs")

    def test_record_jsonl_write_failure_does_not_break_usage_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module = load_portal_module(base)
            tracker = module.UsageTracker()
            with mock.patch.object(module, "_append_usage_jsonl", side_effect=OSError("disk full")):
                tracker.record("seedance", "10.0.0.1", "POST", "/api/jobs", "alice")
            self.assertTrue((base / "usage.json").exists())
            data = json.loads((base / "usage.json").read_text("utf-8"))
            self.assertEqual(len(data["records"]), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
cd /Users/260413a/ai-generation-portable-apps
python3 -m unittest tests.test_daily_report -v 2>&1 | tail -20
```

Expected: `AttributeError: module ... has no attribute '_append_usage_jsonl'` 或 jsonl 文件不存在。

- [ ] **Step 3: 实现 `_append_usage_jsonl` helper + record 里调用**

在 `portal/app.py` 顶部常量区（`USAGE_PATH = STATE_DIR / "usage.json"` 附近）追加：

```python
LOGS_DIR = STATE_DIR / "logs"
USAGE_JSONL_RETENTION_DAYS = 30
```

在 `UsageTracker` 类**外面**（模块级函数）加：

```python
def _append_usage_jsonl(entry: dict, today: str):
    """Append a single usage entry to state/logs/usage-YYYY-MM-DD.jsonl.
    Failures are logged but do NOT propagate — primary usage.json save must not be blocked."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / f"usage-{today}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"  [usage] jsonl append failed for {today}: {exc}", flush=True)


def _prune_old_usage_jsonl(today: str):
    """Delete usage-*.jsonl older than USAGE_JSONL_RETENTION_DAYS. Best-effort."""
    try:
        if not LOGS_DIR.exists():
            return
        from datetime import datetime, timedelta
        cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=USAGE_JSONL_RETENTION_DAYS)
        for p in LOGS_DIR.glob("usage-*.jsonl"):
            try:
                d = datetime.strptime(p.stem[len("usage-"):], "%Y-%m-%d")
                if d < cutoff:
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass
```

修改 `UsageTracker.record`（现在 683 行附近），在 `self._save()` 之前 / 之后（顺序无所谓，写死锁外）加：

```python
def record(self, app: str, client_ip: str, method: str, path: str, username: str = ""):
    today = time.strftime("%Y-%m-%d")
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app, "ip": client_ip,
             "username": username, "method": method, "path": path}
    with self._lock:
        self._data["records"].append(entry)
        if len(self._data["records"]) > 2000:
            self._data["records"] = self._data["records"][-1000:]
        day_stats = self._data["daily"].setdefault(today, {})
        app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
        app_stats["requests"] += 1
        self._save()
    # Below the lock: jsonl append + prune are best-effort, must not block primary save
    try:
        _append_usage_jsonl(entry, today)
    except Exception:
        pass
    # prune once per day (cheap enough to attempt each write; glob is O(days))
    _prune_old_usage_jsonl(today)
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
python3 -m unittest tests.test_daily_report -v 2>&1 | tail -10
```

Expected: 2 tests OK.

- [ ] **Step 5: 语法自查**

```bash
python3 -c "import ast; ast.parse(open('portal/app.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
cd /Users/260413a/ai-generation-portable-apps
git add portal/app.py tests/test_daily_report.py docs/superpowers/plans/2026-07-01-feishu-daily-report.md docs/superpowers/specs/2026-07-01-feishu-daily-report-design.md
git commit -m "$(cat <<'EOF'
feat(portal): daily jsonl usage rotation for reliable report source

UsageTracker.record now also appends every entry to
state/logs/usage-YYYY-MM-DD.jsonl (append-only, 30 day retention).
This gives daily_report.py a truthful data source instead of the
1000-entry sliding window in records[].
EOF
)"
```

---

### Task 2: daily_report 数据聚合与 CSV 生成

**Files:**
- Create: `portal/daily_report.py`
- Test: `tests/test_daily_report.py`（追加）

- [ ] **Step 1: 追加失败测试 — load_events + aggregate + write_csv**

在 `tests/test_daily_report.py` 追加：

```python
def load_daily_report_module():
    spec = importlib.util.spec_from_file_location(
        "daily_report_under_test",
        ROOT / "portal" / "daily_report.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AggregationTests(unittest.TestCase):
    SAMPLE = [
        {"time": "2026-06-30 09:00:00", "app": "seedance",    "ip": "10.0.0.1", "username": "alice", "method": "POST", "path": "/api/jobs"},
        {"time": "2026-06-30 09:00:05", "app": "seedance",    "ip": "10.0.0.1", "username": "alice", "method": "GET",  "path": "/api/jobs/abc"},
        {"time": "2026-06-30 14:20:00", "app": "nano-banana", "ip": "10.0.0.2", "username": "bob",   "method": "POST", "path": "/api/jobs"},
        {"time": "2026-06-30 14:22:00", "app": "nano-banana", "ip": "10.0.0.2", "username": "bob",   "method": "GET",  "path": "/api/download/xyz"},
    ]

    def _write_sample_jsonl(self, base: Path, date: str, rows: list) -> Path:
        logs = base / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        p = logs / f"usage-{date}.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return p

    def test_load_events_from_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_sample_jsonl(base, "2026-06-30", self.SAMPLE)
            mod = load_daily_report_module()
            events, source = mod.load_events(base, "2026-06-30")
            self.assertEqual(len(events), 4)
            self.assertEqual(source, "jsonl")

    def test_load_events_fallback_to_usage_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "usage.json").write_text(json.dumps({"records": self.SAMPLE}), "utf-8")
            mod = load_daily_report_module()
            events, source = mod.load_events(base, "2026-06-30")
            self.assertEqual(len(events), 4)
            self.assertEqual(source, "fallback")

    def test_aggregate(self):
        mod = load_daily_report_module()
        agg = mod.aggregate(self.SAMPLE, "2026-06-30")
        self.assertEqual(agg["date"], "2026-06-30")
        self.assertEqual(agg["total_events"], 4)
        self.assertEqual(agg["unique_users"], 2)
        self.assertEqual(agg["by_app"]["seedance"]["requests"], 2)
        self.assertEqual(agg["by_app"]["nano-banana"]["submits"], 1)
        self.assertEqual(agg["by_app"]["nano-banana"]["downloads"], 1)
        self.assertEqual(len(agg["hourly"]), 24)
        self.assertEqual(agg["hourly"][9], 2)
        self.assertEqual(agg["hourly"][14], 2)
        self.assertIn(agg["peak_hour"], (9, 14))
        top = {u["username"] for u in agg["by_user"]}
        self.assertEqual(top, {"alice", "bob"})

    def test_write_csv_bom_and_derived_event_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            csv_path = mod.write_csv(base, "2026-06-30", self.SAMPLE)
            self.assertTrue(csv_path.exists())
            raw = csv_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "csv must start with UTF-8 BOM")
            text = raw.decode("utf-8-sig")
            lines = text.strip().splitlines()
            self.assertEqual(lines[0], "timestamp,app,username,ip,method,path,event_type")
            self.assertEqual(len(lines), 5)
            self.assertIn("submit_job", lines[1])
            self.assertIn("poll", lines[2])
            self.assertIn("submit_job", lines[3])
            self.assertIn("download", lines[4])
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
python3 -m unittest tests.test_daily_report.AggregationTests -v 2>&1 | tail -10
```

Expected: `FileNotFoundError: ... portal/daily_report.py` or `ModuleNotFoundError`.

- [ ] **Step 3: 创建 `portal/daily_report.py` 骨架 + load_events + aggregate + write_csv**

```python
"""Daily usage report generation for Portal.

Reads state/logs/usage-YYYY-MM-DD.jsonl (falls back to usage.json.records[]
if the jsonl is missing), aggregates per-app / per-user / hourly stats,
writes a UTF-8 BOM CSV, calls DeepSeek for insights, assembles a Feishu
interactive card and POSTs it to the configured group-bot webhook.

Runs both as a background daemon inside portal/app.py's process and as
a standalone CLI (`python3 -m portal.daily_report --date YYYY-MM-DD`).
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _classify(method: str, path: str) -> str:
    """Derive event_type from method+path (see spec §2)."""
    p = path.split("?", 1)[0]
    if method == "POST" and re.search(r"/api/(virtual/)?jobs/?$", p):
        return "submit_job"
    if method == "GET" and re.search(r"/api/(virtual/)?jobs/[^/]+$", p):
        return "poll"
    if "/api/download/" in p:
        return "download"
    if "/api/upload" in p:
        return "upload"
    if method == "POST" and "/api/login" in p:
        return "login"
    return "other"


def load_events(state_dir: Path, date: str) -> tuple[list[dict], str]:
    """Return (events, source). source is 'jsonl' if the per-day file was used,
    or 'fallback' if we filtered usage.json.records[]. Empty list on any error."""
    jsonl = state_dir / "logs" / f"usage-{date}.jsonl"
    if jsonl.exists():
        events: list[dict] = []
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return events, "jsonl"
        except Exception:
            pass
    # Fallback: filter usage.json.records[] by date prefix
    usage_json = state_dir / "usage.json"
    if usage_json.exists():
        try:
            data = json.loads(usage_json.read_text("utf-8"))
            records = data.get("records", []) or []
            filtered = [r for r in records if isinstance(r, dict) and str(r.get("time", "")).startswith(date)]
            return filtered, "fallback"
        except Exception:
            return [], "fallback"
    return [], "fallback"


def aggregate(events: list[dict], date: str) -> dict[str, Any]:
    """Build the stats dict consumed by the card renderer + LLM prompt."""
    by_app: dict[str, dict] = {}
    by_user_raw: dict[str, dict] = defaultdict(lambda: {"submits": 0, "downloads": 0, "apps": set()})
    hourly = [0] * 24
    users_seen: set[str] = set()

    for ev in events:
        app = str(ev.get("app", "unknown")) or "unknown"
        user = str(ev.get("username", "") or "").strip()
        etype = _classify(str(ev.get("method", "")), str(ev.get("path", "")))
        stat = by_app.setdefault(app, {"requests": 0, "submits": 0, "downloads": 0, "users": set()})
        stat["requests"] += 1
        if etype == "submit_job":
            stat["submits"] += 1
        elif etype == "download":
            stat["downloads"] += 1
        if user:
            stat["users"].add(user)
            users_seen.add(user)
            u = by_user_raw[user]
            if etype == "submit_job":
                u["submits"] += 1
            elif etype == "download":
                u["downloads"] += 1
            u["apps"].add(app)
        t = str(ev.get("time", ""))
        if len(t) >= 13 and t[10] == " " and t[11:13].isdigit():
            h = int(t[11:13])
            if 0 <= h < 24:
                hourly[h] += 1

    by_app_out = {}
    for app, stat in by_app.items():
        by_app_out[app] = {
            "requests": stat["requests"],
            "submits": stat["submits"],
            "downloads": stat["downloads"],
            "users": len(stat["users"]),
        }

    by_user_list = []
    for uname, u in by_user_raw.items():
        by_user_list.append({
            "username": uname,
            "submits": u["submits"],
            "downloads": u["downloads"],
            "apps": sorted(u["apps"]),
        })
    by_user_list.sort(key=lambda x: (-x["submits"], -x["downloads"], x["username"]))
    by_user_list = by_user_list[:10]

    peak_hour = max(range(24), key=lambda i: hourly[i]) if any(hourly) else 0

    return {
        "date": date,
        "total_events": len(events),
        "unique_users": len(users_seen),
        "by_app": by_app_out,
        "by_user": by_user_list,
        "hourly": hourly,
        "peak_hour": peak_hour,
    }


def write_csv(state_dir: Path, date: str, events: list[dict]) -> Path:
    """Write UTF-8 BOM CSV to state/reports/YYYY-MM-DD.csv and return the path."""
    reports_dir = state_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{date}.csv"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "app", "username", "ip", "method", "path", "event_type"])
    for ev in events:
        writer.writerow([
            ev.get("time", ""),
            ev.get("app", ""),
            ev.get("username", ""),
            ev.get("ip", ""),
            ev.get("method", ""),
            ev.get("path", ""),
            _classify(str(ev.get("method", "")), str(ev.get("path", ""))),
        ])
    with path.open("wb") as f:
        f.write(b"\xef\xbb\xbf")
        f.write(buf.getvalue().encode("utf-8"))
    return path
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
python3 -m unittest tests.test_daily_report.AggregationTests -v 2>&1 | tail -15
```

Expected: 4 tests OK.

- [ ] **Step 5: Commit**

```bash
git add portal/daily_report.py tests/test_daily_report.py
git commit -m "$(cat <<'EOF'
feat(portal): add daily_report aggregation + CSV writer

Loads events from state/logs/usage-YYYY-MM-DD.jsonl with graceful
fallback to usage.json.records[]. Aggregates by app / by user / hourly.
Writes UTF-8 BOM CSV so Chinese renders correctly in Excel.
EOF
)"
```

---

### Task 3: DeepSeek 洞察调用 + 失败降级

**Files:**
- Modify: `portal/daily_report.py`（追加 `generate_insight`）
- Test: `tests/test_daily_report.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
class InsightTests(unittest.TestCase):
    def test_generate_insight_returns_fallback_when_no_key(self):
        mod = load_daily_report_module()
        agg = {"date": "2026-06-30", "total_events": 100, "unique_users": 5,
               "by_app": {"seedance": {"requests": 100, "submits": 10, "downloads": 5, "users": 5}},
               "by_user": [], "hourly": [0]*24, "peak_hour": 0}
        result = mod.generate_insight(agg, deepseek_key="")
        self.assertIn("trend", result)
        self.assertIn("highlight", result)
        self.assertIn("suggestion", result)
        self.assertTrue(result["_fallback"])

    def test_generate_insight_parses_llm_json(self):
        mod = load_daily_report_module()
        agg = {"date": "2026-06-30", "total_events": 100, "unique_users": 5,
               "by_app": {}, "by_user": [], "hourly": [0]*24, "peak_hour": 0}
        fake_response = {
            "choices": [{"message": {"content": json.dumps({
                "trend": "整体平稳",
                "highlight": "seedance 使用集中",
                "suggestion": "关注高峰时段容量",
            })}}]
        }
        with mock.patch.object(mod, "_deepseek_chat", return_value=fake_response):
            result = mod.generate_insight(agg, deepseek_key="sk-fake")
        self.assertEqual(result["trend"], "整体平稳")
        self.assertFalse(result.get("_fallback"))

    def test_generate_insight_handles_llm_failure(self):
        mod = load_daily_report_module()
        agg = {"date": "2026-06-30", "total_events": 0, "unique_users": 0,
               "by_app": {}, "by_user": [], "hourly": [0]*24, "peak_hour": 0}
        with mock.patch.object(mod, "_deepseek_chat", side_effect=RuntimeError("boom")):
            result = mod.generate_insight(agg, deepseek_key="sk-fake")
        self.assertTrue(result["_fallback"])
        self.assertIn("trend", result)

    def test_generate_insight_handles_bad_json(self):
        mod = load_daily_report_module()
        agg = {"date": "2026-06-30", "total_events": 0, "unique_users": 0,
               "by_app": {}, "by_user": [], "hourly": [0]*24, "peak_hour": 0}
        fake_response = {"choices": [{"message": {"content": "not json at all"}}]}
        with mock.patch.object(mod, "_deepseek_chat", return_value=fake_response):
            result = mod.generate_insight(agg, deepseek_key="sk-fake")
        self.assertTrue(result["_fallback"])
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
python3 -m unittest tests.test_daily_report.InsightTests -v 2>&1 | tail -10
```

Expected: `AttributeError: ... generate_insight`.

- [ ] **Step 3: 实现 `_deepseek_chat` + `generate_insight`**

在 `portal/daily_report.py` 顶部加 import：

```python
import urllib.request
import urllib.error
```

追加到文件底部：

```python
INSIGHT_SYSTEM_PROMPT = """你是一个数据分析助理。给你一份 AI 生成工具的日使用统计（含各子应用请求量、用户活跃度、时段分布），请输出严格 JSON：
{
  "trend": "一句话，描述整体情况（20-40 字）",
  "highlight": "一条最值得注意的现象（30-50 字，正面/负面均可）",
  "suggestion": "一条给运营的建议（30-50 字，可执行）"
}
只输出 JSON，不要 markdown 代码块。所有字段必须存在。"""


def _fallback_insight() -> dict:
    return {
        "trend": "数据洞察生成暂不可用，请查看 CSV 明细。",
        "highlight": "未获取到 AI 分析结果。",
        "suggestion": "检查 DeepSeek API Key 与网络。",
        "_fallback": True,
    }


def _deepseek_chat(api_key: str, messages: list[dict], timeout: int = 60) -> dict:
    """Call DeepSeek Chat API with JSON response_format. Returns raw response dict.
    Raises RuntimeError on non-2xx or network errors."""
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"deepseek http {exc.code}: {exc.reason}")
    except Exception as exc:
        raise RuntimeError(f"deepseek call failed: {exc}")


def _summary_for_prompt(agg: dict) -> str:
    """Compact stats text for the LLM (no IPs, no path detail)."""
    lines = [
        f"日期: {agg['date']}",
        f"总请求数: {agg['total_events']}",
        f"活跃用户数: {agg['unique_users']}",
        f"峰值小时: {agg['peak_hour']}:00",
        "各应用:",
    ]
    for app, stat in sorted(agg.get("by_app", {}).items(), key=lambda kv: -kv[1]["requests"]):
        lines.append(f"  {app}: 请求 {stat['requests']}, 提交 {stat['submits']}, 下载 {stat['downloads']}, 用户 {stat['users']}")
    lines.append("Top 用户 (按提交):")
    for u in agg.get("by_user", [])[:5]:
        lines.append(f"  {u['username']}: 提交 {u['submits']}, 下载 {u['downloads']}, 应用 {u['apps']}")
    return "\n".join(lines)


def generate_insight(agg: dict, deepseek_key: str) -> dict:
    """Return {trend, highlight, suggestion, _fallback?}. Never raises."""
    if not deepseek_key:
        return _fallback_insight()
    try:
        resp = _deepseek_chat(deepseek_key, [
            {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": _summary_for_prompt(agg)},
        ])
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content)
        if not all(k in parsed for k in ("trend", "highlight", "suggestion")):
            raise ValueError("missing keys")
        return {
            "trend": str(parsed["trend"]).strip(),
            "highlight": str(parsed["highlight"]).strip(),
            "suggestion": str(parsed["suggestion"]).strip(),
        }
    except Exception as exc:
        print(f"  [daily_report] insight fallback: {exc}", flush=True)
        return _fallback_insight()
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
python3 -m unittest tests.test_daily_report.InsightTests -v 2>&1 | tail -10
```

Expected: 4 tests OK.

- [ ] **Step 5: Commit**

```bash
git add portal/daily_report.py tests/test_daily_report.py
git commit -m "$(cat <<'EOF'
feat(portal): DeepSeek insight generation with graceful fallback

Prompts DeepSeek with a compact stats summary (no IPs, no paths) and
parses strict JSON. Any failure (missing key, network, bad JSON, missing
fields) returns the fallback triplet so the card still sends.
EOF
)"
```

---

### Task 4: 飞书卡片 JSON 组装 + Webhook 发送 + 签名

**Files:**
- Modify: `portal/daily_report.py`（追加 `build_card` / `sign_webhook_body` / `send_webhook`）
- Test: `tests/test_daily_report.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
class CardBuildTests(unittest.TestCase):
    AGG = {
        "date": "2026-06-30",
        "total_events": 1099,
        "unique_users": 7,
        "by_app": {
            "nano-banana": {"requests": 552, "submits": 11, "downloads": 46, "users": 4},
            "seedance":    {"requests": 352, "submits": 0,  "downloads": 11, "users": 3},
        },
        "by_user": [
            {"username": "高大王", "submits": 8, "downloads": 13, "apps": ["nano-banana"]},
        ],
        "hourly": [0]*24,
        "peak_hour": 14,
    }
    INSIGHT = {"trend": "T", "highlight": "H", "suggestion": "S"}

    def test_build_card_structure(self):
        mod = load_daily_report_module()
        card = mod.build_card(self.AGG, self.INSIGHT, csv_url="https://portal.example/api/reports/daily/2026-06-30.csv")
        self.assertEqual(card["msg_type"], "interactive")
        payload = card["card"]
        self.assertEqual(payload["schema"], "2.0")
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertIn("2026-06-30", blob)
        self.assertIn("1,099", blob)
        self.assertIn("nano-banana", blob)
        self.assertIn("高大王", blob)
        self.assertIn("https://portal.example/api/reports/daily/2026-06-30.csv", blob)
        self.assertIn("T", blob)
        self.assertIn("H", blob)
        self.assertIn("S", blob)

    def test_sign_webhook_body_matches_feishu_algo(self):
        mod = load_daily_report_module()
        sig = mod.sign_webhook_body("secret123", 1700000000)
        expected_string = "1700000000\nsecret123"
        import hmac, hashlib, base64
        expected = base64.b64encode(hmac.new(expected_string.encode(), digestmod=hashlib.sha256).digest()).decode()
        self.assertEqual(sig, expected)


class WebhookSendTests(unittest.TestCase):
    def test_send_webhook_signs_when_secret_present(self):
        mod = load_daily_report_module()
        captured = {}
        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"code":0,"msg":"ok"}'
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, info = mod.send_webhook("https://open.feishu.cn/x", {"msg_type": "interactive", "card": {}}, sign_secret="s")
        self.assertTrue(ok)
        self.assertIn("timestamp", captured["body"])
        self.assertIn("sign", captured["body"])

    def test_send_webhook_no_sign_when_secret_empty(self):
        mod = load_daily_report_module()
        captured = {}
        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"code":0}'
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, _ = mod.send_webhook("https://x", {"msg_type": "interactive", "card": {}}, sign_secret="")
        self.assertTrue(ok)
        self.assertNotIn("sign", captured["body"])

    def test_send_webhook_reports_feishu_error(self):
        mod = load_daily_report_module()
        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"code":19024,"msg":"sign error"}'
        with mock.patch.object(mod.urllib.request, "urlopen", return_value=FakeResp()):
            ok, info = mod.send_webhook("https://x", {"msg_type": "interactive", "card": {}}, sign_secret="")
        self.assertFalse(ok)
        self.assertIn("19024", info)
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
python3 -m unittest tests.test_daily_report.CardBuildTests tests.test_daily_report.WebhookSendTests -v 2>&1 | tail -20
```

Expected: AttributeError for `build_card` / `sign_webhook_body` / `send_webhook`.

- [ ] **Step 3: 实现 build_card + sign_webhook_body + send_webhook**

在 `portal/daily_report.py` 顶部 import 追加：

```python
import base64
import hashlib
import hmac
import time
```

追加到文件底部：

```python
def _fmt_int(n: int) -> str:
    return f"{n:,}"


def build_card(agg: dict, insight: dict, csv_url: str) -> dict:
    """Assemble Feishu interactive-card message body. Schema 2.0."""
    header = {
        "title": {"tag": "plain_text", "content": f"AI 工具日报 · {agg['date']}"},
        "template": "blue",
    }
    top_submits = sum(v["submits"] for v in agg["by_app"].values())
    top_downloads = sum(v["downloads"] for v in agg["by_app"].values())

    summary_md = (
        f"**总请求** {_fmt_int(agg['total_events'])}    "
        f"**活跃用户** {agg['unique_users']}\n"
        f"**提交任务** {top_submits}    **下载** {top_downloads}    "
        f"**峰值** {agg['peak_hour']:02d}:00"
    )

    by_app_lines = []
    for app, stat in sorted(agg["by_app"].items(), key=lambda kv: -kv[1]["requests"]):
        by_app_lines.append(f"- **{app}**    {_fmt_int(stat['requests'])} 请求 · {stat['users']} 用户 · {stat['submits']} 提交 · {stat['downloads']} 下载")
    by_app_md = "\n".join(by_app_lines) if by_app_lines else "_无数据_"

    by_user_lines = []
    for u in agg["by_user"][:10]:
        apps = ", ".join(u.get("apps", []))
        by_user_lines.append(f"- **{u['username']}**    {u['submits']} 提交 · {u['downloads']} 下载 · {apps}")
    by_user_md = "\n".join(by_user_lines) if by_user_lines else "_无提交/下载记录_"

    fallback_flag = " _(数据洞察生成失败,使用占位文案)_" if insight.get("_fallback") else ""
    insight_md = (
        f"**趋势** {insight['trend']}\n\n"
        f"**关注** {insight['highlight']}\n\n"
        f"**建议** {insight['suggestion']}{fallback_flag}"
    )

    elements = [
        {"tag": "markdown", "content": summary_md},
        {"tag": "hr"},
        {"tag": "markdown", "content": "**各应用**\n" + by_app_md},
        {"tag": "hr"},
        {"tag": "markdown", "content": "**Top 用户（按提交数）**\n" + by_user_md},
        {"tag": "hr"},
        {"tag": "markdown", "content": "**💡 洞察**\n" + insight_md},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📥 下载 CSV 明细"},
                "type": "primary",
                "url": csv_url,
            }],
        },
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "Portal 自动生成 · 浏览器提示自签证书时选「继续访问」"}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {"schema": "2.0", "header": header, "body": {"elements": elements}},
    }


def sign_webhook_body(secret: str, timestamp: int) -> str:
    """Feishu custom-bot signature: base64(HMAC-SHA256(secret, '{ts}\\n{secret}'))."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_webhook(webhook_url: str, card_body: dict, sign_secret: str = "", timeout: int = 15) -> tuple[bool, str]:
    """POST the card to the webhook. Returns (ok, info). ok=False when Feishu
    returns non-zero code or the transport itself failed."""
    if not webhook_url:
        return False, "webhook_url not configured"
    payload = dict(card_body)
    if sign_secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = sign_webhook_body(sign_secret, ts)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return False, f"transport error: {exc}"
    try:
        result = json.loads(raw)
    except Exception:
        return False, f"non-json response: {raw[:200]}"
    code = result.get("code", -1)
    if code == 0:
        return True, "ok"
    return False, f"feishu code={code} msg={result.get('msg','')}"
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
python3 -m unittest tests.test_daily_report.CardBuildTests tests.test_daily_report.WebhookSendTests -v 2>&1 | tail -15
```

Expected: 5 tests OK.

- [ ] **Step 5: Commit**

```bash
git add portal/daily_report.py tests/test_daily_report.py
git commit -m "$(cat <<'EOF'
feat(portal): Feishu interactive card assembly + webhook send

Builds a schema 2.0 interactive card with header, summary, per-app
breakdown, top user list, LLM insight block, and a "download CSV"
button linking back to Portal. Signs the body with HMAC-SHA256 when
sign_secret is configured. Surfaces Feishu error codes for diagnosis.
EOF
)"
```

---

### Task 5: `send_daily_report` 编排 + 配置加载 + CLI dry-run

**Files:**
- Modify: `portal/daily_report.py`（追加编排函数 + 配置读写 + `__main__`）
- Test: `tests/test_daily_report.py`（追加端到端 dry-run 测试）

- [ ] **Step 1: 追加失败测试**

```python
class SendDailyReportTests(unittest.TestCase):
    def _seed(self, base: Path):
        (base / "logs").mkdir(parents=True, exist_ok=True)
        events = [
            {"time": "2026-06-30 09:00:00", "app": "seedance", "ip": "10.0.0.1", "username": "alice", "method": "POST", "path": "/api/jobs"},
            {"time": "2026-06-30 14:20:00", "app": "nano-banana", "ip": "10.0.0.2", "username": "bob", "method": "GET", "path": "/api/download/xyz"},
        ]
        with (base / "logs" / "usage-2026-06-30.jsonl").open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_send_daily_report_dry_run_writes_csv_no_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._seed(base)
            mod = load_daily_report_module()
            calls = []
            with mock.patch.object(mod, "send_webhook", side_effect=lambda *a, **kw: calls.append(a) or (True, "ok")):
                result = mod.send_daily_report(base, "2026-06-30",
                                               config={"webhook_url": "https://x", "portal_base_url": "https://p"},
                                               deepseek_key="",
                                               dry_run=True)
            self.assertTrue(result["ok"])
            self.assertTrue((base / "reports" / "2026-06-30.csv").exists())
            self.assertEqual(calls, [], "dry_run must not call send_webhook")

    def test_send_daily_report_real_sends_when_not_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._seed(base)
            mod = load_daily_report_module()
            with mock.patch.object(mod, "send_webhook", return_value=(True, "ok")) as sender:
                result = mod.send_daily_report(base, "2026-06-30",
                                               config={"webhook_url": "https://x", "portal_base_url": "https://p:9091"},
                                               deepseek_key="",
                                               dry_run=False)
            self.assertTrue(result["ok"])
            sender.assert_called_once()
            args, _ = sender.call_args
            self.assertEqual(args[0], "https://x")
            self.assertIn("2026-06-30", json.dumps(args[1], ensure_ascii=False))


class ConfigTests(unittest.TestCase):
    def test_load_config_returns_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            cfg = mod.load_config(base)
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["schedule_time"], "09:05")
            self.assertEqual(cfg["webhook_url"], "")

    def test_save_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            mod.save_config(base, {"enabled": True, "webhook_url": "https://x",
                                    "sign_secret": "s", "schedule_time": "10:00",
                                    "portal_base_url": "https://p"})
            cfg = mod.load_config(base)
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["webhook_url"], "https://x")
            self.assertEqual(cfg["schedule_time"], "10:00")
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
python3 -m unittest tests.test_daily_report.SendDailyReportTests tests.test_daily_report.ConfigTests -v 2>&1 | tail -15
```

Expected: AttributeError for `send_daily_report` / `load_config` / `save_config`.

- [ ] **Step 3: 实现 load_config + save_config + send_daily_report + `__main__`**

在 `portal/daily_report.py` 顶部追加：

```python
import argparse
import os
```

追加到文件底部：

```python
DEFAULT_CONFIG = {
    "enabled": False,
    "webhook_url": "",
    "sign_secret": "",
    "schedule_time": "09:05",
    "portal_base_url": "",
}


def _config_path(state_dir: Path) -> Path:
    return state_dir / "feishu.json"


def load_config(state_dir: Path) -> dict:
    path = _config_path(state_dir)
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
        except Exception as exc:
            print(f"  [daily_report] config load failed: {exc}", flush=True)
    return cfg


def save_config(state_dir: Path, values: dict) -> dict:
    cfg = load_config(state_dir)
    for k in DEFAULT_CONFIG:
        if k in values:
            cfg[k] = values[k]
    path = _config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)
    return cfg


def _load_deepseek_key(state_dir: Path) -> str:
    """Portal doesn't own DEEPSEEK_KEY_PATH; reuse seedance/state/deepseek.key if present.
    Env var overrides."""
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env
    for candidate in (state_dir / "deepseek.key", state_dir.parent.parent / "seedance" / "state" / "deepseek.key"):
        try:
            if candidate.exists():
                return candidate.read_text("utf-8").strip()
        except Exception:
            continue
    return ""


def send_daily_report(state_dir: Path, date: str, config: dict, deepseek_key: str, dry_run: bool = False) -> dict:
    """End-to-end: load events -> aggregate -> csv -> insight -> card -> (send).
    Returns {ok, source, csv_path, card, feishu_info}."""
    events, source = load_events(state_dir, date)
    csv_path = write_csv(state_dir, date, events)
    agg = aggregate(events, date)
    insight = generate_insight(agg, deepseek_key)
    portal_base = (config.get("portal_base_url") or "").rstrip("/")
    csv_url = f"{portal_base}/api/reports/daily/{date}.csv" if portal_base else f"/api/reports/daily/{date}.csv"
    card = build_card(agg, insight, csv_url=csv_url)
    if source == "fallback":
        card["card"]["body"]["elements"].insert(
            -2,
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "⚠️ 该日 jsonl 明细未找到,数据从 usage.json 回退读取,可能不完整"}]},
        )
    result = {"ok": True, "source": source, "csv_path": str(csv_path), "card": card, "feishu_info": "dry_run"}
    if dry_run:
        return result
    ok, info = send_webhook(config.get("webhook_url", ""), card, sign_secret=config.get("sign_secret", ""))
    result["ok"] = ok
    result["feishu_info"] = info
    return result


def _default_state_dir() -> Path:
    return Path(__file__).resolve().parent / "state"


def main():
    parser = argparse.ArgumentParser(description="Portal daily Feishu report")
    parser.add_argument("--date", default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                        help="YYYY-MM-DD, default = yesterday")
    parser.add_argument("--dry-run", action="store_true", help="build card and CSV but do not send to Feishu")
    parser.add_argument("--state-dir", default=str(_default_state_dir()))
    args = parser.parse_args()
    state_dir = Path(args.state_dir)
    cfg = load_config(state_dir)
    key = _load_deepseek_key(state_dir)
    result = send_daily_report(state_dir, args.date, cfg, deepseek_key=key, dry_run=args.dry_run)
    printable = {k: v for k, v in result.items() if k != "card"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("--- card preview ---")
        print(json.dumps(result["card"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认 pass**

```bash
python3 -m unittest tests.test_daily_report -v 2>&1 | tail -20
```

Expected: all tests OK (previous + 5 new).

- [ ] **Step 5: CLI dry-run 手动验证**

```bash
cd /Users/260413a/ai-generation-portable-apps
python3 -m portal.daily_report --date $(date -v-1d +%Y-%m-%d) --dry-run --state-dir portal/state 2>&1 | head -30
```

Expected: 打印 JSON 结果，包含 `csv_path`、`feishu_info: "dry_run"`，然后 `--- card preview ---` 段展示卡片 JSON。

- [ ] **Step 6: Commit**

```bash
git add portal/daily_report.py tests/test_daily_report.py
git commit -m "$(cat <<'EOF'
feat(portal): daily report orchestration + config + CLI dry-run

send_daily_report chains load -> aggregate -> csv -> insight -> card ->
send, and stamps a warning note onto the card when the source fell back
from jsonl to usage.json. load_config/save_config manage state/feishu.json.
CLI entry: python3 -m portal.daily_report --date YYYY-MM-DD [--dry-run].
EOF
)"
```

---

### Task 6: Portal 4 个新端点

**Files:**
- Modify: `portal/app.py` (do_GET 分发 + do_POST 分发 + 4 个 handler 方法)
- Test: 手动 curl（endpoints 逻辑很薄，主要复用 daily_report 的测试）

- [ ] **Step 1: 在 `portal/app.py` 顶部 import 里追加**

找到已有的 import 段（约文件前 30 行），追加：

```python
import daily_report as _daily_report_module
```

（`portal/` 目录当作模块根，直接同目录 import。如果启动方式使运行时 sys.path 找不到，用 `from pathlib import Path; import sys; sys.path.insert(0, str(Path(__file__).resolve().parent))` 保底——检查现有代码看是否已有类似插入，若无则加。）

**验证：**

```bash
grep -n "^import \|^from " portal/app.py | head -20
```

如已有 `sys.path.insert(0, str(Path(__file__).resolve().parent))` 直接 import；否则先补上 sys.path 再 import。

- [ ] **Step 2: 添加 handler 方法**

在 `Handler` 类里（`_platform_portrait_key_set` 附近）新增：

```python
    def _report_csv_download(self, user: dict, date: str):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self._json(400, {"ok": False, "error": "invalid date"})
            return
        state_dir = STATE_DIR
        csv_path = state_dir / "reports" / f"{date}.csv"
        if not csv_path.exists():
            # generate on demand
            events, _ = _daily_report_module.load_events(state_dir, date)
            csv_path = _daily_report_module.write_csv(state_dir, date, events)
        data = csv_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="usage-{date}.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _report_send(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json() or {}
        date = str(body.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self._json(400, {"ok": False, "error": "invalid date"})
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        key = _daily_report_module._load_deepseek_key(STATE_DIR)
        try:
            result = _daily_report_module.send_daily_report(STATE_DIR, date, cfg, deepseek_key=key, dry_run=False)
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"send failed: {exc}"})
            return
        self._json(200, {"ok": result["ok"], "info": result["feishu_info"], "source": result["source"]})

    def _report_preview(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json() or {}
        date = str(body.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self._json(400, {"ok": False, "error": "invalid date"})
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        key = _daily_report_module._load_deepseek_key(STATE_DIR)
        try:
            result = _daily_report_module.send_daily_report(STATE_DIR, date, cfg, deepseek_key=key, dry_run=True)
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"preview failed: {exc}"})
            return
        self._json(200, {
            "ok": True,
            "source": result["source"],
            "card": result["card"],
        })

    def _feishu_config_get(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        masked = dict(cfg)
        w = masked.get("webhook_url", "")
        if w:
            masked["webhook_url"] = w[:32] + "..." if len(w) > 35 else w
        s = masked.get("sign_secret", "")
        if s:
            masked["sign_secret_present"] = True
            masked["sign_secret"] = ""
        else:
            masked["sign_secret_present"] = False
        self._json(200, {"ok": True, "config": masked})

    def _feishu_config_put(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json()
        if body is None:
            return
        updates: dict = {}
        for k in ("enabled", "webhook_url", "sign_secret", "schedule_time", "portal_base_url"):
            if k in body:
                updates[k] = body[k]
        cfg = _daily_report_module.save_config(STATE_DIR, updates)
        self._json(200, {"ok": True, "config": {k: cfg[k] for k in ("enabled", "schedule_time", "portal_base_url")}})
```

（注意：`re` 已在 `portal/app.py` 顶部 import。如未 import，追加 `import re`。先 grep 检查。）

- [ ] **Step 3: 挂到 do_GET / do_POST 路由**

在 `do_GET` 里，`/api/platform/portrait-key` 那个 `elif` 之后追加：

```python
        elif path == "/api/feishu/config":
            self._feishu_config_get(user)
        elif path.startswith("/api/reports/daily/") and path.endswith(".csv"):
            date = path[len("/api/reports/daily/"):-len(".csv")]
            self._report_csv_download(user, date)
```

在 `do_POST` 里，`/api/platform/portrait-key` 那行之后追加：

```python
        if path == "/api/reports/send":
            self._report_send(user)
            return
        if path == "/api/reports/preview":
            self._report_preview(user)
            return
        if path == "/api/feishu/config":
            self._feishu_config_put(user)
            return
```

- [ ] **Step 4: 语法自查**

```bash
python3 -c "import ast; ast.parse(open('portal/app.py').read()); print('OK')"
```

- [ ] **Step 5: 手动 curl 冒烟**

先启动 portal（如果生产 portal 已跑，kill 让 launchd 拉起）。然后拿 session cookie（浏览器登录一次 → devtools 复制 `session=xxx`）：

```bash
SESSION="session=xxx"  # 从浏览器复制
BASE="https://127.0.0.1:9091"
# preview
curl -sk -X POST "$BASE/api/reports/preview" -H "Cookie: $SESSION" -H "Content-Type: application/json" -d '{"date":"'$(date -v-1d +%Y-%m-%d)'"}' | python3 -m json.tool | head -30
# csv download
curl -sk "$BASE/api/reports/daily/$(date -v-1d +%Y-%m-%d).csv" -H "Cookie: $SESSION" | head -5
# config get
curl -sk "$BASE/api/feishu/config" -H "Cookie: $SESSION" | python3 -m json.tool
```

Expected: preview 返回 card JSON；csv 前 5 行含 header + BOM；config 返回默认值。

- [ ] **Step 6: Commit**

```bash
git add portal/app.py
git commit -m "$(cat <<'EOF'
feat(portal): expose /api/reports and /api/feishu admin endpoints

- GET  /api/reports/daily/YYYY-MM-DD.csv   session-authed CSV download
- POST /api/reports/send                    fire the day's report now
- POST /api/reports/preview                 build card JSON, do not send
- GET/POST /api/feishu/config               read/write state/feishu.json
All 4 endpoints are admin-only. webhook_url and sign_secret are masked on GET.
EOF
)"
```

---

### Task 7: 调度 daemon 线程 + marker 防重发

**Files:**
- Modify: `portal/daily_report.py`（追加 `scheduler_loop`）
- Modify: `portal/app.py`（main() 里 spawn daemon 线程）
- Test: `tests/test_daily_report.py`（追加 marker 逻辑测试）

- [ ] **Step 1: 追加失败测试**

```python
class SchedulerMarkerTests(unittest.TestCase):
    def test_should_run_true_when_time_match_and_no_marker(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertTrue(mod._should_run_now(base, "09:05", now_hhmm="09:05", today="2026-07-01"))

    def test_should_run_false_when_time_mismatch(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertFalse(mod._should_run_now(base, "09:05", now_hhmm="09:06", today="2026-07-01"))

    def test_should_run_false_when_marker_exists(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "reports").mkdir()
            (base / "reports" / ".sent-2026-07-01").write_text("", "utf-8")
            self.assertFalse(mod._should_run_now(base, "09:05", now_hhmm="09:05", today="2026-07-01"))

    def test_mark_sent_creates_marker(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod._mark_sent(base, "2026-07-01")
            self.assertTrue((base / "reports" / ".sent-2026-07-01").exists())
```

- [ ] **Step 2: 跑测试确认 fail**

```bash
python3 -m unittest tests.test_daily_report.SchedulerMarkerTests -v 2>&1 | tail -10
```

- [ ] **Step 3: 实现 scheduler helpers + loop**

在 `portal/daily_report.py` 底部（`if __name__ == "__main__":` 之前）追加：

```python
def _marker_path(state_dir: Path, today: str) -> Path:
    return state_dir / "reports" / f".sent-{today}"


def _should_run_now(state_dir: Path, schedule_time: str, now_hhmm: str, today: str) -> bool:
    if now_hhmm != schedule_time:
        return False
    return not _marker_path(state_dir, today).exists()


def _mark_sent(state_dir: Path, today: str):
    p = _marker_path(state_dir, today)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def scheduler_loop(state_dir: Path):
    """Runs forever inside portal process. Every 60s, checks whether current
    time-of-day matches configured schedule_time and today's marker is absent.
    On failure, retries next minute; hard-caps at 3 attempts per day, then
    stamps the marker to stop retrying and let humans investigate."""
    fail_counts: dict[str, int] = {}
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            now_hhmm = now.strftime("%H:%M")
            cfg = load_config(state_dir)
            if cfg.get("enabled") and _should_run_now(state_dir, cfg.get("schedule_time", "09:05"), now_hhmm, today):
                yesterday = (now - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
                key = _load_deepseek_key(state_dir)
                try:
                    result = send_daily_report(state_dir, yesterday, cfg, deepseek_key=key, dry_run=False)
                    if result["ok"]:
                        _mark_sent(state_dir, today)
                        print(f"  [daily_report] sent {yesterday} report OK: {result['feishu_info']}", flush=True)
                    else:
                        fail_counts[today] = fail_counts.get(today, 0) + 1
                        print(f"  [daily_report] send failed (attempt {fail_counts[today]}): {result['feishu_info']}", flush=True)
                        if fail_counts[today] >= 3:
                            _mark_sent(state_dir, today)
                            print(f"  [daily_report] circuit-broken after 3 failures for {today}", flush=True)
                except Exception as exc:
                    fail_counts[today] = fail_counts.get(today, 0) + 1
                    print(f"  [daily_report] exception (attempt {fail_counts[today]}): {exc}", flush=True)
                    if fail_counts[today] >= 3:
                        _mark_sent(state_dir, today)
        except Exception as exc:
            print(f"  [daily_report] scheduler tick failed: {exc}", flush=True)
        time.sleep(60)
```

- [ ] **Step 4: 在 `portal/app.py` `main()` 里挂线程**

找到 `manager.start_all()` 附近，在它之后（或之前，无所谓）追加：

```python
    # Feishu daily report scheduler (daemon, tolerates all errors internally)
    threading.Thread(
        target=_daily_report_module.scheduler_loop,
        args=(STATE_DIR,),
        daemon=True,
        name="daily_report_scheduler",
    ).start()
    print("  [daily_report] scheduler thread started", flush=True)
```

（`threading` 已在 portal/app.py 顶部 import，grep 确认。）

- [ ] **Step 5: 跑测试确认 pass**

```bash
python3 -m unittest tests.test_daily_report -v 2>&1 | tail -25
```

Expected: 所有 tests OK。

- [ ] **Step 6: 冒烟：把当日 schedule_time 设成当前 +2 分钟，看是否触发**

（可选，如果时间不够留给最后集成测试）

- [ ] **Step 7: Commit**

```bash
git add portal/daily_report.py portal/app.py tests/test_daily_report.py
git commit -m "$(cat <<'EOF'
feat(portal): daily report scheduler daemon + marker anti-duplication

Every 60s the loop checks whether HH:MM matches configured
schedule_time and today's .sent marker is absent, then sends
yesterday's report. Failed attempts retry each minute but circuit-break
after 3 tries per day. All exceptions are caught inside the loop so the
portal main thread is never affected.
EOF
)"
```

---

### Task 8: 前端运维面板 —「飞书日报」

**Files:**
- Modify: `portal/static/index.html`（在 stats tab 追加 section）
- Modify: `portal/static/app.js`（StatsApp 扩展 state + methods）

- [ ] **Step 1: 在 stats tab 追加 section（`portal/static/index.html`）**

定位 `<section class="stats-panel" v-if="isAdmin">` 段落（约 528 行，portrait-key 面板），在它之后追加：

```html
      <section class="stats-panel" v-if="isAdmin">
        <h2>飞书日报</h2>
        <p style="font-size:12px;color:#94a3b8;margin:0 0 10px">每天到点自动把昨日 Portal 使用数据推送到飞书群机器人。管理员可预览/立即发送。</p>
        <div class="stats-row" style="gap:10px;flex-wrap:wrap;align-items:flex-end">
          <label style="flex:2 1 320px">
            <div style="font-size:12px;color:#94a3b8">Webhook URL</div>
            <input v-model="feishu.webhook_url" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." style="width:100%">
          </label>
          <label style="flex:1 1 160px">
            <div style="font-size:12px;color:#94a3b8">签名 secret (可选)</div>
            <input v-model="feishu.sign_secret" :placeholder="feishu._secretPresent ? '已配置(留空不修改)' : ''" style="width:100%">
          </label>
          <label style="flex:0 0 120px">
            <div style="font-size:12px;color:#94a3b8">触发时间</div>
            <input v-model="feishu.schedule_time" placeholder="09:05" style="width:100%">
          </label>
          <label style="flex:1 1 220px">
            <div style="font-size:12px;color:#94a3b8">Portal 基础 URL</div>
            <input v-model="feishu.portal_base_url" placeholder="https://192.168.x.x:9091" style="width:100%">
          </label>
          <label style="flex:0 0 auto;display:flex;align-items:center;gap:6px">
            <input type="checkbox" v-model="feishu.enabled">
            <span>启用定时</span>
          </label>
        </div>
        <div class="stats-row" style="gap:10px;margin-top:10px;align-items:center">
          <button @click="saveFeishuConfig()">保存配置</button>
          <label>
            <span style="font-size:12px;color:#94a3b8">日期</span>
            <input v-model="feishu.previewDate" type="date" style="margin-left:6px">
          </label>
          <button @click="previewFeishu()">预览卡片</button>
          <button @click="sendFeishuNow()" style="background:#2563eb;color:#fff">立即发送</button>
          <span v-if="feishu.status" style="font-size:12px;color:#cbd5e1">{{ feishu.status }}</span>
        </div>
        <pre v-if="feishu.previewJson" style="max-height:320px;overflow:auto;background:#0f172a;padding:10px;border-radius:6px;font-size:11px;margin-top:10px">{{ feishu.previewJson }}</pre>
      </section>
```

- [ ] **Step 2: 扩展 StatsApp state + methods（`portal/static/app.js`）**

定位 `StatsApp` 定义。在其 reactive state（或返回对象）里，`isAdmin` / `byUser` 等旁边加：

```javascript
feishu: {
  enabled: false,
  webhook_url: '',
  sign_secret: '',
  schedule_time: '09:05',
  portal_base_url: '',
  previewDate: '',
  previewJson: '',
  status: '',
  _secretPresent: false,
},
```

在 `init()` 里追加（`isAdmin` 判断成立时）：

```javascript
if (this.isAdmin) {
  this.loadFeishuConfig();
  // default previewDate = yesterday
  const d = new Date(); d.setDate(d.getDate() - 1);
  this.feishu.previewDate = d.toISOString().slice(0, 10);
}
```

在 methods 段追加：

```javascript
async loadFeishuConfig() {
  try {
    const r = await fetch('/api/feishu/config').then(r => r.json());
    if (r.ok && r.config) {
      const c = r.config;
      this.feishu.enabled = !!c.enabled;
      this.feishu.webhook_url = c.webhook_url || '';
      this.feishu.schedule_time = c.schedule_time || '09:05';
      this.feishu.portal_base_url = c.portal_base_url || '';
      this.feishu._secretPresent = !!c.sign_secret_present;
      // never populate sign_secret from server (masked); user re-types to change
      this.feishu.sign_secret = '';
    }
  } catch (e) {
    this.feishu.status = '读取配置失败: ' + e;
  }
},
async saveFeishuConfig() {
  this.feishu.status = '保存中...';
  const body = {
    enabled: this.feishu.enabled,
    webhook_url: this.feishu.webhook_url,
    schedule_time: this.feishu.schedule_time,
    portal_base_url: this.feishu.portal_base_url,
  };
  // Only send sign_secret if user typed something; empty means "don't change"
  if ((this.feishu.sign_secret || '').length > 0) body.sign_secret = this.feishu.sign_secret;
  try {
    const r = await fetch('/api/feishu/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
    this.feishu.status = r.ok ? '已保存' : ('保存失败: ' + (r.error || ''));
    if (r.ok) await this.loadFeishuConfig();
  } catch (e) {
    this.feishu.status = '保存失败: ' + e;
  }
},
async previewFeishu() {
  const date = this.feishu.previewDate;
  if (!date) { this.feishu.status = '请选日期'; return; }
  this.feishu.status = '生成预览...';
  try {
    const r = await fetch('/api/reports/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({date})}).then(r => r.json());
    if (r.ok) {
      this.feishu.previewJson = JSON.stringify(r.card, null, 2);
      this.feishu.status = '预览就绪 (source=' + r.source + ')';
    } else {
      this.feishu.status = '预览失败: ' + (r.error || '');
    }
  } catch (e) {
    this.feishu.status = '预览失败: ' + e;
  }
},
async sendFeishuNow() {
  const date = this.feishu.previewDate;
  if (!date) { this.feishu.status = '请选日期'; return; }
  if (!confirm(`确认向飞书发送 ${date} 的日报？`)) return;
  this.feishu.status = '发送中...';
  try {
    const r = await fetch('/api/reports/send', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({date})}).then(r => r.json());
    this.feishu.status = r.ok ? ('已发送: ' + (r.info || '')) : ('发送失败: ' + (r.error || r.info || ''));
  } catch (e) {
    this.feishu.status = '发送失败: ' + e;
  }
},
```

- [ ] **Step 3: 前端手动验证（浏览器 hard-refresh）**

打开 `https://<lan>:9091` → 登录 admin → 「统计」tab → 滚到「飞书日报」面板：

1. 首次访问 → 显示默认值（enabled=false, schedule_time=09:05）
2. 填 webhook URL → 保存 → 状态显示「已保存」
3. 点「预览卡片」→ 下方 pre 展示 card JSON
4. 点「立即发送」→ confirm → 状态显示「已发送: ok」→ 飞书群里看到卡片
5. 点卡片「📥 下载 CSV 明细」→ 浏览器新标签下载 CSV

- [ ] **Step 4: Commit**

```bash
git add portal/static/index.html portal/static/app.js
git commit -m "$(cat <<'EOF'
feat(portal): stats tab panel for Feishu daily report config + trigger

Admin-only card with webhook URL / sign_secret / schedule_time / portal
base URL / enabled toggle. Buttons: preview card JSON, send now (with
confirm). sign_secret is masked on GET and only sent when the user
retypes it so accidental save doesn't wipe existing configuration.
EOF
)"
```

---

### Task 9: End-to-end 集成验证

**Files:** 无代码改动，纯手动/CLI 冒烟

- [ ] **Step 1: 重启 portal**

```bash
# 找 portal pid 让 launchd 拉起（用户可能需要手工 kill；分类器会拦，找不到就自然重启一次）
launchctl kickstart -k gui/$(id -u)/com.ai-portal 2>&1 | head
# 或用户手动 kill portal 主进程 pid
```

- [ ] **Step 2: 用真实飞书测试群 webhook 走一遍**

在飞书群里创建自定义机器人 → 复制 webhook URL → Portal 运维面板填入 → 保存 → 预览 → 立即发送 → 群里出现卡片 → 点按钮下载 CSV。

- [ ] **Step 3: 校验 jsonl 日切生效**

```bash
ls portal/state/logs/ 2>&1 | head
wc -l portal/state/logs/usage-$(date +%Y-%m-%d).jsonl
```

Expected: 存在当日 jsonl，行数 = 今日 usage.json.records 里当日条数（大致）。

- [ ] **Step 4: 校验 marker 防重发**

```bash
# 手动 touch marker，看下次到点是否跳过
touch portal/state/reports/.sent-$(date +%Y-%m-%d)
```

- [ ] **Step 5: 全量测试**

```bash
python3 -m unittest tests.test_daily_report -v 2>&1 | tail -25
python3 -m unittest tests -v 2>&1 | tail -30
```

Expected: 所有测试 OK，portal_startup 测试不 break。

无 commit。

