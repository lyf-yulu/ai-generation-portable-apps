#!/usr/bin/env python3
"""One-shot: collect scattered outputs (custom-path folders outside outputs/)
into outputs/_待认领/<原文件夹名>/ so the Feishu sync can pick them up.

Background: before the output-path lock, remote users' custom output_dir wrote
folders straight into the repo root (【公交车】7.15 蔡月琴, 7015, 722, ...).
Those sit outside <app>/outputs/ and are invisible to feishu-output-sync.
122/129 can't be attributed to a login user (activity_log only keeps ~100
recent records), so we don't guess — everything goes under a single
"_待认领" (to-be-claimed) user dir, preserving the original business folder
name as a subdir for humans to sort out later.

SAFE: dry-run by default (prints plan, moves nothing). --apply COPIES (not
moves) into outputs/_待认领/, leaving originals in place until you verify.
Use --purge-originals separately, only after you've confirmed the copies.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# Filenames start with a YYYYMMDD timestamp, e.g. 20260716_131009_run1_...mp4
_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


def _date_from_name(name: str) -> str:
    """Extract YYYY-MM-DD from a filename's leading timestamp; '未知日期' if none.
    scanner.py requires outputs/<user>/<YYYY-MM-DD>/file, so the middle dir MUST
    be a real date or the sync will skip it."""
    m = _DATE_RE.search(name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return "未知日期"


def _slug(folder_name: str) -> str:
    """Business folder name → safe filename prefix (keeps it as a claim clue)."""
    s = re.sub(r"[\s/:\\]+", "_", folder_name.strip())
    return s[:40]

ROOT = Path(__file__).resolve().parent.parent
STD_DIRS = {"static", "state", "outputs", "archives", "uploads", "accounts",
            "test-data", "__pycache__", ".venv", "logs"}
MEDIA_EXTS = {".mp4", ".png", ".jpg", ".jpeg", ".webp"}
CLAIM_USER = "_待认领"
APPS = ["seedance", "nano-banana", "dreamina", "volcengine-portrait"]


def scattered_dirs(app: str) -> list[Path]:
    base = ROOT / app
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if not d.is_dir() or d.name in STD_DIRS or d.name.startswith("."):
            continue
        # only folders that actually hold media
        if any(f.is_file() and f.suffix.lower() in MEDIA_EXTS for f in d.rglob("*")):
            out.append(d)
    return out


def media_files(d: Path) -> list[Path]:
    return [f for f in d.rglob("*") if f.is_file() and f.suffix.lower() in MEDIA_EXTS]


def main() -> int:
    apply = "--apply" in sys.argv
    total_files = 0
    total_dirs = 0
    print(f"{'APPLY (copy)' if apply else 'DRY-RUN'} — scattered outputs → outputs/{CLAIM_USER}/\n")
    for app in APPS:
        dirs = scattered_dirs(app)
        if not dirs:
            continue
        print(f"===== {app} ({len(dirs)} folders) =====")
        claim_base = ROOT / app / "outputs" / CLAIM_USER
        for d in sorted(dirs):
            files = media_files(d)
            total_dirs += 1
            total_files += len(files)
            prefix = _slug(d.name)
            print(f"  {d.name!r}: {len(files)} files → "
                  f"outputs/{CLAIM_USER}/<日期>/{prefix}__*")
            if apply:
                for f in files:
                    # Middle dir MUST be a real date (scanner requirement); the
                    # business folder name is preserved as a filename prefix so
                    # it still serves as a claim clue in Feishu.
                    date = _date_from_name(f.name)
                    dest = claim_base / date
                    dest.mkdir(parents=True, exist_ok=True)
                    target = dest / f"{prefix}__{f.name}"
                    if target.exists():
                        target = dest / f"{prefix}__{f.stem}_{abs(hash(str(f)))%10000}{f.suffix}"
                    shutil.copy2(f, target)
    print(f"\n{'COPIED' if apply else 'WOULD COPY'} {total_files} files "
          f"from {total_dirs} folders into outputs/{CLAIM_USER}/")
    if not apply:
        print("\nDRY-RUN. Nothing moved. Re-run with --apply to COPY (originals kept).")
    else:
        print("\nDONE (copies). Originals left in place. Verify in Feishu, then "
              "delete the scattered source folders manually if satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
