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
