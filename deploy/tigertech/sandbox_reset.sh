#!/bin/bash
# Rebuild the practice sandbox from scratch: a fresh pretend Meeting.
# Run by deploy.sh the first time, then nightly by cron, so whatever
# visitors did during the day is cleared away.
set -euo pipefail
cd "$HOME/meetinghouse"
mkdir -p "$HOME/meetinghouse-sandbox"
chmod 700 "$HOME/meetinghouse-sandbox"
rm -rf "$HOME/meetinghouse-sandbox/data"
mkdir -p "$HOME/meetinghouse-sandbox/data"
"$HOME/meetinghouse/venv/bin/python3" sandbox.py "$HOME/meetinghouse-sandbox/data/sandbox.db"
# A changed index.fcgi makes the web server start fresh copies of the
# sandbox, which then read the new database.
touch "$HOME/html/sandbox/index.fcgi" 2>/dev/null || true
