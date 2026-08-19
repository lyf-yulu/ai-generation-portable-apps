#!/usr/bin/env python3
"""Pack production user-data for a seamless move to a new machine.

Goal: on the new box, after `git clone` + venv setup, unpack this archive and
the 26 users log in with their SAME passwords (pw_hash migrates), stats/history
are intact, sub-app keys/accounts are present, and outputs/archives are there —
i.e. a "no-op" source switch for end users.

INCLUDED (the data that makes users whole):
  - portal/state: users.json, user_keys.json, usage.json(+.bak), feishu.json, reports/
  - <app>/state: activity_log.json, preset.json, secrets.json, deepseek.key,
                 accounts.json(+.bak), download_files.json, history.json.legacy.bak
  - <app>/: outputs/, archives/, accounts/, uploads/

EXCLUDED (would fail / be meaningless / travels another way):
  - sessions.json      → login tokens tied to this box; users just re-login
  - certs/*.pem|*.key  → bound to this machine's IP; new box regenerates them
  - usage.pre-backfill*→ one-off backfill snapshot, not live data
  - state/workspaces/, state/media/ → draft caches (users re-upload refs)
  - code, .venv, .git  → the new box gets code via `git clone`

Usage:
    python3 tools/pack_user_data.py                 # dry-run: list what goes in
    python3 tools/pack_user_data.py --apply         # write the tar.gz

New machine:
    git clone <repo> && cd <repo>
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    tar xzf user-data-YYYYMMDD.tar.gz                # unpack at repo root
    # fill feishu-output-sync/config.json if using the mover
    # start via launchd/systemd — ensure_certs() makes a fresh cert for the new IP
"""
from __future__ import annotations

import os
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_NAME = "user-data.tar.gz"  # date stamp appended by caller if desired

APPS = ["seedance", "nano-banana", "dreamina", "volcengine-portrait"]

# portal/state: whitelist the files that matter; skip sessions/pre-backfill/scripts/certs.
PORTAL_STATE_KEEP = {
    "users.json", "user_keys.json", "usage.json", "usage.json.bak",
    "feishu.json",
}
PORTAL_STATE_KEEP_DIRS = {"reports"}

# <app>/state: whitelist trunk files; skip workspaces/media/.gitkeep.
APP_STATE_KEEP_SUFFIX = (".json", ".key", ".bak", ".legacy.bak")
APP_STATE_SKIP_DIRS = {"workspaces", "media", "logs", "refmedia"}

# <app>/ data dirs to include wholesale.
APP_DATA_DIRS = ["outputs", "archives", "accounts", "uploads"]


def _collect() -> list[tuple[Path, str]]:
    """Return [(abs_path, arcname)] pairs to add to the tar."""
    items: list[tuple[Path, str]] = []

    # portal/state whitelisted files
    pstate = ROOT / "portal" / "state"
    for name in PORTAL_STATE_KEEP:
        f = pstate / name
        if f.is_file():
            items.append((f, f"portal/state/{name}"))
    for d in PORTAL_STATE_KEEP_DIRS:
        dd = pstate / d
        if dd.is_dir():
            for f in dd.rglob("*"):
                if f.is_file():
                    items.append((f, str(f.relative_to(ROOT))))

    # each app: state trunk + data dirs
    for app in APPS:
        st = ROOT / app / "state"
        if st.is_dir():
            for f in st.iterdir():
                if f.is_dir():
                    continue  # skip workspaces/media/etc entirely
                if f.name == ".gitkeep":
                    continue
                if f.name.endswith(APP_STATE_KEEP_SUFFIX):
                    items.append((f, f"{app}/state/{f.name}"))
        for sub in APP_DATA_DIRS:
            dd = ROOT / app / sub
            if dd.is_dir():
                for f in dd.rglob("*"):
                    if f.is_file():
                        items.append((f, str(f.relative_to(ROOT))))
    return items


def main() -> int:
    apply = "--apply" in sys.argv
    items = _collect()

    total_bytes = sum(f.stat().st_size for f, _ in items if f.exists())
    print(f"{'PACK' if apply else 'DRY-RUN'} — {len(items)} files, "
          f"{total_bytes/1024/1024:.1f} MB uncompressed\n")

    # summarize by top group
    from collections import defaultdict
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for f, arc in items:
        key = "/".join(arc.split("/")[:2])
        groups[key][0] += 1
        groups[key][1] += f.stat().st_size if f.exists() else 0
    for key in sorted(groups):
        n, b = groups[key]
        print(f"  {key}/: {n} files, {b/1024/1024:.1f} MB")

    # explicit "excluded" note so nothing surprises the operator
    print("\nEXCLUDED (by design): sessions.json, certs/, usage.pre-backfill*, "
          "state/workspaces/, state/media/, code/.venv/.git")

    if not apply:
        print("\nDRY-RUN. Nothing written. Re-run with --apply to create the tar.gz.")
        return 0

    out = ROOT.parent / OUT_NAME
    with tarfile.open(out, "w:gz") as tar:
        for f, arc in items:
            tar.add(f, arcname=arc)
    print(f"\nWROTE {out} ({out.stat().st_size/1024/1024:.1f} MB compressed)")
    print("Copy it to the new machine and `tar xzf` at the repo root.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
