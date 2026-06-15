#!/usr/bin/env python3
"""AI Generation Portal — macOS 启动器 (tkinter GUI)
双点 .command 文件或终端执行 python3 启动器.command
"""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext

ROOT = Path(__file__).resolve().parent
PORTAL_DIR = ROOT / "portal"
PID_FILE = PORTAL_DIR / ".launcher_pid.json"
PORTS = [8787, 8797, 8888, 9089, 9090]

# ---------- process helpers ----------

def find_python() -> str | None:
    for candidate in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]:
        if Path(candidate).exists():
            return candidate
    return None

def _pids() -> dict[str, int]:
    if PID_FILE.exists():
        try:
            return json.loads(PID_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_pids(data: dict[str, int]):
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data))

def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def status_text() -> str:
    pids = _pids()
    if not pids:
        return "未运行"
    alive = {k: v for k, v in pids.items() if _is_alive(v)}
    if not alive:
        return "未运行（上次异常退出）"
    port_info = ", ".join(f"{k}:{v}" for k, v in sorted(alive.items()))
    return f"运行中 — {port_info}"

def stop_all(log_cb=None):
    """Graceful stop first, then force-kill remaining."""
    pids = _pids()
    stopped = []
    for name, pid in list(pids.items()):
        if _is_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                if log_cb:
                    log_cb(f"  发送 SIGTERM → {name} (pid {pid})")
            except OSError:
                pass
    time.sleep(2)
    for name, pid in list(pids.items()):
        if _is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                if log_cb:
                    log_cb(f"  强制终止 → {name} (pid {pid})")
            except OSError:
                pass
    # Also clean up any orphans on project ports
    for port in PORTS:
        try:
            out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
            for line in out.splitlines():
                pid = int(line.strip())
                os.kill(pid, signal.SIGKILL)
                if log_cb:
                    log_cb(f"  清理端口 {port} (pid {pid})")
        except Exception:
            pass
    _save_pids({})
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)

def record_child_pid(name: str):
    pids = _pids()
    found = None
    for port in PORTS:
        if not found:
            try:
                out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
                for line in out.splitlines():
                    pid = int(line.strip())
                    found = (name, pid)
                    break
            except Exception:
                pass
    if found:
        pids[found[0]] = found[1]
        _save_pids(pids)

# ---------- GUI ----------

class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AI Generation Portal 启动器")
        self.root.geometry("500x420")
        self.root.resizable(True, True)
        self.running = False
        self.process: subprocess.Popen | None = None

        # --- top bar ---
        top = tk.Frame(root)
        top.pack(fill=tk.X, padx=12, pady=(12, 4))
        tk.Label(top, text="AI Generation Portal", font=("Helvetica", 16, "bold")).pack(side=tk.LEFT)
        self.status_label = tk.Label(top, text=status_text(), fg="gray")
        self.status_label.pack(side=tk.RIGHT)

        # --- buttons ---
        btn_frame = tk.Frame(root)
        btn_frame.pack(fill=tk.X, padx=12, pady=4)
        self.start_btn = tk.Button(btn_frame, text="启动", command=self.start, width=10, bg="#4CAF50", fg="white")
        self.start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.restart_btn = tk.Button(btn_frame, text="重启", command=self.restart, width=10)
        self.restart_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = tk.Button(btn_frame, text="停止", command=self.stop, width=10, bg="#f44336", fg="white")
        self.stop_btn.pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="打开网关", command=self.open_browser, width=10).pack(side=tk.RIGHT)

        # --- log ---
        tk.Label(root, text="日志", anchor="w").pack(fill=tk.X, padx=12, pady=(8, 0))
        self.log = scrolledtext.ScrolledText(root, height=14, font=("Menlo", 10), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.log.insert(tk.END, "就绪。点击「启动」开始所有服务。\n")
        self.log.see(tk.END)

        self._update_status()
        self.root.after(3000, self._poll_status)

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.insert(tk.END, f"[{ts}] {msg}\n")
        self.log.see(tk.END)

    def _update_status(self):
        self.status_label.config(text=status_text())
        alive = bool(_pids())
        if self.running or alive:
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.restart_btn.config(state=tk.NORMAL)
        else:
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.restart_btn.config(state=tk.DISABLED)

    def _poll_status(self):
        self._update_status()
        self.root.after(3000, self._poll_status)

    def start(self):
        python = find_python()
        if not python:
            messagebox.showerror("错误", "未找到 Python 3。请安装 Homebrew Python:\n  brew install python@3.12")
            return
        self._log(f"Python: {python}")
        self._log("启动 Portal + 子应用...")
        self.running = True
        self._update_status()
        threading.Thread(target=self._run, args=(python,), daemon=True).start()

    def _run(self, python: str):
        try:
            self.process = subprocess.Popen(
                [python, "app.py"],
                cwd=str(PORTAL_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            # Give Portal time to spawn sub-apps, then capture their PIDs
            time.sleep(4)
            record_child_pid("portal(9090)")
            record_child_pid("seedance(8787)")
            record_child_pid("nano-banana(8797)")
            record_child_pid("dreamina(8888)")
            self.root.after(0, lambda: self._log(f"  已启动，PID: {_pids()}"))
            self.root.after(0, self._update_status)
            for line in self.process.stdout:
                stripped = line.rstrip()
                if stripped:
                    self.root.after(0, lambda s=stripped: self._log(s))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"错误: {e}"))
        finally:
            self.running = False
            self.root.after(0, self._update_status)

    def stop(self):
        self._log("正在停止所有服务...")
        if self.process and self.process.poll() is None:
            self.process.terminate()
        stop_all(log_cb=self._log)
        self.process = None
        self.running = False
        self._log("已停止。")
        self._update_status()

    def restart(self):
        self._log("重启中...")
        self.stop()
        self.root.after(1500, self.start)

    def open_browser(self):
        import webbrowser
        webbrowser.open("https://127.0.0.1:9090")
        self._log("已打开浏览器: https://127.0.0.1:9090")


def main():
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
