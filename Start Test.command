#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=== 测试环境启动 ==="
echo ""

# 停止旧的测试进程
for port in 9190 9189 8788 8798 8890 8892; do
  pids=$(lsof -ti :$port 2>/dev/null | sort -u)
  for pid in $pids; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null)
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
    if echo "$cmd" | grep -q "app.py"; then
      case "$cwd" in
        "$DIR"/*)
          kill -9 "$pid" 2>/dev/null && echo "  已停止 $cwd (port $port, pid $pid)"
          ;;
      esac
    fi
  done
done

sleep 1

# 查找 Python
PYTHON=""
for p in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if "$p" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
    PYTHON="$p"; break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Error: Python 3.9+ not found."
  read -p "Press Enter to exit..."
  exit 1
fi
echo "Python: $PYTHON ($($PYTHON --version))"

# 设置测试环境变量
export TEST_MODE=1
export PORTAL_PORT=9190
export REDIRECT_PORT=9189
export SEEDANCE_PORT=8788
export NANO_PORT=8798
export DREAMINA_PORT=8890
export VOLCENGINE_PORTRAIT_PORT=8892
export DATA_DIR="$DIR/portal/test-data"

# Stage 2: sub-app engine switch. Set to `fastapi` to launch app_fastapi.py
# via uvicorn (requires .venv from requirements.txt); default `stdlib` runs
# the legacy app.py directly. Per-app override so we can flip one at a time.
export NANO_BANANA_ENGINE=fastapi
export SEEDANCE_ENGINE=${SEEDANCE_ENGINE:-fastapi}
export DREAMINA_ENGINE=${DREAMINA_ENGINE:-stdlib}
export VOLCENGINE_PORTRAIT_ENGINE=${VOLCENGINE_PORTRAIT_ENGINE:-fastapi}

echo ""
echo "端口分配:"
echo "  Portal HTTPS:      https://127.0.0.1:9190"
echo "  Portal HTTP→HTTPS: http://127.0.0.1:9189"
echo "  Seedance:          :8788"
echo "  Nano Banana:       :8798"
echo "  Dreamina:          :8890"
echo "  Volcengine 人像:   :8892"
echo ""
echo "数据目录: $DATA_DIR"
echo "子应用数据: seedance/test-data/  nano-banana/test-data/  dreamina/test-data/  volcengine-portrait/test-data/"
echo ""

sleep 2 && open "https://127.0.0.1:9190" &
cd portal
exec "$PYTHON" app.py
