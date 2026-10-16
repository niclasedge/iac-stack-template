"""scripts/iac_secrets.py — the one path from encrypted file to os.environ.

One module reads the encrypted file for every script that needs it, so a
mistake here is a mistake in monitor-check and in every payload job at once. The
three behaviours that the alert chain depends on are pinned:

  * setdefault, never overwrite: ./j and any scheduler's own environment arrive
    as already-exported variables and must win over the file;
  * a legitimate no-op (no file, no sops) stays quiet AND must not set the
    IAC_SECRETS marker, which means "delivered", not "tried";
  * a failing decrypt raises. Falling through to defaults is the exact silent
    failure the chain exists to catch, and it looks like success from outside.

sops itself is never invoked: the subprocess call is faked, so these run on a
machine without sops, without an age key, and without the repo's real secrets.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import iac_secrets  # noqa: E402


@pytest.fixture
def fake_sops(monkeypatch):
    """Pretend sops is installed and make it return `text` (or fail)."""

    def install(text: str = "", returncode: int = 0, stderr: str = ""):
        monkeypatch.setattr(iac_secrets, "sops_binary", lambda: "/fake/sops")
        calls: list[list[str]] = []

        def _run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, text, stderr)

        monkeypatch.setattr(iac_secrets.subprocess, "run", _run)
        return calls

    return install


@pytest.fixture
def secrets_file(tmp_path):
    p = tmp_path / "app.env"
    p.write_text("# encrypted, contents irrelevant: sops is faked\n")
    return p


class TestLoadEnv:
    def test_populates_os_environ(self, fake_sops, secrets_file, monkeypatch):
        fake_sops("FOO_TEST_VAR=bar\n# comment\n\nBAZ_TEST_VAR=qux\n")
        monkeypatch.delenv("IAC_SECRETS", raising=False)
        monkeypatch.delenv("FOO_TEST_VAR", raising=False)
        monkeypatch.delenv("BAZ_TEST_VAR", raising=False)

        added = iac_secrets.load_env(secrets_file)

        import os
        assert os.environ["FOO_TEST_VAR"] == "bar"
        assert os.environ["BAZ_TEST_VAR"] == "qux"
        assert added == 2
        assert os.environ["IAC_SECRETS"] == "1"

    def test_does_not_overwrite_an_exported_value(self, fake_sops, secrets_file, monkeypatch):
        fake_sops("FOO_TEST_VAR=from_file\n")
        monkeypatch.delenv("IAC_SECRETS", raising=False)
        monkeypatch.setenv("FOO_TEST_VAR", "already_set")

        iac_secrets.load_env(secrets_file)

        import os
        assert os.environ["FOO_TEST_VAR"] == "already_set"

    def test_sops_metadata_never_reaches_the_environment(self, fake_sops, secrets_file, monkeypatch):
        fake_sops("REAL_TEST_VAR=x\nsops_version=3.13.3\nsops_mac=ENC[...]\n")
        monkeypatch.delenv("IAC_SECRETS", raising=False)
        monkeypatch.delenv("sops_version", raising=False)

        iac_secrets.load_env(secrets_file)

        import os
        assert "sops_version" not in os.environ
        assert os.environ["REAL_TEST_VAR"] == "x"

    def test_already_loaded_short_circuits(self, fake_sops, secrets_file, monkeypatch):
        calls = fake_sops("SHOULD_NOT_APPEAR=1\n")
        monkeypatch.setenv("IAC_SECRETS", "1")

        assert iac_secrets.load_env(secrets_file) == 0
        assert calls == [], "running under ./j must not decrypt a second time"


class TestQuietNoOps:
    """No file and no sops are both normal — but neither may claim delivery."""

    def test_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("IAC_SECRETS", raising=False)
        assert iac_secrets.load_env(tmp_path / "nope.env") == 0
        import os
        assert "IAC_SECRETS" not in os.environ

    def test_sops_not_installed(self, monkeypatch, secrets_file):
        # A host or container without sops: the repo clone carries the encrypted
        # file, the environment comes from the scheduler's own store.
        monkeypatch.delenv("IAC_SECRETS", raising=False)
        monkeypatch.setattr(iac_secrets, "sops_binary", lambda: None)
        assert iac_secrets.load_env(secrets_file) == 0
        import os
        assert "IAC_SECRETS" not in os.environ


class TestFailingDecryptIsLoud:
    def test_raises_instead_of_falling_back(self, fake_sops, secrets_file, monkeypatch):
        fake_sops("", returncode=1, stderr="no identity matched any of the recipients")
        monkeypatch.delenv("IAC_SECRETS", raising=False)

        with pytest.raises(RuntimeError, match="no identity matched"):
            iac_secrets.load_env(secrets_file)


class TestSopsBinaryResolution:
    def test_searches_homebrew_even_with_crons_path(self, monkeypatch):
        """cron gives PATH=/usr/bin:/bin; Homebrew's sops is not on it.

        Same trap as gh_binary() in monitor-check.py, which makes the alert
        chain a silent no-op. The assertion is on the search path handed to
        which(), because whether sops is actually installed here is not the
        point.
        """
        seen: dict[str, str] = {}
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(
            iac_secrets.shutil, "which",
            lambda name, path=None: seen.update(name=name, path=path) or None,
        )

        iac_secrets.sops_binary()

        assert seen["name"] == "sops"
        assert "/opt/homebrew/bin" in seen["path"]
        assert "/usr/local/bin" in seen["path"]


class TestParseDotenv:
    def test_ignores_comments_and_blanks_and_keeps_inner_equals(self):
        parsed = iac_secrets.parse_dotenv(
            "# note\n\nA=1\nB=x=y\n  C = 3 \nnot a pair\n"
        )
        assert parsed == {"A": "1", "B": "x=y", "C": "3"}
