#!/usr/bin/with-contenv bashio

export HA_TOKEN="$(bashio::config 'ha_url' 2>/dev/null || true)"
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"

exec python3 /app/src/main.py
