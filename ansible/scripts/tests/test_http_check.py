"""The example job's payload script classifies URLs and never crashes on a finding.

`ansible/scripts/jobs/http_check.py` prints one JSON object. A down URL is a
FINDING (counted, listed, exit 0); only a broken script may exit non-zero,
because the playbook turns a non-zero rc into a failed job while a finding must
travel through the level:error result log instead.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ansible" / "scripts" / "jobs" / "http_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("http_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(urls: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={"HTTP_CHECK_URLS": urls, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_urls_is_a_valid_empty_result() -> None:
    proc = run("")
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"total": 0, "up": 0, "down": 0, "down_urls": []}


def test_an_unreachable_url_is_a_finding_not_a_crash() -> None:
    # Port 9 on loopback (discard) refuses the connection; nothing leaves the host.
    proc = run("http://127.0.0.1:9/")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["total"] == 1 and data["down"] == 1 and data["up"] == 0
    assert data["down_urls"][0]["url"] == "http://127.0.0.1:9/"
    assert data["down_urls"][0]["reason"]


def test_probe_returns_none_for_a_healthy_response(monkeypatch) -> None:
    module = _load()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **kw: Response())
    assert module.probe("https://example.com/") is None


def test_probe_reports_http_errors(monkeypatch) -> None:
    module = _load()

    class Response:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **kw: Response())
    assert module.probe("https://example.com/") == "HTTP 503"
