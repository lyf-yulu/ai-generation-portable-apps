#!/bin/bash
set -euo pipefail

APP_TITLE="Nano Banana 多图生成器"
PORT_START="8797"
PORT_END="8899"

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

RUNTIME_DIR="$APP_DIR/.portable_python"
PYTHON="$RUNTIME_DIR/python/bin/python3"
LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/startup_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"

pause_exit() {
  echo ""
  echo "窗口即将退出。"
  read -r -p "按回车退出..." || true
}

on_error() {
  code=$?
  echo ""
  echo "启动失败，错误码：$code"
  echo "日志位置：$LOG_FILE"
  echo ""
  echo "常见原因："
  echo "1. app.py 不在这个 .command 同一个文件夹里。"
  echo "2. 首次运行需要联网下载便携 Python。"
  echo "3. 下载被网络阻断，可以换网络后重试。"
  pause_exit
  exit "$code"
}

trap on_error ERR

exec > >(tee -a "$LOG_FILE") 2>&1

echo "$APP_TITLE"
echo "程序目录：$APP_DIR"
echo "日志文件：$LOG_FILE"
echo ""

if [[ ! -f "$APP_DIR/app.py" ]]; then
  echo "找不到 app.py。"
  echo "请确认你没有单独移动这个 .command，它必须和 app.py、static 文件夹放在同一个目录。"
  pause_exit
  exit 1
fi

have_good_system_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  python3 - <<'PY' >/dev/null 2>&1
import sys
# app.py 里可能用到 cgi，Python 3.13+ 已移除 cgi，所以这里最多接受 3.12。
raise SystemExit(0 if (sys.version_info.major == 3 and sys.version_info.minor <= 12) else 1)
PY
}

download_file() {
  url="$1"
  out="$2"
  echo "正在下载：$url"
  curl -L --fail --connect-timeout 20 --retry 3 --retry-delay 2 -o "$out" "$url"
}

install_portable_python() {
  arch="$(uname -m)"
  case "$arch" in
    arm64)
      file="cpython-3.11.14+20260203-aarch64-apple-darwin-install_only.tar.gz"
      ;;
    x86_64)
      file="cpython-3.11.14+20260203-x86_64-apple-darwin-install_only.tar.gz"
      ;;
    *)
      echo "不支持的 Mac 架构：$arch"
      exit 1
      ;;
  esac

  tmpdir="$(mktemp -d "$APP_DIR/.python_download.XXXXXX")"
  archive="$tmpdir/python.tar.gz"

  echo "未检测到可用 Python，开始安装本地便携 Python。"
  echo "本操作不会安装到系统，也不需要 Xcode / xcode-select / 管理员权限。"
  echo ""

  primary="https://github.com/astral-sh/python-build-standalone/releases/download/20260203/$file"
  mirror="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/20260203/$file"

  if ! download_file "$primary" "$archive"; then
    echo "GitHub 下载失败，尝试国内镜像..."
    download_file "$mirror" "$archive"
  fi

  rm -rf "$RUNTIME_DIR"
  mkdir -p "$RUNTIME_DIR"
  echo "正在解压 Python..."
  tar -xzf "$archive" -C "$RUNTIME_DIR"
  rm -rf "$tmpdir"

  if [[ ! -x "$PYTHON" ]]; then
    echo "便携 Python 解压失败：$PYTHON 不存在。"
    exit 1
  fi

  "$PYTHON" --version
}

if [[ -x "$PYTHON" ]]; then
  echo "使用本地便携 Python：$PYTHON"
elif have_good_system_python; then
  PYTHON="$(command -v python3)"
  echo "使用系统 Python：$PYTHON"
else
  install_portable_python
fi

mkdir -p outputs archives state

PORT="$("$PYTHON" - <<PY
import socket
start = int("$PORT_START")
end = int("$PORT_END")
for port in range(start, end + 1):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
PY
)"

if [[ -z "${PORT}" ]]; then
  echo "没有找到可用端口，请关闭一些本地服务后重试。"
  pause_exit
  exit 1
fi

export PORT
URL="http://127.0.0.1:${PORT}"

echo "启动本地服务..."
"$PYTHON" app.py &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "等待服务就绪..."
ready=0
for i in {1..120}; do
  if curl -sS "${URL}/api/config" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "本地服务进程提前退出。"
    exit 1
  fi
  sleep 0.25
done

if [[ "$ready" != "1" ]]; then
  echo "服务启动超时。"
  exit 1
fi

open "$URL"
echo ""
echo "已启动：${URL}"
echo "关闭这个窗口会停止本地服务。"
wait "$SERVER_PID"
