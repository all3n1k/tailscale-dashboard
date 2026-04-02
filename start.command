#!/bin/bash
# Tailnet Dashboard — macOS launcher
# Double-click this file in Finder to start (or run in terminal)

cd "$(dirname "$0")"

echo "Installing / checking dependencies..."
pip3 install -q flask flask-sock paramiko 2>/dev/null \
  || python3 -m pip install -q flask flask-sock paramiko

echo "Stopping any existing instance..."
pkill -f "dashboard.py" 2>/dev/null
sleep 1

echo "Starting Tailnet Dashboard..."
nohup python3 "$(pwd)/dashboard.py" > "$(pwd)/dashboard.log" 2>&1 &
sleep 2

echo "Opening browser..."
open http://localhost:5555

echo ""
echo "Dashboard running at http://localhost:5555"
echo "Log: $(pwd)/dashboard.log"
echo "(Close this window or press Ctrl-C — the server keeps running)"
