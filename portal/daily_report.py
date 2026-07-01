"""Daily usage report generation for Portal.

Reads state/logs/usage-YYYY-MM-DD.jsonl (falls back to usage.json.records[]
if the jsonl is missing), aggregates per-app / per-user / hourly stats,
writes a UTF-8 BOM CSV, calls DeepSeek for insights, assembles a Feishu
interactive card and POSTs it to the configured group-bot webhook.

Runs both as a background daemon inside portal/app.py's process and as
a standalone CLI (`python3 -m portal.daily_report --date YYYY-MM-DD`).
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import socket
import time
import urllib.request
import urllib.error
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
        {"tag": "markdown", "content": "<font color='grey'>Portal 自动生成 · 浏览器提示自签证书时选「继续访问」</font>"},
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


def _default_portal_base_url() -> str:
    """Best-effort LAN base URL when config.portal_base_url is empty.
    Uses the UDP-connect trick to grab the outbound interface IP; falls back
    to 127.0.0.1 which is only useful for local self-testing but at least
    yields a valid absolute URL so the Feishu button doesn't 404."""
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    port = os.environ.get("PORTAL_PORT", "9090")
    return f"https://{ip}:{port}"


def send_daily_report(state_dir: Path, date: str, config: dict, deepseek_key: str, dry_run: bool = False) -> dict:
    """End-to-end: load events -> aggregate -> csv -> insight -> card -> (send).
    Returns {ok, source, csv_path, card, feishu_info}."""
    events, source = load_events(state_dir, date)
    csv_path = write_csv(state_dir, date, events)
    agg = aggregate(events, date)
    insight = generate_insight(agg, deepseek_key)
    portal_base = (config.get("portal_base_url") or "").rstrip("/") or _default_portal_base_url()
    csv_url = f"{portal_base}/api/reports/daily/{date}.csv"
    card = build_card(agg, insight, csv_url=csv_url)
    if source == "fallback":
        card["card"]["body"]["elements"].insert(
            -2,
            {"tag": "markdown", "content": "<font color='orange'>⚠️ 该日 jsonl 明细未找到,数据从 usage.json 回退读取,可能不完整</font>"},
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
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
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


if __name__ == "__main__":
    main()
