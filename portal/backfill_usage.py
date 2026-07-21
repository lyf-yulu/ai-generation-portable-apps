#!/usr/bin/env python3
"""One-shot usage backfill for the X-Job-Id outage (2026-07-15 onward).

Background
----------
The FastAPI engines for seedance / nano-banana returned a bare dict from their
job-creation endpoints, so the `X-Job-Id` response header was never emitted.
Portal's _proxy only registers usage stats when that header is present, so from
2026-07-15 no seedance seconds / nano-banana images were tracked. The code fix
lives in seedance/app_fastapi.py + nano-banana/app_fastapi.py.

This script rebuilds the LOST per-user stats from each sub-app's
`activity_log.json`, which stores the real per-job `duration` and `done` counts.

Scope / limits
--------------
- activity_log.json keeps only the last ACTIVITY_LIMIT (=100) records per app,
  so only the most recent days survive (typically 07-20 / 07-21). Earlier outage
  days (07-15..07-19) are NOT recoverable from activity — their per-job duration
  was never persisted. usage-*.jsonl has the job COUNT for those days but not the
  duration, so seconds cannot be reconstructed accurately and are left alone.
- seedance metric = seconds = sum(done * duration) per (day, user)
- nano-banana metric = images = sum(done) per (day, user)
- Records with a missing/empty username are SKIPPED (cannot attribute).

Safety
------
- MUST run with Portal stopped. Portal holds usage.json in memory and flushes a
  full snapshot on any _save(), which would overwrite an in-place edit. The
  script refuses to run if it detects the portal process, unless --force.
- Idempotent: writes an audit marker under _backfill_applied in usage.json. A day
  already marked as backfilled is skipped so re-runs never double-count.
- Dry-run by default: prints the planned diff and writes nothing. Pass --apply.
- On --apply it first copies usage.json -> usage.pre-backfill.<ts>.json.

Usage
-----
    python3 portal/backfill_usage.py            # dry-run, prints plan
    python3 portal/backfill_usage.py --apply     # writes usage.json
    python3 portal/backfill_usage.py --apply --force   # skip portal-running guard
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE_PATH = ROOT / "portal" / "state" / "usage.json"
SOURCES = [
    # (activity_log path, app name, metric): metric "sec" => done*duration, "img" => done
    (ROOT / "seedance" / "state" / "activity_log.json", "seedance", "sec"),
    (ROOT / "nano-banana" / "state" / "activity_log.json", "nano-banana", "img"),
]
TERMINAL_OK = {"succeeded", "completed", "success"}


def _day_of(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _portal_running() -> bool:
    """Best-effort: is a portal/app.py process alive?"""
    try:
        out = subprocess.run(["pgrep", "-fl", "app.py"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    for line in out.splitlines():
        # portal runs `python app.py` from the portal/ dir; the sub-apps run uvicorn
        if "app.py" in line and "backfill_usage" not in line and "uvicorn" not in line:
            return True
    return False


def compute_plan() -> dict:
    """Return {day: {user: {app: value}}} rebuilt from activity logs."""
    plan: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    skipped_no_user = 0
    for path, app, metric in SOURCES:
        if not path.exists():
            print(f"  ! activity log missing: {path}", file=sys.stderr)
            continue
        items = json.loads(path.read_text(encoding="utf-8"))
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("status") not in TERMINAL_OK:
                continue
            user = (it.get("username") or "").strip()
            if not user:
                skipped_no_user += 1
                continue
            day = _day_of(it.get("started_at") or it.get("finished_at"))
            if not day:
                continue
            result = it.get("result") or {}
            if not isinstance(result, dict):
                continue
            done = int(result.get("done") or 0)
            if done <= 0:
                continue
            if metric == "sec":
                dur = int(result.get("duration") or 0)
                plan[day][user][app] += done * dur
            else:
                plan[day][user][app] += done
    if skipped_no_user:
        print(f"  (skipped {skipped_no_user} succeeded records with no username)")
    return plan


def main() -> int:
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv

    if apply and _portal_running() and not force:
        print("REFUSING: portal app.py appears to be running. Stop it first "
              "(launchctl unload / kill) so its in-memory usage.json snapshot "
              "cannot overwrite this edit. Re-run with --force to override.",
              file=sys.stderr)
        return 2

    if not USAGE_PATH.exists():
        print(f"usage.json not found at {USAGE_PATH}", file=sys.stderr)
        return 1

    data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    by_user = data.setdefault("by_user", {})
    applied_marker = data.setdefault("_backfill_applied", {})  # day -> ts

    plan = compute_plan()

    total_changes = 0
    print("\n=== BACKFILL PLAN (activity_log → usage.json by_user) ===")
    for day in sorted(plan):
        if applied_marker.get(day):
            print(f"--- {day}: already backfilled at "
                  f"{datetime.fromtimestamp(applied_marker[day]).isoformat()}, SKIP ---")
            continue
        day_bucket = by_user.get(day, {})
        print(f"--- {day} ---")
        for user in sorted(plan[day]):
            for app, value in plan[day][user].items():
                existing = day_bucket.get(user, {}).get(app, {})
                cur_img = int(existing.get("images", 0) or 0)
                cur_sec = int(existing.get("seconds", 0) or 0)
                if app == "seedance":
                    new_sec, new_img = value, 0
                else:
                    new_sec, new_img = 0, value
                # Only fill when the target is currently empty for this metric;
                # never stack on top of a non-zero value (avoids double count if
                # some rows were partially tracked).
                metric = "seconds" if app == "seedance" else "images"
                cur = cur_sec if app == "seedance" else cur_img
                unit = "秒" if app == "seedance" else "张"
                if cur > 0:
                    print(f"    {user} {app}: has {cur}{unit} already, leave as-is")
                    continue
                print(f"    {user} {app}: {metric} {cur} -> {value}{unit}")
                total_changes += 1
                if apply:
                    u = by_user.setdefault(day, {}).setdefault(user, {})
                    slot = u.setdefault(app, {"images": 0, "seconds": 0})
                    slot["images"] = new_img
                    slot["seconds"] = new_sec

    print(f"\n{total_changes} (user, app) cells to fill.")

    if not apply:
        print("\nDRY-RUN. Nothing written. Re-run with --apply to write usage.json.")
        return 0

    if total_changes == 0:
        print("Nothing to apply.")
        return 0

    # backup, mark, write
    ts = int(time.time())
    backup = USAGE_PATH.with_name(f"usage.pre-backfill.{ts}.json")
    shutil.copyfile(USAGE_PATH, backup)
    for day in plan:
        if not applied_marker.get(day):
            applied_marker[day] = ts
    tmp = USAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, USAGE_PATH)
    print(f"\nAPPLIED. Backup saved to {backup.name}. "
          f"Start portal now so it _load()s the backfilled file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
