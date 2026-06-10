#!/bin/bash
cd "$(dirname "$0")/portal"
echo "Starting AI Generation Portal on port 9090..."
echo "Access at: http://127.0.0.1:9090"
sleep 2 && open "http://127.0.0.1:9090" &
python3 app.py
