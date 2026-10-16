"""Decrypt secrets/app.env (SOPS + age) into os.environ — in memory only.

The one place that knows how this repo gets at its secrets: which sops binary,
which age key, and what a failure means. Those are exactly the parts that must
not be copy-pasted into every script.

Usage, at module level, before any constant reads os.environ:

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from iac_secrets import load_env
    load_env()

Values are set with setdefault, so an already-exported variable always wins:
`./j` (sops exec-env) and the environment of a CI runner both come in that way,
and a script must not undo them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_FILE = REPO_ROOT / "secrets" / "app.env"

# How SOPS gets at this machine's private age key. It does not find it on every
# platform by itself: on macOS its default is ~/Library/Application Support/
# sops/age/keys.txt (Go's os.UserConfigDir), so the conventional ~/.config path
# is missed. The failure reads "no identity matched any of the recipients", a
# complaint about the recipient list for a fault in the lookup path.
#
# scripts/age-key.sh resolves the identity and prints it; ./j and the justfile
# point at the same script, so the three cannot drift apart.
AGE_KEY_CMD = REPO_ROOT / "scripts" / "age-key.sh"


def sops_binary() -> str | None:
    """Absolute path to the sops CLI, or None if it is not installed.

    cron runs with PATH=/usr/bin:/bin, and Homebrew's sops is in
    /opt/homebrew/bin: a bare "sops" that resolves in every interactive shell
    raises FileNotFoundError in precisely the scheduled runs this exists for.
    Same trap, same fix as gh_binary() in monitor-check.py; verify a change here
    with `env -i PATH=/usr/bin:/bin HOME=$HOME`.

    ~/.local/bin is in the list for hosts without a package manager, where
    sops and uv are installed as plain binaries.
    """
    return shutil.which(
        "sops",
        path=os.environ.get("PATH", "")
        + ":/opt/homebrew/bin:/usr/local/bin:"
        + str(Path.home() / ".local" / "bin"),
    )


def parse_dotenv(text: str) -> dict[str, str]:
    """The dotenv subset this repo writes: KEY=value, # comments, blank lines."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def decrypt_text(secrets_file: Path | None = None) -> str:
    """The decrypted file as dotenv text. Empty string when there is nothing.

    Two ways this is legitimately empty:

    * the file is missing — a checkout without secrets, the same tolerance the
      old ``if not ENV_FILE.exists()`` had.
    * sops is not installed: a runner whose environment is injected from
      somewhere else (a CI secret store, for example). Failing here would
      break every run to fix nothing.

    A failing decrypt, by contrast, raises. sops being present says the caller
    meant to use it, so "cannot open the file" is a real defect (usually a
    missing or wrong age key) and must not fall through to defaults — that is
    the silent-fallback failure mode the whole alert chain is built to avoid.
    """
    path = secrets_file or SECRETS_FILE
    if not path.exists():
        return ""
    sops = sops_binary()
    if sops is None:
        return ""

    env = dict(os.environ)
    # Unconditional, and never the empty string: an empty SOPS_AGE_KEY_FILE is
    # skipped by sops, but an empty SOPS_AGE_KEY_CMD panics it (3.13.3, slice
    # bounds out of range in age.getOutputFromCmd). An already-set value wins,
    # the same rule setdefault() follows for the secrets themselves.
    env.setdefault("SOPS_AGE_KEY_CMD", str(AGE_KEY_CMD))

    proc = subprocess.run(
        [sops, "-d", "--output-type", "dotenv", str(path)],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"sops could not decrypt {path}: {proc.stderr.strip()}\n"
            f"Does {AGE_KEY_CMD} find this machine's age key, "
            f"and is its public half listed in .sops.yaml?"
        )
    return proc.stdout


def load_env(secrets_file: Path | None = None) -> int:
    """Populate os.environ from the encrypted secrets. Returns keys added.

    A no-op when ``IAC_SECRETS`` is set: the process already runs under ./j,
    so sops has decrypted into this environment and doing it again would cost
    a second decrypt to change nothing. Otherwise see decrypt_text().
    """
    if os.environ.get("IAC_SECRETS"):
        return 0

    # Only claim IAC_SECRETS when something was actually decrypted. Setting it
    # after a legitimate no-op would tell the next caller "already loaded" and
    # make the marker mean intent instead of delivery — the exact distinction
    # the justfile guard relies on.
    text = decrypt_text(secrets_file)
    if not text:
        return 0

    added = 0
    for k, v in parse_dotenv(text).items():
        if k.startswith("sops_"):  # SOPS' own metadata, not a secret
            continue
        if k not in os.environ:
            os.environ[k] = v
            added += 1
    os.environ["IAC_SECRETS"] = "1"
    return added
