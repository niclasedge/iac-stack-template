"""No plaintext secrets under secrets/.

`secrets/example.env` is the one plaintext file allowed there, and it holds
placeholders. Any other `secrets/*.env` is a real secrets file and must be
SOPS-encrypted: SOPS leaves the keys readable and replaces every value with
`ENC[...]`, and appends `sops_*` metadata lines. A file that has neither is a
plaintext file someone is about to commit.

This also guards the template itself: it must not ship a real secrets file.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SECRETS = REPO / "secrets"
PLACEHOLDER = re.compile(r"^(replace-me|your-[a-z-]+|admin|https?://\S+|[\w./~:-]*)$")


def real_secret_files() -> list[Path]:
    return sorted(p for p in SECRETS.glob("*.env") if p.name != "example.env")


def test_every_real_secrets_file_is_sops_encrypted() -> None:
    for path in real_secret_files():
        text = path.read_text()
        assert "sops_mac=" in text or "sops_version=" in text, (
            f"{path.relative_to(REPO)} has no SOPS metadata: it is a plaintext "
            f"secrets file. Encrypt it (`sops --encrypt --in-place {path.name}`) "
            f"before committing."
        )
        values = [
            line.split("=", 1)[1]
            for line in text.splitlines()
            if "=" in line and not line.startswith(("#", "sops_"))
        ]
        assert all(v.startswith("ENC[") for v in values), (
            f"{path.relative_to(REPO)} has values that are not ENC[...]"
        )


def test_the_example_file_holds_placeholders_only() -> None:
    """A real-looking value in example.env is the bug, caught here by shape."""
    for line in (SECRETS / "example.env").read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        assert PLACEHOLDER.match(value), (
            f"secrets/example.env: {key} holds {value!r}, which does not look "
            f"like a placeholder"
        )
        assert len(value) < 40, f"secrets/example.env: {key} value is suspiciously long"


def test_example_and_env_example_list_the_same_keys() -> None:
    def keys(path: Path) -> set[str]:
        return {
            line.split("=", 1)[0]
            for line in path.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        }

    assert keys(SECRETS / "example.env") == keys(REPO / ".env.example")
