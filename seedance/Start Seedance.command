#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "Seedance 多参考生成器"
echo "程序目录：$APP_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "未检测到 python3。正在尝试打开 macOS Command Line Tools 安装器..."
  xcode-select --install >/dev/null 2>&1 || true
  echo "安装完成后，请重新双击本文件。"
  read -r -p "按回车退出..."
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "首次运行：创建本地 Python 环境..."
  python3 -m venv .venv
fi

PYTHON="$APP_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

mkdir -p outputs archives state

PORT="$("$PYTHON" - <<'PY'
import socket
for port in range(8787, 8899):
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
  read -r -p "按回车退出..."
  exit 1
fi

export PORT
URL="http://127.0.0.1:${PORT}"

"$PYTHON" app.py &
SERVER_PID=$!

for _ in {1..80}; do
  if curl -sS "${URL}/api/config" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

open "$URL"
echo ""
echo "已启动：${URL}"
echo "关闭这个窗口会停止本地服务。"
wait "$SERVER_PID"
