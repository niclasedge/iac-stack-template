#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""services.py: overview of services.yml.

    ./scripts/services.py status

prints one row per service: host, state, deploy pattern, resolved domain and
health path, plus whether services/<name>/ exists on this controller. Read-only;
it never talks to a host. `just validate` is the gate, this is the overview.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SERVICES_YML = REPO / "services.yml"


def resolve_domain(value: str, suffix: str) -> str:
    """Dot rule, identical to deploy.yml and validate_services.py."""
    return value if "." in value else f"{value}.{suffix}"


def rows(data: dict) -> list[list[str]]:
    defaults = data.get("defaults") or {}
    out = []
    for name, raw in sorted((data.get("services") or {}).items()):
        spec = {**defaults, **(raw or {})}
        domain = spec.get("domain")
        resolved = resolve_domain(domain, spec.get("domain_suffix", "")) if domain else "-"
        compose = REPO / "services" / name / spec.get("compose_file", "docker-compose.yml")
        out.append(
            [
                name,
                str(spec.get("host", "-")),
                str(spec.get("state", "active")),
                str(spec.get("deploy", "compose-pull")),
                resolved,
                str(spec.get("health", "-")),
                "yes" if compose.is_file() else "no",
            ]
        )
    return out


def status() -> int:
    data = yaml.safe_load(SERVICES_YML.read_text()) or {}
    header = ["service", "host", "state", "deploy", "domain", "health", "compose here"]
    table = [header, *rows(data)]
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    for i, row in enumerate(table):
        print("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    return 0


def main(argv: list[str]) -> int:
    if argv[1:2] == ["status"]:
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
