#!/usr/bin/env python3
"""Read one key out of an encrypted dotenv file, on the controller, in memory.

    DB_PASSWORD={{ lookup('sops_dotenv', 'DB_PASSWORD') }}

This is how an encrypted secrets file reaches a playbook WITHOUT reaching the
process environment. `./j` pours secrets/app.env into the environment of one
command; a lookup asks for exactly the key it needs, which is what the
service_deploy role uses to render a host's .env (`env_from_secrets`).

Lookups always run on the controller, which is also the only machine with the
age key, so this works for a play targeting a remote host without that host
ever holding a secret it was not meant to hold.

A missing key is a hard error, never an empty string. That is the whole reason
this plugin exists instead of `lookup('env', ...)`: the values it serves are
credentials that a running container is going to use, and rendering an empty
one produces a `.env` that looks fine and breaks the stack on next start. The
same rule as the `_secrets` guard in the justfile.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

DOCUMENTATION = r"""
name: sops_dotenv
short_description: read a key from a SOPS-encrypted dotenv file
description:
  - Decrypts a SOPS dotenv file on the controller and returns the value of each
    requested key. The plaintext is never written to disk and never enters the
    process environment.
  - Raises when a key is absent. Callers want a credential, and an empty string
    is a broken service that reports success.
options:
  _terms:
    description: key names to look up
    required: true
    type: list
    elements: str
  file:
    description: path to the encrypted dotenv file, relative to the repo root
    default: secrets/app.env
    type: str
"""

EXAMPLES = r"""
- name: Render a stack .env from the encrypted secrets
  ansible.builtin.copy:
    dest: /home/deploy/apps/miniflux/.env
    content: |
      POSTGRES_PASSWORD={{ lookup('sops_dotenv', 'MINIFLUX_DB_PASSWORD') }}
  no_log: true

- name: Another file
  ansible.builtin.debug:
    msg: "{{ lookup('sops_dotenv', 'GITHUB_TOKEN', file='secrets/other.env') }}"
"""

RETURN = r"""
_raw:
  description: the value of each requested key, in order
  type: list
  elements: str
"""

# ansible/lookup_plugins/sops_dotenv.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from iac_secrets import decrypt_text, parse_dotenv  # noqa: E402

# One decrypt per file per ansible run. A play that renders a dozen keys would
# otherwise pay a sops invocation each time, and `sops` is ~200 ms.
_CACHE: dict[str, dict[str, str]] = {}


def _load(path: Path) -> dict[str, str]:
    key = str(path)
    if key not in _CACHE:
        if not path.exists():
            raise AnsibleError(
                f"sops_dotenv: {path} does not exist. Create it from "
                f"secrets/example.env, see docs/secrets.md"
            )
        try:
            text = decrypt_text(path)
        except RuntimeError as exc:
            raise AnsibleError(f"sops_dotenv: {exc}") from exc
        if not text:
            # decrypt_text() is deliberately quiet when sops is missing, for
            # runners whose environment comes from somewhere else. A play that
            # asks for a key by name has no such fallback, so here that silence
            # has to become noise.
            raise AnsibleError(
                f"sops_dotenv: nothing decrypted from {path}. Is sops installed "
                f"on this controller, and is the age key readable?"
            )
        _CACHE[key] = {k: v for k, v in parse_dotenv(text).items()
                       if not k.startswith("sops_")}
    return _CACHE[key]


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)
        rel = self.get_option("file")
        # `REPO_ROOT / rel` is NOT confinement: pathlib discards the left
        # operand when the right one is absolute, so `REPO_ROOT / "/etc/passwd"`
        # is `/etc/passwd`. The file name is author-supplied through
        # services.yml's env_from_secrets, so it has to be checked here too and
        # not only in the schema.
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise AnsibleError(
                f"sops_dotenv: file={rel!r} must be a repo-relative path "
                f"without '..' — it is resolved against {REPO_ROOT}"
            )
        path = REPO_ROOT / rel
        data = _load(path)

        out = []
        for term in terms:
            if term not in data:
                raise AnsibleError(
                    f"sops_dotenv: key {term!r} not found in {path}. "
                    f"Add it with `just secrets-edit {rel}`."
                )
            out.append(data[term])
        return out
