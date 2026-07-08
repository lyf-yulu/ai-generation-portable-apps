#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "停止测试环境..."

for port in 9190 9189 8788 8798 8890 8892; do
  pids=$(lsof -ti :$port 2>/dev/null | sort -u)
  for pid in $pids; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null)
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
    if echo "$cmd" | grep -q "app.py"; then
      case "$cwd" in
        "$DIR"/*)
          kill "$pid" 2>/dev/null && echo "  已停止 $cwd (port $port, pid $pid)"
          ;;
      esac
    fi
  done
done

sleep 1

# 强制清理残留
for port in 9190 9189 8788 8798 8890 8892; do
  pids=$(lsof -ti :$port 2>/dev/null | sort -u)
  for pid in $pids; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null)
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
    if echo "$cmd" | grep -q "app.py"; then
      case "$cwd" in
        "$DIR"/*)
          kill -9 "$pid" 2>/dev/null && echo "  强制停止 $cwd (port $port, pid $pid)"
          ;;
      esac
    fi
  done
done

echo "测试环境已停止"
