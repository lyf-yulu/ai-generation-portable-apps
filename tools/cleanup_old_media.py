#!/usr/bin/env python3
"""按日期清理生成产物（mp4/png/jpg/jpeg），移到 macOS 回收站。

用法：
    python3 tools/cleanup_old_media.py --before 2026-07-01           # 预览
    python3 tools/cleanup_old_media.py --before 2026-07-01 --apply   # 真删（走回收站）

安全设计：
  1. 白名单目录：只清代码里列出的「输出/合集/浏览器下载/uploads」
  2. 硬保护：路径含 /state/ 或 /portal/ 一律跳过（存档系统、证书）
  3. today 保护：若命中的文件有 mtime==今天 的，必须 --include-today 才允许
     （防止误删正在进行/刚完成的任务产物）
  4. 走回收站：mv 到 ~/.Trash/ai-portable-cleanup-<时间戳>/，10 天内可恢复
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 只清这些目录（相对 REPO_ROOT）。任何未列出的目录一律跳过。
CLEAN_DIRS = [
    "seedance/outputs",
    "seedance/视频生成合集",
    "seedance/图片生成合集",
    "seedance/图",
    "seedance/浏览器下载",
    "seedance/Pictures",
    "seedance/0706",
    "seedance/AI Tool",
    "seedance/【公交车】7.2 ai片头需求 黄敏",
    "seedance/【水排序】7.3 ai片头需求 饶津毓",
    "seedance/【水排序】7.3 ai片头需求2黄淼",
    "seedance/鸟排序:7.6日 汪洪秀 AI需求:2个需求",
    "nano-banana/outputs",
    "nano-banana/图片生成合集",
    "nano-banana/视频生成合集",
    "nano-banana/图",
    "nano-banana/浏览器下载",
    "nano-banana/AI Tool",
    "nano-banana/0706",
    "dreamina/outputs",
    "dreamina/uploads",
    "volcengine-portrait/视频生成合集",
    "volcengine-portrait/uploads",
]

EXTS = {".mp4", ".png", ".jpg", ".jpeg"}

# 无论路径怎么写，只要 parts 里出现这些片段就跳过
FORBIDDEN_PARTS = {"state", "portal", ".git", "static"}


def is_target_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in EXTS:
        return False
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return False
    return True


def collect(before: dt.date) -> list[tuple[Path, int, dt.date]]:
    """返回 (path, size, mtime_date) 列表，按 mtime 升序。"""
    hits: list[tuple[Path, int, dt.date]] = []
    for rel in CLEAN_DIRS:
        root = REPO_ROOT / rel
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not is_target_file(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            mdate = dt.date.fromtimestamp(st.st_mtime)
            if mdate < before:
                hits.append((p, st.st_size, mdate))
    hits.sort(key=lambda x: x[2])
    return hits


def human_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.1f}{unit}"
        n /= step
    return f"{n:.1f}PB"


def move_to_trash(files: list[Path]) -> tuple[int, Path]:
    """把文件移到 ~/.Trash/ai-portable-cleanup-<ts>/。返回成功数和目标根。"""
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = Path.home() / ".Trash" / f"ai-portable-cleanup-{ts}"
    trash_root.mkdir(parents=True, exist_ok=True)
    ok = 0
    for src in files:
        try:
            rel = src.relative_to(REPO_ROOT)
        except ValueError:
            rel = Path(src.name)
        dst = trash_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            ok += 1
        except OSError as e:
            print(f"  跳过 {src}: {e}", file=sys.stderr)
    return ok, trash_root


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", required=True, type=parse_date,
                    help="删除该日期之前修改的文件，格式 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true",
                    help="真的执行（移到回收站）；不加则只预览")
    ap.add_argument("--yes", action="store_true", help="非交互确认（配合 --apply）")
    ap.add_argument("--include-today", action="store_true",
                    help="允许删除今天修改的文件（默认拒绝，防止误删 in-flight 任务）")
    args = ap.parse_args()

    today = dt.date.today()

    print(f"扫描：{REPO_ROOT}")
    print(f"规则：删除 mtime < {args.before} 的 mp4/png/jpg 文件")
    print(f"目录白名单：{len(CLEAN_DIRS)} 个（全部位于子应用输出区，跳过 state/portal）")
    print()

    hits = collect(args.before)
    if not hits:
        print("没有匹配的文件。")
        return 0

    today_hits = [h for h in hits if h[2] == today]
    if today_hits and not args.include_today:
        print()
        print(f"⚠️  命中 {len(today_hits)} 个 mtime==今天({today}) 的文件，这些可能是刚完成或正在生成的产物。")
        for p, sz, _ in today_hits[:5]:
            try:
                rel = p.relative_to(REPO_ROOT)
            except ValueError:
                rel = p
            print(f"    {rel}  ({human_size(sz)})")
        if len(today_hits) > 5:
            print(f"    ... 还有 {len(today_hits)-5} 个")
        print()
        print("要删除今天的新产出，请加 --include-today 显式确认。")
        print("（或改小 --before 只清历史文件）")
        return 3

    total_size = sum(sz for _, sz, _ in hits)
    by_dir: dict[str, tuple[int, int]] = {}
    for p, sz, _ in hits:
        try:
            key = str(p.relative_to(REPO_ROOT).parts[0]) + "/" + str(p.relative_to(REPO_ROOT).parts[1])
        except (ValueError, IndexError):
            key = str(p.parent)
        cnt, s = by_dir.get(key, (0, 0))
        by_dir[key] = (cnt + 1, s + sz)

    print(f"命中 {len(hits)} 个文件，共 {human_size(total_size)}")
    print(f"时间范围：{hits[0][2]} ~ {hits[-1][2]}")
    print()
    print("按目录汇总：")
    for k in sorted(by_dir, key=lambda x: -by_dir[x][1]):
        cnt, sz = by_dir[k]
        print(f"  {cnt:>5} 个  {human_size(sz):>8}  {k}")

    if not args.apply:
        print()
        print("[dry-run] 未执行任何删除。加 --apply 真正移到回收站。")
        return 0

    if not args.yes:
        print()
        ans = input(f"确认把 {len(hits)} 个文件（{human_size(total_size)}）移到 ~/.Trash/？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return 1

    ok, trash_root = move_to_trash([p for p, _, _ in hits])
    print()
    print(f"完成：{ok}/{len(hits)} 个文件已移到 {trash_root}")
    print("提示：macOS 回收站默认 10 天后自动清空。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
