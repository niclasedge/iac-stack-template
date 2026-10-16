#!/usr/bin/env bash
# =============================================================================
# example-heartbeat.sh — Reference Script for Structured JSON Logging
# =============================================================================
#
# Demonstrates the recommended logging pattern:
#   - JSON structured logs for precise Kibana queries
#   - Heartbeat ONLY on clean exit (exit code 0)
#   - Error logs written BEFORE exit on failure
#
# Usage:
#   ./monitoring/scripts/example-heartbeat.sh
#
# Log output: monitoring/logs/{service}.log (mounted into Filebeat as /logs/)

set -euo pipefail

SERVICE="example-heartbeat"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGFILE="${SCRIPT_DIR}/../logs/${SERVICE}.log"

# Ensure log directory exists
mkdir -p "$(dirname "$LOGFILE")"

# --- JSON Logger ---
# Usage: log <level> <event> <message>
log() {
  local level="${1:-info}"
  local event="${2:-unknown}"
  local message="${3:-}"
  printf '{"@timestamp":"%s","service":"%s","level":"%s","event":"%s","message":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$SERVICE" "$level" "$event" "$message" >> "$LOGFILE"
}

# --- Heartbeat: ONLY on clean exit (exit code 0) ---
# If the script fails, this trap is NOT triggered (status != 0).
# Error logs must be written explicitly before exit.
trap '[ $? -eq 0 ] && log "info" "heartbeat" "run completed ok"' EXIT

# =============================================================================
# REAL WORK BEGINS HERE
# =============================================================================

log "info" "start" "example heartbeat script started"

# Simulate some work
sleep 2

# Simulate a successful operation
log "info" "task_complete" "example task completed successfully"

# If something fails, log an error and exit non-zero:
#   some_command || { log "error" "task_failed" "description of what failed"; exit 1; }

log "info" "end" "example heartbeat script finished"

# =============================================================================
# REAL WORK ENDS HERE — trap EXIT writes the heartbeat
# =============================================================================
