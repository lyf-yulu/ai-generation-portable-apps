#!/usr/bin/env python3
"""feishu-output-sync entry point.

One round = scan outputs -> skip already-synced -> for each new artifact,
ensure the user's bitable exists, upload the media, append a record.

    python3 sync.py --once          # single pass, then exit
    python3 sync.py                 # loop forever, sleeping poll_interval

Fully independent: reads sub-app outputs directories (read-only) and talks to
Feishu over outbound HTTPS. Touches nothing in portal / the sub-apps / Codex's
feishu-generation-agent. A per-file failure is logged and skipped; it is NOT
recorded in the registry, so the next round retries it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from feishu import FeishuClient, FeishuError
from registry import Registry, fingerprint
from scanner import scan

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_DB = HERE / "state" / "sync.sqlite3"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"配置文件不存在: {path}\n"
            f"请复制 config.example.json 为 config.json 并填写 app_id/app_secret/"
            f"folder_token 和 user_open_ids。"
        )
    cfg = json.loads(path.read_text("utf-8"))
    for key in ("app_id", "app_secret"):
        if not cfg.get(key):
            raise SystemExit(f"配置缺少必填项: {key}")
    return cfg


def resolve_roots(cfg: dict) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for app, raw in (cfg.get("outputs_roots") or {}).items():
        p = Path(raw)
        if not p.is_absolute():
            p = (HERE / p).resolve()
        roots[app] = p
    return roots


def run_once(client: FeishuClient, registry: Registry, cfg: dict,
             roots: dict[str, Path], *, limit: int = 0) -> dict:
    stats = {"scanned": 0, "skipped_seen": 0, "uploaded": 0, "failed": 0}

    artifacts = scan(roots)
    stats["scanned"] = len(artifacts)

    for art in artifacts:
        # --limit N: stop after N successful uploads this round (smoke testing).
        if limit and stats["uploaded"] >= limit:
            _log(f"达到 --limit {limit},本轮停止(其余留待后续)")
            break
        fp = fingerprint(art)
        if registry.seen(fp):
            stats["skipped_seen"] += 1
            continue

        try:
            app_token, tables = client.ensure_base_for_user(art.user, registry)
            table_id = tables.get(art.app)
            if not table_id:
                raise FeishuError(f"用户 {art.user} 的表格缺少子应用附表: {art.app}")
            file_token = client.upload_media(app_token, art.path)
            record_id = client.add_record(
                app_token, table_id, art.fields(), file_token
            )
            registry.mark(fp, art, record_id)
            stats["uploaded"] += 1
            _log(f"已搬运: {art.user} / {art.app} / {art.filename} -> {record_id}")
        except (FeishuError, OSError) as exc:
            stats["failed"] += 1
            _log(f"失败(下轮重试): {art.user} / {art.app} / {art.filename} — {exc}")
            # NOT marked in registry -> retried next round.

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书产出搬运器")
    parser.add_argument("--once", action="store_true",
                        help="只跑一轮后退出(默认循环)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=0,
                        help="每轮最多上传 N 个(0=不限,用于冒烟联调)")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    roots = resolve_roots(cfg)
    client = FeishuClient(
        cfg["app_id"], cfg["app_secret"],
        folder_token=cfg.get("folder_token", ""),
    )
    registry = Registry(Path(args.db))

    interval = int(cfg.get("poll_interval_seconds", 300))

    try:
        while True:
            try:
                stats = run_once(client, registry, cfg, roots, limit=args.limit)
                _log(f"本轮完成: {stats}")
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                _log(f"本轮异常(将继续下一轮): {type(exc).__name__}: {exc}")
            if args.once:
                break
            time.sleep(interval)
    finally:
        registry.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
