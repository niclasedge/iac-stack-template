"""scripts/age-key.sh: the one answer to "where is this machine's age key".

Three call sites hand this script to SOPS as SOPS_AGE_KEY_CMD (./j, the justfile,
scripts/iac_secrets.py). It is therefore the single point of failure for every
secret in this repo, and the ways it can fail are quiet ones:

  * a wrong PATH breaks only the scheduled runs. cron gives PATH=/usr/bin:/bin,
    so nothing but coreutils may be needed. Only `env -i` shows it, never an
    interactive shell.
  * an empty SOPS_AGE_KEY_CMD does not degrade, it panics sops outright (slice
    bounds out of range in age.getOutputFromCmd). An empty SOPS_AGE_KEY_FILE is
    merely skipped, so the asymmetry is easy to forget and a caller that
    defaults to "" when it finds nothing takes the whole process down. The
    structural test below pins the rule.
  * a missing key must fail loudly. Printing nothing and exiting 0 would hand
    SOPS an empty identity and turn "locked out" into "no recipients matched".

No real key and no sops are involved: HOME is redirected to a tmp_path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "age-key.sh"

# What cron actually hands a job. Reproducing it is the only way the PATH bug is
# visible: in an interactive shell the extra directories are already there.
CRON_PATH = "/usr/bin:/bin"


def run(home: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": CRON_PATH, "HOME": str(home)}
    env.update(extra_env or {})
    return subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)


def write_key(home: Path, body: str = "AGE-SECRET-KEY-TESTVALUE\n") -> Path:
    key_dir = home / ".config" / "sops" / "age"
    key_dir.mkdir(parents=True, exist_ok=True)
    key = key_dir / "keys.txt"
    key.write_text(body)
    return key


def test_script_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} missing: every secrets path points here"
    assert os.access(SCRIPT, os.X_OK), "SOPS runs this as a command, not as an argument"


def test_default_key_is_printed_under_crons_path(tmp_path):
    write_key(tmp_path)
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "AGE-SECRET-KEY-TESTVALUE\n"


def test_explicit_key_file_wins_over_the_default(tmp_path):
    write_key(tmp_path, "AGE-SECRET-KEY-DEFAULT\n")
    explicit = tmp_path / "other.txt"
    explicit.write_text("AGE-SECRET-KEY-EXPLICIT\n")
    r = run(tmp_path, {"SOPS_AGE_KEY_FILE": str(explicit)})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "AGE-SECRET-KEY-EXPLICIT\n"


def test_unreadable_explicit_file_falls_back_to_the_default(tmp_path):
    write_key(tmp_path)
    r = run(tmp_path, {"SOPS_AGE_KEY_FILE": str(tmp_path / "missing.txt")})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "AGE-SECRET-KEY-TESTVALUE\n"


def test_no_key_fails_loudly_and_prints_nothing(tmp_path):
    """Exit 0 with empty stdout would be the silent failure, not a missing key."""
    r = run(tmp_path)
    assert r.returncode != 0
    assert r.stdout == ""
    assert "no age identity" in r.stderr
    assert "docs/secrets.md" in r.stderr, "the error must point at the setup"


def test_every_call_site_defaults_to_the_script_unconditionally():
    """No caller may hand SOPS an empty SOPS_AGE_KEY_CMD: that is a sops panic.

    Structural, not behavioural: the three call sites are bash, just and python,
    so there is no shared runtime to assert against. What they must share is the
    fallback, and it must not be guarded by an existence check: the script
    itself decides what exists.
    """
    for rel, needle in (
        ("j", 'SOPS_AGE_KEY_CMD="${SOPS_AGE_KEY_CMD:-$PWD/scripts/age-key.sh}"'),
        ("justfile", "env('SOPS_AGE_KEY_CMD', justfile_directory() / \"scripts/age-key.sh\")"),
        ("scripts/iac_secrets.py", 'env.setdefault("SOPS_AGE_KEY_CMD", str(AGE_KEY_CMD))'),
    ):
        text = (REPO / rel).read_text()
        assert needle in text, f"{rel} no longer defaults SOPS_AGE_KEY_CMD to age-key.sh"
