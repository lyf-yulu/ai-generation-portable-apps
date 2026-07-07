#!/usr/bin/env python3
"""本地清理工具的 HTTP 服务，配合 tools/cleanup.html 使用。

只监听 127.0.0.1，不暴露给局域网。启动后浏览器打开 http://127.0.0.1:9099/
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cleanup_old_media as core  # noqa: E402

HOST = "127.0.0.1"
PORT = 9099
HTML_FILE = HERE / "cleanup.html"


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def parse_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def do_scan(before: dt.date) -> dict:
    hits = core.collect(before)
    today = dt.date.today()
    by_dir: dict[str, dict] = {}
    total_size = 0
    today_count = 0
    for p, sz, mdate in hits:
        total_size += sz
        if mdate == today:
            today_count += 1
        try:
            rel = p.relative_to(core.REPO_ROOT)
            key = "/".join(rel.parts[:2]) if len(rel.parts) >= 2 else str(rel)
        except ValueError:
            key = str(p.parent)
        entry = by_dir.setdefault(key, {"count": 0, "size": 0})
        entry["count"] += 1
        entry["size"] += sz
    return {
        "total": len(hits),
        "total_size": total_size,
        "total_size_h": core.human_size(total_size),
        "today_count": today_count,
        "today": today.isoformat(),
        "oldest": hits[0][2].isoformat() if hits else None,
        "newest": hits[-1][2].isoformat() if hits else None,
        "by_dir": [
            {"path": k, "count": v["count"], "size": v["size"], "size_h": core.human_size(v["size"])}
            for k, v in sorted(by_dir.items(), key=lambda x: -x[1]["size"])
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write(f"[cleanup] {self.address_string()} - {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._serve_html()
            return
        if parsed.path == "/api/scan":
            qs = parse_qs(parsed.query)
            before_str = (qs.get("before") or [""])[0]
            try:
                before = dt.datetime.strptime(before_str, "%Y-%m-%d").date()
            except ValueError:
                json_response(self, 400, {"error": "invalid before, need YYYY-MM-DD"})
                return
            json_response(self, 200, do_scan(before))
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/apply":
            self.send_error(404)
            return
        body = parse_body(self)
        before_str = body.get("before", "")
        include_today = bool(body.get("include_today"))
        try:
            before = dt.datetime.strptime(before_str, "%Y-%m-%d").date()
        except ValueError:
            json_response(self, 400, {"error": "invalid before"})
            return

        today = dt.date.today()
        hits = core.collect(before)
        if not hits:
            json_response(self, 200, {"moved": 0, "trash": None, "message": "没有匹配文件"})
            return

        today_hits = [h for h in hits if h[2] == today]
        if today_hits and not include_today:
            json_response(self, 409, {
                "error": "today_files_present",
                "today_count": len(today_hits),
                "today": today.isoformat(),
            })
            return

        moved, trash_root = core.move_to_trash([p for p, _, _ in hits])
        json_response(self, 200, {
            "moved": moved,
            "total": len(hits),
            "trash": str(trash_root),
        })

    def _serve_html(self) -> None:
        if not HTML_FILE.exists():
            self.send_error(500, "cleanup.html missing")
            return
        body = HTML_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"清理工具已启动：{url}")
    print("Ctrl+C 停止")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
