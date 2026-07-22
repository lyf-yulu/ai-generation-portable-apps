"""Read-only scanner over the sub-apps' outputs directories.

Layout produced by seedance / nano-banana / dreamina / volcengine-portrait:

    <app>/outputs/<username>/<YYYY-MM-DD>/<timestamp>_run*_*.{mp4,png,jpg,webp}

This module ONLY reads. It never deletes, moves, or rewrites any source file.
Each media file becomes one Artifact carrying the four dimensions we care about
(app / user / date / file) plus size+mtime for fingerprinting.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

# Media extensions we upload. Everything else (.json sidecars, .part, .DS_Store)
# is ignored.
MEDIA_EXTS = {".mp4", ".png", ".jpg", ".jpeg", ".webp"}

# A file whose mtime is younger than this many seconds is assumed to still be
# written (a job flushing its result) and is skipped this round; the next scan
# picks it up once it has settled.
MIN_AGE_SECONDS = 10


@dataclass(frozen=True)
class Artifact:
    app: str          # sub-app name, e.g. "seedance"
    user: str         # sanitized username directory, e.g. "苏湘"
    date: str         # "YYYY-MM-DD" directory
    path: Path        # absolute path to the media file
    size: int         # bytes
    mtime: float      # file mtime (epoch seconds) == generation time

    @property
    def filename(self) -> str:
        return self.path.name

    def generated_at(self) -> str:
        """Human-readable local generation time from mtime."""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.mtime))

    def fields(self) -> dict[str, str]:
        """Bitable record fields (attachment added separately by the uploader)."""
        return {
            "文件名": self.filename,
            "日期": self.date,
            "生成时间": self.generated_at(),
            "子应用": self.app,
        }


def _is_date_dir(name: str) -> bool:
    """Cheap YYYY-MM-DD shape check without pulling in datetime parsing."""
    if len(name) != 10 or name[4] != "-" or name[7] != "-":
        return False
    y, m, d = name[:4], name[5:7], name[8:10]
    return y.isdigit() and m.isdigit() and d.isdigit()


def scan(roots: dict[str, Path], *, now: float | None = None) -> list[Artifact]:
    """Walk each app's outputs root and yield uploadable Artifacts.

    roots: {app_name: outputs_dir_path}. Missing/non-dir roots are skipped so a
    sub-app that has never produced anything doesn't break the scan.
    now: injectable clock for tests (defaults to time.time()).
    """
    current = time.time() if now is None else now
    artifacts: list[Artifact] = []

    for app, outputs_dir in roots.items():
        base = Path(outputs_dir)
        try:
            if not base.is_dir():
                continue
            user_dirs = [p for p in base.iterdir() if p.is_dir()]
        except OSError:
            continue

        for user_dir in user_dirs:
            user = user_dir.name
            try:
                date_dirs = [p for p in user_dir.iterdir() if p.is_dir()]
            except OSError:
                continue

            for date_dir in date_dirs:
                if not _is_date_dir(date_dir.name):
                    continue
                try:
                    files = [p for p in date_dir.iterdir() if p.is_file()]
                except OSError:
                    continue

                for f in files:
                    if f.suffix.lower() not in MEDIA_EXTS:
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if st.st_size <= 0:
                        continue
                    if current - st.st_mtime < MIN_AGE_SECONDS:
                        continue  # still being written; catch it next round
                    artifacts.append(
                        Artifact(
                            app=app,
                            user=user,
                            date=date_dir.name,
                            path=f.resolve(),
                            size=st.st_size,
                            mtime=st.st_mtime,
                        )
                    )

    # Stable order: oldest first, so the bitable rows accrue chronologically.
    artifacts.sort(key=lambda a: (a.mtime, str(a.path)))
    return artifacts
