#!/bin/bash
# Run on the mesh-logger server, on demand — no automation, just a manual export.
# Appends journalctl entries for mesh-logger.service logged since the last run into
# service-logs/mesh-logger.log — a plain text file that can then be pulled off the
# server the same way logs/ already is (see ops/sync-logs.sh). A missing stamp file
# (first run, or never run before) falls back to the last 7 days.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p service-logs
STAMP_FILE=service-logs/.last_export
LAST=$(cat "$STAMP_FILE" 2>/dev/null || echo "-7 days")

journalctl -u mesh-logger --since "$LAST" --no-pager -o short-iso >> service-logs/mesh-logger.log
date '+%Y-%m-%d %H:%M:%S' > "$STAMP_FILE"
