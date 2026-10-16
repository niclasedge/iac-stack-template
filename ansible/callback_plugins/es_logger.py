# =============================================================================
# es_logger — Ansible callback plugin: traces-as-logs for Elasticsearch
# =============================================================================
# Emits ONE JSON line per Ansible task (a "span") and ONE summary line per
# playbook run (a "job_run"), correlated by a shared trace_id. Filebeat ships
# the file (IAC_LOG_DIR/ansible.log) into Elasticsearch like any other
# structured log — no external tracing backend (Jaeger/Tempo) required.
#
# Correlation:
#   trace_id  ← env IAC_RUN_ID  (set by `just job`; else auto-generated)
#   service   ← env IAC_JOB_ID  (friendly job name; else playbook basename)
#
# Field note: uses `target_host` (string) for the managed host, NOT `host`,
# because Filebeat's add_host_metadata already maps `host` as an object in ES.
#
# IAC_ES_URL — for controllers whose log file nothing ships
# --------------------------------------------------------
# The file drop above only becomes a trace if some Filebeat reads it. That holds
# on the log host and nowhere else: a scheduler running playbooks inside a
# container has an IAC_LOG_DIR that no Filebeat watches, so every span and every
# job_run from such a run would be written and thrown away, while the scheduler
# UI still says success.
#
# When IAC_ES_URL is set, the documents are buffered and sent to ES in ONE
# `_bulk` request at the end of the run. The file write is unchanged and still
# happens, so nothing about a local run changes.
#
# Set IAC_ES_URL only where the log file is NOT already shipped. Setting it on
# the log host would index every document twice, once from here and once via
# Filebeat. The deterministic `_id` below deduplicates retries of *this* path
# only; Filebeat generates its own ids and would duplicate regardless.
#
# Enable via ansible.cfg:
#   [defaults]
#   callback_plugins = ./callback_plugins
#   callbacks_enabled = es_logger
# =============================================================================

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: es_logger
    type: notification
    short_description: Write per-task spans + run summary as JSON for Elasticsearch
    description:
        - Emits one JSON "span" per task and one "job_run" summary per playbook,
          correlated by a shared trace_id, to IAC_LOG_DIR/ansible.log.
    requirements:
        - Enable in configuration (callbacks_enabled = es_logger)
"""


def _now() -> str:
    """UTC timestamp with millisecond precision, matching filebeat layouts."""
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "es_logger"
    CALLBACK_NEEDS_ENABLED = True

    # Matches filebeat.yml's `index: "logs-%{+yyyy.MM.dd}"`. Directly written
    # documents must land in the same daily index, or monitor-check, which
    # queries `logs-*`, would not see them.
    ES_INDEX_PREFIX = "logs-"
    # One request at the end of the run, so this is the only network wait the
    # whole playbook pays. Short enough that an unreachable ES costs seconds.
    ES_TIMEOUT = 8
    # A guard against a pathological run, not a tuning knob.
    ES_MAX_BUFFER = 4000

    def __init__(self):
        super().__init__()
        self.trace_id = os.environ.get("IAC_RUN_ID") or uuid.uuid4().hex[:16]
        self.service = os.environ.get("IAC_JOB_ID") or None
        logdir = os.environ.get("IAC_LOG_DIR") or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "monitoring", "logs")
        )
        self.logfile = os.path.join(logdir, "ansible.log")
        self.playbook_name = None
        self.play_start = time.monotonic()
        self.task_start: dict[str, float] = {}
        self.counts = {"ok": 0, "changed": 0, "failed": 0,
                       "unreachable": 0, "skipped": 0}
        self.es_url = (os.environ.get("IAC_ES_URL") or "").strip().rstrip("/")
        # `--check` is not on the play object, it is a CLI-wide setting, so it has
        # to come from ansible's parsed args. Wrapped because a callback that
        # cannot be constructed disables tracing for the whole run: an import or
        # attribute change in ansible-core must cost the flag, never the spans.
        try:
            from ansible import context as _ansible_context
            self.check_mode = bool(_ansible_context.CLIARGS.get("check"))
        except Exception:
            self.check_mode = False
        self._buffer: list[dict] = []
        self._dropped = 0
        try:
            os.makedirs(logdir, exist_ok=True)
        except OSError:
            pass

    # ── I/O ──────────────────────────────────────────────────────────────────
    def _emit(self, doc: dict) -> None:
        doc.setdefault("@timestamp", _now())
        doc.setdefault("service", self.service or "ansible")
        doc.setdefault("trace_id", self.trace_id)
        # A dry run must not page anyone. `just deploy-check <service>` is
        # documented as "changes nothing", so it is what people reach for to
        # explore, and monitor-check keys on `level:error` and cannot tell a
        # rehearsal from the performance.
        #
        # Downgraded rather than suppressed: what a dry run found is worth
        # having in ES and Kibana, it is just not a job that could not work.
        # `check_mode` rides along so the two are told apart in a query instead
        # of by guessing at a `warn`.
        if self.check_mode:
            doc["check_mode"] = True
            if doc.get("level") == "error":
                doc["level"] = "warn"
        try:
            with open(self.logfile, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if self.es_url:
            if len(self._buffer) < self.ES_MAX_BUFFER:
                self._buffer.append(doc)
            else:
                self._dropped += 1

    def _flush_to_es(self) -> None:
        """Send the buffered documents to ES in one `_bulk` request.

        Never raises and never fails the run: this plugin observes playbooks, so
        a monitoring outage must not become a deploy outage. Problems are
        reported as warnings, which land in the run output, the one place
        someone is looking when a scheduled run misbehaves. Staying silent here
        would recreate the very blind spot this path exists to close.
        """
        if not self.es_url or not self._buffer:
            return
        lines = []
        for doc in self._buffer:
            # Index by the document's OWN date, not by flush time, so a run
            # crossing midnight files its spans the way Filebeat would.
            day = str(doc.get("@timestamp", ""))[:10].replace("-", ".")
            index = self.ES_INDEX_PREFIX + (day or _now()[:10].replace("-", "."))
            # Deterministic id, so re-sending THIS request (the buffer holds the
            # same documents with the same span_ids) overwrites instead of
            # duplicating. It does not deduplicate two separate runs: a fresh run
            # generates fresh span_ids, and its trace_id is unique anyway
            # (IAC_RUN_ID comes from `just job` per run, and where it is unset
            # the plugin generates one).
            doc_id = f"{doc.get('trace_id')}-{doc.get('span_id') or doc.get('event')}"
            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps(doc, ensure_ascii=False))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        req = urllib.request.Request(
            self.es_url + "/_bulk", data=body, method="POST",
            headers={"Content-Type": "application/x-ndjson"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.ES_TIMEOUT) as resp:
                payload = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._display.warning(
                f"es_logger: {len(self._buffer)} trace documents did not reach "
                f"{self.es_url} ({exc}). They are in {self.logfile}."
            )
            return
        if payload.get("errors"):
            # A rejected document is invisible otherwise — this is how a field
            # mapped as an object (see the `host` vs `target_host` rule) shows up.
            reasons = [
                item.get("index", {}).get("error", {}).get("reason", "")
                for item in payload.get("items", [])
                if item.get("index", {}).get("error")
            ]
            self._display.warning(
                f"es_logger: ES rejected {len(reasons)}/{len(self._buffer)} trace "
                f"documents. First reason: {reasons[0][:200] if reasons else 'unknown'}"
            )
        if self._dropped:
            self._display.warning(
                f"es_logger: buffer limit {self.ES_MAX_BUFFER} reached, "
                f"{self._dropped} documents were not sent (the log file has all)."
            )
        self._buffer.clear()

    def _span(self, result, status: str, level: str) -> None:
        task = result._task
        start = self.task_start.get(task._uuid, self.play_start)
        duration = round(time.monotonic() - start, 3)
        host = result._host.get_name()
        self.counts[status] = self.counts.get(status, 0) + 1

        doc = {
            "level": level,
            "event": "task",
            "span_id": uuid.uuid4().hex[:12],
            "task": task.get_name(),
            "target_host": host,
            "status": status,
            "duration_seconds": duration,
            "playbook": self.playbook_name,
            "message": f"{task.get_name()} → {status} on {host}",
        }
        if status in ("failed", "unreachable"):
            res = result._result or {}
            ctx = res.get("msg") or res.get("stderr") or res.get("exception") or ""
            doc["error_context"] = str(ctx)[:500]
            doc["message"] = f"{task.get_name()} FAILED on {host}: {str(ctx)[:120]}"
        self._emit(doc)

    # ── Ansible hooks ──────────────────────────────────────────────────────────
    def v2_playbook_on_start(self, playbook):
        self.playbook_name = os.path.basename(playbook._file_name)
        if not self.service:
            self.service = self.playbook_name.rsplit(".", 1)[0]
        self.play_start = time.monotonic()
        self._emit({"level": "info", "event": "playbook_start",
                    "playbook": self.playbook_name,
                    "message": f"playbook {self.playbook_name} started"})

    def v2_playbook_on_task_start(self, task, is_conditional):
        self.task_start[task._uuid] = time.monotonic()

    def v2_playbook_on_handler_task_start(self, task):
        self.task_start[task._uuid] = time.monotonic()

    def v2_runner_on_ok(self, result):
        changed = bool(result._result.get("changed", False))
        self._span(result, "changed" if changed else "ok", "info")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._span(result, "failed", "warn" if ignore_errors else "error")

    def v2_runner_on_unreachable(self, result):
        self._span(result, "unreachable", "error")

    def v2_runner_on_skipped(self, result):
        self._span(result, "skipped", "info")

    def v2_playbook_on_stats(self, stats):
        duration = round(time.monotonic() - self.play_start, 3)
        # Mirror ansible's exit-code semantics: unreachables tolerated via
        # play-level ignore_unreachable land in neither failures nor dark,
        # so an offline host does not mark the whole run as failed.
        failed = sum(stats.failures.values()) + sum(stats.dark.values())
        status = "failed" if failed else "success"
        level = "error" if failed else "info"
        ok = self.counts.get("ok", 0) + self.counts.get("changed", 0)
        hosts = ",".join(sorted(stats.processed.keys()))
        self._emit({
            "level": level,
            "event": "job_run",
            "status": status,
            "duration_seconds": duration,
            "tasks_ok": ok,
            "tasks_changed": self.counts.get("changed", 0),
            "tasks_failed": failed,
            "tasks_skipped": self.counts.get("skipped", 0),
            "target_host": hosts,
            "playbook": self.playbook_name,
            # "(check)" in the text as well as the field: the message is what a
            # human reads in Kibana's log table, where the `check_mode` field is
            # not shown.
            "message": (f"{self.service} {status}"
                        f"{' (check)' if self.check_mode else ''}"
                        f": {ok} ok, {failed} failed in {duration}s"),
        })
        # After the summary, so the job_run document every consumer keys on is
        # part of the same request as its spans — never shipped without them.
        self._flush_to_es()
