"""A dry run must not reach the alert chain.

`just deploy-check <service>` is documented as a dry run that changes nothing,
so it is what people reach for to explore a service. If `es_logger` emitted
`level:error` for the would-be failure of such a probe, monitor-check, which
keys on exactly that, would open a real GitHub issue. Nothing would distinguish
the rehearsal from the performance.

The fix is a downgrade, not a suppression: `level:error` becomes `warn` under
`--check`, and `check_mode: true` is stamped on the document. What the dry run
found stays in Elasticsearch and Kibana, it just is not a job that could not
work.

Two properties, and the second is the one with teeth:

  1. under --check, an error becomes warn and carries check_mode
  2. WITHOUT --check nothing changes: no field, no downgrade

Property 2 exists because the failure mode of this change is worse than the bug
it fixes: a flag stuck on would silence every real alert in the system, and it
would look like calm.

`CallbackBase` and `ansible.context` are stubbed in `sys.modules` before the
plugin is imported, so the test does not depend on the installed ansible version.

Run:
    uvx --with pytest pytest ansible/scripts/tests/test_es_logger_check_mode.py -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "ansible" / "callback_plugins" / "es_logger.py"


@contextlib.contextmanager
def stubbed_ansible(check: bool):
    """es_logger importable and CONSTRUCTIBLE with ansible stubbed.

    The stubs have to outlive the import: `self.check_mode` is read inside
    `__init__`, so restoring sys.modules before constructing the callback lets the
    real ansible answer instead — with an empty CLIARGS, i.e. check=False. That is
    a test that passes for the wrong reason, and it did.
    """
    callback_mod = types.ModuleType("ansible.plugins.callback")

    class CallbackBase:  # minimal stand-in; the plugin only calls super().__init__
        def __init__(self, *a, **kw):
            pass

    callback_mod.CallbackBase = CallbackBase

    context_mod = types.ModuleType("ansible.context")
    context_mod.CLIARGS = {"check": check}

    ansible_pkg = types.ModuleType("ansible")
    plugins_pkg = types.ModuleType("ansible.plugins")
    # Attributes, not just sys.modules entries: `from ansible import context`
    # resolves by getattr on the package object, and a stub without them makes the
    # plugin's own try/except swallow an ImportError and report check_mode=False —
    # which is exactly the failure this test is supposed to catch.
    ansible_pkg.plugins = plugins_pkg
    ansible_pkg.context = context_mod
    plugins_pkg.callback = callback_mod

    stubs = {
        "ansible": ansible_pkg,
        "ansible.plugins": plugins_pkg,
        "ansible.plugins.callback": callback_mod,
        "ansible.context": context_mod,
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    # A fresh module object per call: self.check_mode is read once in __init__.
    sys.modules.pop("es_logger_under_test", None)
    try:
        spec = importlib.util.spec_from_file_location("es_logger_under_test", PLUGIN)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["es_logger_under_test"] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop("es_logger_under_test", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def emit(check: bool, tmp_path: Path, level: str) -> dict:
    """Run one document through the real _emit and read it back off disk."""
    logdir = tmp_path / "logs"
    logdir.mkdir()
    with stubbed_ansible(check) as mod:
        # IAC_LOG_DIR is read in __init__, so it must be set before construction.
        with mock.patch.dict(os.environ, {"IAC_LOG_DIR": str(logdir),
                                          "IAC_JOB_ID": "probe"}, clear=False):
            os.environ.pop("IAC_ES_URL", None)
            cb = mod.CallbackModule()
            cb._emit({"level": level, "event": "job_run", "message": "probe"})
    written = (logdir / "ansible.log").read_text().strip().splitlines()
    assert len(written) == 1, f"expected one document, got {len(written)}"
    return json.loads(written[0])


def test_check_mode_downgrades_error_and_marks_the_document(tmp_path: Path) -> None:
    doc = emit(check=True, tmp_path=tmp_path, level="error")
    assert doc["level"] == "warn", "an error under --check must not reach monitor-check"
    assert doc["check_mode"] is True


def test_check_mode_leaves_info_alone(tmp_path: Path) -> None:
    doc = emit(check=True, tmp_path=tmp_path, level="info")
    assert doc["level"] == "info"
    assert doc["check_mode"] is True


@pytest.mark.parametrize("level", ["error", "warn", "info"])
def test_a_real_run_is_untouched(level: str, tmp_path: Path) -> None:
    """The dangerous direction: a flag stuck on would mute the whole system."""
    doc = emit(check=False, tmp_path=tmp_path, level=level)
    assert doc["level"] == level
    assert "check_mode" not in doc


def test_a_broken_cliargs_lookup_defaults_to_a_real_run(tmp_path: Path) -> None:
    """If ansible ever moves `check`, fail towards alerting, not towards silence.

    Simulated the way it would actually happen: the import inside __init__ raises,
    the plugin's except branch runs. Asserting on a hand-set attribute would test
    the assignment rather than the fallback.
    """
    logdir = tmp_path / "logs"
    logdir.mkdir()
    with stubbed_ansible(check=True) as mod:
        # Both, or nothing breaks: `from ansible import context` tries getattr on
        # the package and then falls back to sys.modules['ansible.context'], so
        # deleting only the attribute still resolves and check_mode stays True.
        del sys.modules["ansible"].context
        sys.modules.pop("ansible.context", None)
        with mock.patch.dict(os.environ, {"IAC_LOG_DIR": str(logdir),
                                          "IAC_JOB_ID": "probe"}, clear=False):
            cb = mod.CallbackModule()
            assert cb.check_mode is False
            cb._emit({"level": "error", "event": "job_run", "message": "probe"})
    doc = json.loads((logdir / "ansible.log").read_text().strip())
    assert doc["level"] == "error"
    assert "check_mode" not in doc
