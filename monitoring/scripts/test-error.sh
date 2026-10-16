#!/usr/bin/env bash
# =============================================================================
# test-error.sh — error generator for alert testing
# =============================================================================
#
# Intentionally writes an error log line to exercise the alert chain:
#   log -> Filebeat -> Elasticsearch -> monitor-check.py -> GitHub Issue
#
# The default event `test_error` is on monitor-check's IGNORED_ERROR_PATTERNS,
# so the default run reaches ES and stops there (deliberately, or every test
# would file an issue). That also means the default can never verify the half
# that matters most: creating the issue. Override EVENT to test that half for
# real, and close the resulting issue by hand, because resolve_stale() waits
# RESOLVE_AFTER_HOURS (24 h) of quiet, not five minutes.
#
# Usage:
#   ./monitoring/scripts/test-error.sh                           # ES only, no issue
#   EVENT=alert_chain_probe ./monitoring/scripts/test-error.sh   # full chain
#   SERVICE=my-service ./monitoring/scripts/test-error.sh        # custom service name
#
# Log output: $IAC_TEST_LOG_DIR/{service}.log, default monitoring/logs/
#
# IAC_TEST_LOG_DIR exists because the chain starts wherever Filebeat is reading.
# When the Elastic stack runs on another host (`es_log_host` in
# ansible/inventory/group_vars/all/main.yml), writing here while Filebeat
# watches somewhere else makes the probe pass with no document, no issue and no
# error: a test that quietly stopped testing. For the real chain, write on the
# log host, for example
#   ssh <log-host> 'SERVICE=alert-probe EVENT=alert_chain_probe \
#     IAC_TEST_LOG_DIR=~/monitoring/logs bash -s' < monitoring/scripts/test-error.sh

set -euo pipefail

SERVICE="${SERVICE:-test-error}"
EVENT="${EVENT:-test_error}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGFILE="${IAC_TEST_LOG_DIR:-${SCRIPT_DIR}/../logs}/${SERVICE}.log"
TIMESTAMP=$(date -u +%FT%TZ)

mkdir -p "$(dirname "$LOGFILE")"

echo "Writing test error to ${LOGFILE}..."
echo "Service: ${SERVICE}"

cat >> "$LOGFILE" << JSONEOF
{"@timestamp":"${TIMESTAMP}","service":"${SERVICE}","level":"error","event":"${EVENT}","message":"This is a deliberate test error. If you see a GitHub Issue, the alert chain works."}
JSONEOF

echo ""
echo "Test error written."
echo ""
echo "Next steps:"
echo "  1. Wait ~30s for Filebeat to ship it, then run: just monitor-check"
echo "  2. Check Kibana Discover (http://localhost:5601, index: logs-*)"
if [ "${EVENT}" = "test_error" ]; then
  echo "  3. No GitHub Issue: event '${EVENT}' is on IGNORED_ERROR_PATTERNS,"
  echo "     so this run stops at ES. For the full chain incl. the issue:"
  echo "     EVENT=alert_chain_probe $0"
else
  echo "  3. Verify a GitHub Issue was created ('${EVENT}' is not ignored)."
  echo "     Close it yourself: resolve_stale() only closes after 24h without"
  echo "     a recurrence (RESOLVE_AFTER_HOURS), so a probe issue lingers a day."
fi
echo ""
echo "To trigger the dead man's switch (heartbeat rule):"
echo "  simply do NOT run example-heartbeat.sh for longer than the expected interval."
