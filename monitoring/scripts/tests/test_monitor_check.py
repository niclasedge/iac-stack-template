"""Tests for monitor-check.py: the alert chain's own failure modes.

These exist because of a failure shape that is easy to build and hard to see: a
run that gets HTTP 401 from GitHub files nothing while printing "All clear".
Nothing is wrong with the detection; the chain lies about the delivery.

Every test below pins one link of that chain. No network: the collaborators are
monkeypatched, so what is under test is the bookkeeping, not GitHub.

Run: uv run --with pytest python3 -m pytest monitoring/scripts/tests/ -q
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SCRIPTS_DIR / "monitor-check.py"


def _load_module():
    # The filename has a hyphen, so a plain import is not available.
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("monitor_check", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mc = _load_module()


# Shaped exactly like what find_recent_errors() / find_missing_heartbeats()
# append — keys included. A short-cut dict here would pass the assertions while
# check_once raised KeyError against the real thing.
ERROR_ALERT = {
    "alert_id": "error-someservice",
    "service": "someservice",
    "count": 1,
    "message": "boom",
    "timestamp": "2026-07-27T09:24:00Z",
    "event": "job_failed",
    "type": "error",
}

HEARTBEAT_ALERT = {
    "alert_id": "heartbeat-worker",
    "service": "worker",
    "max_age_minutes": 65,
    "type": "missing_heartbeat",
}


@pytest.fixture
def chain(monkeypatch):
    """check_once with every collaborator stubbed out.

    Returns a dict the test can adjust: `errors`/`heartbeats` are what detection
    finds, `created` is what create_issue returns (None = the failure this whole
    file is about).
    """
    cfg = {"errors": [], "heartbeats": [], "created": "https://gh/x/y/issues/1"}
    calls = {"create": 0}

    def fake_create(*_a, **_kw):
        calls["create"] += 1
        return cfg["created"]

    monkeypatch.setattr(mc, "find_recent_errors", lambda *a, **k: cfg["errors"])
    monkeypatch.setattr(mc, "find_missing_heartbeats", lambda *a, **k: cfg["heartbeats"])
    monkeypatch.setattr(mc, "create_issue", fake_create)
    monkeypatch.setattr(mc, "resolve_stale", lambda *a, **k: 0)
    monkeypatch.setattr(mc, "state_save", lambda *a, **k: None)
    cfg["calls"] = calls
    return cfg


def _run(dry_run=False):
    state = {"issue_map": {}}
    return mc.check_once("tok", "owner", "repo", state, dry_run)


class TestDroppedAlerts:
    """A found-but-unfiled alert must never read as success."""

    def test_failed_create_is_counted_as_dropped(self, chain):
        chain["errors"] = [ERROR_ALERT]
        chain["created"] = None
        new, dropped = _run()
        assert (new, dropped) == (0, 1)

    def test_failed_create_does_not_say_all_clear(self, chain, capsys):
        chain["errors"] = [ERROR_ALERT]
        chain["created"] = None
        _run()
        out = capsys.readouterr().out
        assert "All clear" not in out, "the exact lie that hid a 3-day outage"
        assert "NOT filed" in out

    def test_successful_create_is_not_dropped(self, chain):
        chain["errors"] = [ERROR_ALERT]
        new, dropped = _run()
        assert (new, dropped) == (1, 0)

    def test_nothing_found_is_genuinely_all_clear(self, chain, capsys):
        new, dropped = _run()
        assert (new, dropped) == (0, 0)
        assert "All clear" in capsys.readouterr().out

    def test_a_dropped_heartbeat_alert_counts_too(self, chain):
        # The heartbeat branch needs the same accounting: a missing
        # dead-man's-switch alert is the one you least want silently swallowed.
        chain["heartbeats"] = [HEARTBEAT_ALERT]
        chain["created"] = None
        new, dropped = _run()
        assert (new, dropped) == (0, 1)

    def test_partial_failure_reports_both(self, chain, monkeypatch):
        # Two services, create fails for the second only.
        chain["errors"] = [ERROR_ALERT, {**ERROR_ALERT, "alert_id": "error-other",
                                         "service": "other"}]
        seq = iter(["https://gh/x/y/issues/1", None])
        monkeypatch.setattr(mc, "create_issue", lambda *a, **k: next(seq))
        new, dropped = _run()
        assert (new, dropped) == (1, 1)

    def test_dry_run_never_reports_drops(self, chain):
        # --dry-run does not call create_issue at all, so it must not invent
        # failures; otherwise the crontab's exit code would flap on a no-op.
        chain["errors"] = [ERROR_ALERT]
        chain["created"] = None
        new, dropped = _run(dry_run=True)
        assert dropped == 0 and new == 1
        assert chain["calls"]["create"] == 0


class TestGhBinary:
    """cron's PATH is /usr/bin:/bin; a package-manager gh is not in it."""

    def test_cron_path_still_finds_gh(self, monkeypatch):
        """The regression itself, reproduced against the real filesystem.

        No mocking of shutil here on purpose: the bug was that the *lookup*
        used the ambient PATH, and a test that stubs the lookup cannot see it.
        """
        found = mc.gh_binary()
        if found is None:
            pytest.skip("gh not installed on this host")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")  # what cron actually gives us
        if shutil.which("gh"):
            pytest.skip("gh lives on the cron PATH here — nothing to prove")
        # Not asserting *which* gh: an interactive shell may resolve a plugin
        # shim first. The contract is that some usable one is still found where
        # a bare "gh" raised FileNotFoundError.
        under_cron = mc.gh_binary()
        assert under_cron is not None, "bare `gh` raised FileNotFoundError under cron"
        assert os.access(under_cron, os.X_OK)

    def test_returns_none_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(mc.shutil, "which", lambda *a, **k: None)
        assert mc.gh_binary() is None

    def test_result_is_absolute_or_none(self):
        # The contract subprocess.run depends on: an absolute path or None,
        # never the bare "gh" that only resolves in an interactive shell.
        found = mc.gh_binary()
        assert found is None or Path(found).is_absolute()


class TestResolveGithubAuth:
    """A set-but-rejected token used to shadow a working gh login."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_OWNER", "GH_OWNER",
                    "GITHUB_REPO"):
            monkeypatch.delenv(var, raising=False)

    def test_valid_env_token_is_used_as_is(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "good")
        monkeypatch.setattr(mc, "token_works", lambda t: True)
        token, _, _ = mc.resolve_github_auth()
        assert token == "good"

    def test_rejected_env_token_falls_back_to_gh(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "expired")
        monkeypatch.setattr(mc, "token_works", lambda t: False)
        monkeypatch.setattr(mc, "gh_binary", lambda: "/usr/local/bin/gh")
        monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: type(
            "R", (), {"returncode": 0, "stdout": "gho_fromcli\n"})())
        token, _, _ = mc.resolve_github_auth()
        assert token == "gho_fromcli", "the whole point: do not keep a dead token"

    def test_no_token_and_no_gh_yields_empty(self, monkeypatch):
        # Clear both token names: a developer machine that can decrypt the
        # secrets may have deposited a real token into os.environ, and this test
        # would then call the real token_works() with a live credential.
        for name in ("GITHUB_TOKEN", "GH_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(mc, "gh_binary", lambda: None)
        token, _, _ = mc.resolve_github_auth()
        assert token == ""

    def test_a_valid_token_costs_no_gh_call(self, monkeypatch):
        # token_works is one GET; the fallback must not also fire.
        monkeypatch.setenv("GITHUB_TOKEN", "good")
        monkeypatch.setattr(mc, "token_works", lambda t: True)
        monkeypatch.setattr(mc, "gh_binary", lambda: pytest.fail(
            "gh must not be consulted when the env token works"))
        assert mc.resolve_github_auth()[0] == "good"
