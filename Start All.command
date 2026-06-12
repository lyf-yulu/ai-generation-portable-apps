#!/bin/bash
cd "$(dirname "$0")/portal"

echo "Stopping existing processes on ports 8787-9090..."
for port in 8787 8797 8888 9089 9090; do
  pid=$(lsof -ti :$port 2>/dev/null)
  [ -n "$pid" ] && kill -9 $pid 2>/dev/null && echo "  Freed port $port (pid $pid)"
done
sleep 1

# Find usable Python (try Homebrew first, then system)
PYTHON=""
for candidate in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$candidate" ]; then
    ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ] 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Error: Python 3.9+ not found."
  echo "Please install: brew install python@3.12"
  read -p "Press Enter to exit..."
  exit 1
fi
echo "Using: $PYTHON ($($PYTHON --version))"

echo "Starting AI Generation Portal on port 9090 (HTTPS)..."
echo "Access at: https://127.0.0.1:9090"
sleep 2 && open "https://127.0.0.1:9090" &
$PYTHON app.py
