#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/portal"

echo "Stopping previous instances (projects ports only)..."
PROJECT_ROOT="$SCRIPT_DIR"
for port in 8787 8797 8888 9089 9090; do
  pids=$(lsof -ti :$port 2>/dev/null | sort -u)
  for pid in $pids; do
    # Only stop Python app.py processes launched from this project.
    cmd=$(ps -p "$pid" -o command= 2>/dev/null)
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
    if echo "$cmd" | grep -q "app.py"; then
      case "$cwd" in
        "$PROJECT_ROOT"/*)
          kill "$pid" 2>/dev/null && echo "  Stopping app.py on port $port (pid $pid)"
          ;;
        *)
          echo "  [WARN] Port $port occupied by app.py outside this project (pid $pid), skipping"
          ;;
      esac
    else
      echo "  [WARN] Port $port occupied by non-project process (pid $pid), skipping"
    fi
  done
done

sleep 1
for port in 8787 8797 8888 9089 9090; do
  pids=$(lsof -ti :$port 2>/dev/null | sort -u)
  for pid in $pids; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null)
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
    if echo "$cmd" | grep -q "app.py"; then
      case "$cwd" in
        "$PROJECT_ROOT"/*)
          kill -9 "$pid" 2>/dev/null && echo "  Force stopped stuck app.py on port $port (pid $pid)"
          ;;
      esac
    fi
  done
done

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
echo "  Local:  https://127.0.0.1:9090"
echo "  HTTP → HTTPS:  http://127.0.0.1:9089"
sleep 2 && open "https://127.0.0.1:9090" &
$PYTHON app.py
