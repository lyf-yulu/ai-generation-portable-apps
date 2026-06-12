#!/bin/bash
cd "$(dirname "$0")/portal"

echo "Stopping existing processes..."
pkill -9 -f "Python app.py" 2>/dev/null
pkill -9 -f "python3 app.py" 2>/dev/null
pkill -9 -f "python3.12 app.py" 2>/dev/null
pkill -9 -f "osascript.*choose folder" 2>/dev/null
sleep 2

PYTHON="/opt/homebrew/bin/python3.12"
if [ ! -x "$PYTHON" ]; then
  echo "Error: Homebrew Python not found at $PYTHON"
  echo "Please install: brew install python@3.12"
  read -p "Press Enter to exit..."
  exit 1
fi

echo "Starting AI Generation Portal on port 9090 (HTTPS)..."
echo "Access at: https://127.0.0.1:9090"
sleep 2 && open "https://127.0.0.1:9090" &
$PYTHON app.py
