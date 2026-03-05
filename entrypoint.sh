#!/bin/sh
set -e

# ── Seed data directory on first run ──────────────────────────────────────
# The /app/data volume is empty on first container launch. Copy required
# seed files so the server can start. On subsequent runs the volume
# already has data — existing files are never overwritten.

if [ ! -f /app/data/school_registry.json ]; then
  echo "[entrypoint] Seeding school_registry.json"
  cp /app/data-seed/school_registry.json /app/data/
fi

if [ ! -f /app/data/users.json ]; then
  echo "[entrypoint] Seeding users.json"
  cp /app/data-seed/users.json /app/data/
fi

if [ ! -d /app/data/teams ] || [ -z "$(ls -A /app/data/teams 2>/dev/null)" ]; then
  echo "[entrypoint] Seeding teams/"
  cp -r /app/data-seed/teams /app/data/
fi

mkdir -p /app/data/cache

echo "[entrypoint] Data directory ready — starting server"

# ── Hand off to CMD (node server/index.js) ────────────────────────────────
exec "$@"
