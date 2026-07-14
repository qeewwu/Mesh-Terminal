#!/bin/bash
# Run on demand on your laptop (or any machine you want a mirror on) — no automation,
# just run this whenever you actually need a local copy. Pulls logs/ (chat history) and
# service-logs/ (mesh-logger.py's own runtime log, see ops/mesh-log-export.sh, run on the
# server first if you want that included) via rsync — additive only (no --delete), so a
# briefly unreachable server just leaves last run's copy in place rather than a partial one.
#
# One-time setup on this machine:
#   - an SSH key already authorized on the server (see CLAUDE.md's Backing up logs section)
#   - optionally, a "mesh-server" alias in ~/.ssh/config so this script needs no config
#
# Override the defaults via env vars if you don't use the "mesh-server" alias, e.g.:
#   MESH_BACKUP_REMOTE=qeewwu@100.99.143.119 ops/sync-logs.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE="${MESH_BACKUP_REMOTE:-mesh-server}"
REMOTE_DIR="${MESH_BACKUP_REMOTE_DIR:-~/Mesh}"
DEST="${MESH_BACKUP_DEST:-backup}"

mkdir -p "$DEST"
rsync -avz "$REMOTE:$REMOTE_DIR/logs/" "$DEST/logs/"
rsync -avz "$REMOTE:$REMOTE_DIR/service-logs/" "$DEST/service-logs/"
