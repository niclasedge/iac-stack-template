#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml", "jsonschema"]
# ///
"""
validate_services.py — check services.yml against services.schema.json plus
contract rules.

Two layers:
  1. JSON schema (services.schema.json): structure, types, enums.
  2. Rules a schema cannot express:
       - deploy: git-pull needs repo
       - the service directory services/<name>/ must contain the compose file
         (except git-pull, which acquires on the target host)
       - compose-pull tolerates no `build:`, and rsync-build never transfers
         more than RSYNC_BUILD_MAX_BYTES
       - `sync:` covers every `./` mount whose source exists in the service
         directory, and applies to compose-pull only
       - a service with `domain:` must not publish a port: the port bypasses
         the firewall and the reverse proxy's middlewares
       - resolved domains must not collide (across hosts and states, aliases
         included)
       - health needs domain
       - env_from_secrets references use `[<file>:]KEY` from a fixed allowlist
       - domain resolution follows the dot rule (see resolve_domain)

Reports ALL errors in one run, not just the first one; otherwise editing is a
guessing game. Exit 1 on errors, 0 on success.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parent.parent
SERVICES_YML = REPO / "services.yml"
SCHEMA_JSON = REPO / "services.schema.json"
SERVICES_DIR = "services"

_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""


def resolve_domain(value: str, suffix: str) -> str:
    """Dot rule: a dot in the value means "already complete", else append the suffix.

    Must match the logic in ansible/playbooks/deploy.yml exactly; the contract
    check and the playbook both rely on it.
    """
    if "." in value:
        return value
    return f"{value}.{suffix}"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def schema_errors(data: dict, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"Schema {loc}: {err.message}")
    return errors


# The size at which an rsync-build working tree becomes a problem. rsync-build
# transfers the WHOLE service directory, so this cap only trips when the
# directory holds more than the service needs (build artifacts, datasets,
# a vendored monorepo). A small example app is a few KB.
#
# 50 MB is a limit, not a rule: it sits orders of magnitude above the legitimate
# case and only trips when the directory contains more than the service. Adjust
# it if a build context is legitimately larger.
RSYNC_BUILD_MAX_BYTES = 50 * 1024 * 1024


def tree_bytes(path: Path) -> tuple[int, int]:
    """(bytes, files) of a working tree, without .git."""
    total = count = 0
    for f in path.rglob("*"):
        if ".git" in f.parts:
            continue
        if f.is_file() and not f.is_symlink():
            total += f.stat().st_size
            count += 1
    return total, count


def service_root(name: str) -> Path:
    """Controller-side directory of a service: services/<name>.

    The path follows the SERVICE NAME, not `path:`; `path:` only moves the
    target on the host, the layout in this repo stays flat.
    """
    return REPO / SERVICES_DIR / name


def local_mount_sources(compose: Path) -> list[str]:
    """`./`-relative bind-mount sources in a compose file, as written.

    Deliberately a line scan rather than a YAML parse: the file may use either
    volume syntax and `${VAR}` interpolation, and this only needs the source
    side of `- ./x:/y` entries. Over-reporting is harmless: every hit is
    checked against the service directory below, and a path that is not in the
    repo is runtime state, which drops out there.
    """
    sources: list[str] = []
    for line in compose.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ./") or ":" not in stripped:
            continue
        src = stripped[2:].split(":", 1)[0].strip()
        if src.startswith("./"):
            sources.append(src[2:].rstrip("/"))
    return sources


# `- "8888:8080"`, `- 8888:8080`, `- "127.0.0.1:5432:5432"`, `- "[::1]:80:80/tcp"`.
# Anchored on digits so a volume line (`- ./data:/data`) cannot match: a path is
# not a port number, and that is the whole difference between the two.
PORT_LINE = re.compile(
    r"""^-\s*["']?
        (?:(?P<ip>\d[\d.]*|\[[^\]]+\]):)?   # optional bind address
        (?P<host>\d+):(?P<container>\d+)
        (?:/(?:tcp|udp))?["']?$""",
    re.X,
)

# The reverse proxy itself must publish these: they are the entrypoints every
# other service is reached through. No second service can publish them anyway:
# the port would collide with the proxy and the container would not start.
ENTRYPOINT_PORTS = {"80", "443"}


def public_port_publications(compose: Path) -> list[str]:
    """Ports the compose file publishes on every interface.

    A bind address of 127.0.0.1 (or [::1]) is not one of them; that is the fix
    for an accidentally public port, and the rule must not ask for it to be
    undone. `0.0.0.0` is spelled out sometimes and means "everywhere", so it
    counts as public.
    """
    found = []
    for line in compose.read_text().splitlines():
        m = PORT_LINE.match(line.strip())
        if not m:
            continue
        ip = m.group("ip")
        if ip and ip not in ("0.0.0.0", "[::]"):
            continue  # bound to loopback or a specific address
        if m.group("host") in ENTRYPOINT_PORTS:
            continue
        found.append(f"{m.group('host')}:{m.group('container')}")
    return found


def published_port_error(name: str, spec: dict) -> str | None:
    """A service behind the reverse proxy must not also publish a port.

    The port is a SECOND door, and the worse one: Docker writes its port
    publications into iptables ahead of the UFW chains, so the host firewall
    does not govern it. It also bypasses whatever the proxy applies (TLS,
    authentication middlewares).

    Keyed on `domain:` because that is what says "reachable through the proxy".
    A service without one may legitimately need its port.

    `published_port_ok: <reason>` waives it. There has to be a way out, because
    the rule's premise is a public IP with a firewall behind Docker's iptables;
    on a host that sits behind a NAT with no port forward, the premise does not
    hold. Every waiver is printed on every run (see summarize()).
    """
    if spec.get("published_port_ok"):
        return None
    if not spec.get("domain"):
        return None
    compose = service_root(name) / spec.get("compose_file", "docker-compose.yml")
    if not compose.is_file():
        # Reported by deployable_error instead.
        return None
    public = public_port_publications(compose)
    if not public:
        return None
    return (
        f"{name}: has domain {spec['domain']} (so the proxy routes it) but "
        f"{compose.name} still publishes {', '.join(public)} on every interface. "
        f"Docker's iptables rules sit in front of UFW, so that port is open to "
        f"the network and skips the proxy's TLS and middlewares. Remove it, or "
        f"bind it to 127.0.0.1 if something on the host needs it."
    )


def sync_errors(name: str, spec: dict, deploy: str) -> list[str]:
    """`sync:` is the positive list of what travels to the host besides the compose file.

    Three rules, and the middle one is the whole point of the mechanism:

      - `sync:` is compose-pull only. rsync-build transfers the whole tree
        anyway, git-pull clones on the target; an entry there would look
        load-bearing and do nothing, which is worse than not offering it.
      - a `./` mount whose source EXISTS in the service directory must be
        declared, or it never reaches the host. On a fresh host docker then
        creates a directory at that source and the container fails on a path
        that is suddenly a folder. A source that does NOT exist in the repo is
        runtime state (a live database file, for example) and must stay
        undeclared: copying it would push a committed snapshot over the
        running file.
      - a declared path must exist, otherwise it is a typo with the same
        directory-instead-of-file outcome.
    """
    errors: list[str] = []
    declared = spec.get("sync") or []

    if declared and deploy != "compose-pull":
        errors.append(
            f"{name}: sync: is only implemented for compose-pull, but deploy is "
            f"{deploy}; rsync-build already transfers the whole tree and "
            f"git-pull clones on the target, so the entry would do nothing."
        )
        return errors

    root = service_root(name)
    for entry in declared:
        if not (root / entry).exists():
            errors.append(
                f"{name}: sync: names '{entry}', which does not exist in "
                f"{root.relative_to(REPO)}"
            )

    if deploy != "compose-pull":
        return errors

    compose = root / spec.get("compose_file", "docker-compose.yml")
    if not compose.is_file():
        # Reported by deployable_error instead.
        return errors

    covered = set(declared) | {spec.get("compose_file", "docker-compose.yml")}
    for src in local_mount_sources(compose):
        if src in covered:
            continue
        if not (root / src).exists():
            continue  # runtime state on the host, not ours to ship
        errors.append(
            f"{name}: {compose.name} mounts ./{src}, which exists in "
            f"{root.relative_to(REPO)} but is not in sync: (compose-pull copies "
            f"the compose file plus sync: only, so it would be missing on the "
            f"host and docker would create a directory in its place)."
        )
    return errors


def compose_pattern_error(name: str, spec: dict, deploy: str) -> str | None:
    """Pattern against reality: build context and transfer volume.

    Two rules, deliberately of different strictness:

      - `compose-pull` with `build:` is plainly broken. The pattern copies only
        the compose file, so the build context is missing on the host.
      - `rsync-build` without `build:` is NOT automatically wrong. The name says
        "build", but what the case really needs is "transfer the tree", which
        is right for a service with a sidecar file next to its compose file.
        It becomes wrong by volume, not by label.

    A missing compose file stays silent here; deployable_error reports it.
    """
    if deploy not in ("rsync-build", "compose-pull"):
        return None
    compose = spec.get("compose_file", "docker-compose.yml")
    root = service_root(name)
    path = root / compose
    if not path.is_file():
        return None

    has_build = any(
        line.strip().startswith("build:") for line in path.read_text().splitlines()
    )
    if deploy == "compose-pull" and has_build:
        return (
            f"{name}: deploy: compose-pull, but {compose} has a `build:`; "
            f"compose-pull copies only the compose file, so the build context "
            f"is missing on the host. rsync-build transfers it."
        )
    if deploy == "rsync-build":
        size, files = tree_bytes(root)
        if size > RSYNC_BUILD_MAX_BYTES:
            mb = size / 1024 / 1024
            return (
                f"{name}: deploy: rsync-build transfers the WHOLE working tree, "
                f"and it is {mb:,.0f} MB in {files} files, more than the "
                f"service needs. Either the directory holds unrelated material "
                f"(separate it) or the pattern is wrong. Every deploy writes "
                f"that volume to the host."
            )
    return None


# Which encrypted files an env_from_secrets reference may name. An allowlist,
# not a path check: the reference ends up in `REPO_ROOT / <file>` inside
# lookup_plugins/sops_dotenv.py, and pathlib DISCARDS the left operand when the
# right one is absolute: `REPO_ROOT / "/etc/passwd"` is `/etc/passwd`. A fixed
# name cannot express that, a free path can. Add further files here when you
# split secrets (and to .sops.yaml and ./j).
SECRET_FILES = ("secrets/app.env",)
DEFAULT_SECRET_FILE = "secrets/app.env"
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def split_secret_ref(ref: str) -> tuple[str, str]:
    """`[<file>:]<KEY>` -> (file, key). No validation, just the split.

    rsplit, not split: a dotenv key can never contain a colon, so the LAST
    colon is the unambiguous separator even for a path that contains one.
    A bare key means the default secrets file.
    """
    if ":" not in ref:
        return DEFAULT_SECRET_FILE, ref
    file, key = ref.rsplit(":", 1)
    return file, key


def deployable_error(name: str, spec: dict, deploy: str) -> str | None:
    """A service that cannot be deployed must fail validation, not the deploy.

    compose-pull and rsync-build both read the compose file from
    services/<name>/ on the controller (compose-pull copies it, rsync-build
    transfers the tree). Without it the role fails at the acquisition step while
    `just validate` stays green; a gate that passes for something that cannot
    run is the failure mode this check exists to prevent.

    git-pull acquires on the host from `repo:`, which the git-pull rule already
    requires. Inactive services are exempt: they are parked declarations.
    """
    if deploy not in ("compose-pull", "rsync-build"):
        return None
    if spec.get("state", "active") != "active":
        return None
    compose = service_root(name) / spec.get("compose_file", "docker-compose.yml")
    if not compose.is_file():
        return (
            f"{name}: deploy: {deploy}, but {compose.relative_to(REPO)} does not "
            f"exist; add the service directory or set state: inactive"
        )
    return None


def env_from_secrets_errors(name: str, spec: dict) -> list[str]:
    """The map's right-hand side is `[<file>:]<KEY>` and nothing else.

    Worth enforcing here rather than trusting the schema alone: a malformed
    reference does not fail loudly at render time in a useful way. An empty key
    makes the sops_dotenv lookup report 'key not found' for the empty string,
    and a file outside the allowlist would be resolved against the repo root,
    or, if absolute, not resolved against it at all.
    """
    errors: list[str] = []
    for var, ref in (spec.get("env_from_secrets") or {}).items():
        file, key = split_secret_ref(ref)
        if not key:
            errors.append(f"{name}: env_from_secrets[{var}] has no key ('{ref}')")
        elif not KEY_RE.match(key):
            errors.append(
                f"{name}: env_from_secrets[{var}] key '{key}' is not a valid "
                f"dotenv name"
            )
        if file not in SECRET_FILES:
            errors.append(
                f"{name}: env_from_secrets[{var}] points at '{file}'; only "
                f"{', '.join(SECRET_FILES)} are allowed"
            )
    return errors


def contract_errors(data: dict) -> list[str]:
    """The contract rules that additionalProperties and enums alone do not cover."""
    errors: list[str] = []
    defaults = data.get("defaults") or {}
    services = data.get("services") or {}

    # resolved domain -> owners, for the collision check
    seen_domains: dict[str, list[str]] = {}

    for name, raw_spec in services.items():
        spec = {**defaults, **(raw_spec or {})}
        deploy = spec.get("deploy", "compose-pull")
        domain = spec.get("domain")
        suffix = spec.get("domain_suffix", "")
        aliases = spec.get("aliases") or []
        health = spec.get("health")

        # Rule: git-pull needs repo.
        if deploy == "git-pull" and not spec.get("repo"):
            errors.append(f"{name}: deploy: git-pull without repo")

        # Rule: env_from_secrets names an allowed file and a valid key.
        errors.extend(env_from_secrets_errors(name, spec))

        # Rule: the service directory must exist for the patterns that read it.
        err = deployable_error(name, spec, deploy)
        if err:
            errors.append(err)

        # Rule: the pattern must match reality. `deploy:` is a hand-assigned
        # label; nothing else checks it against the compose file or the data
        # volume. Details in compose_pattern_error().
        compose_problem = compose_pattern_error(name, spec, deploy)
        if compose_problem:
            errors.append(compose_problem)

        # Rule: what belongs on the host next to the compose file is declared,
        # not guessed. Rationale in sync_errors().
        errors += sync_errors(name, spec, deploy)

        # Rule: behind the proxy a service needs no port of its own. Rationale
        # in published_port_error().
        port_problem = published_port_error(name, spec)
        if port_problem:
            errors.append(port_problem)

        # Rule: health needs domain.
        if health and not domain:
            errors.append(f"{name}: health set ({health}), but no domain")

        # Collect domain resolution (main domain + aliases) for the collision
        # check below; state: inactive takes part on purpose.
        if domain:
            resolved = resolve_domain(domain, suffix)
            seen_domains.setdefault(resolved, []).append(name)
        for alias in aliases:
            resolved = resolve_domain(alias, suffix)
            seen_domains.setdefault(resolved, []).append(f"{name} (alias)")

    # Rule: domain collisions across all services, hosts and states.
    for resolved, owners in seen_domains.items():
        if len(owners) > 1:
            errors.append(f"Domain collision '{resolved}': " + ", ".join(sorted(owners)))

    return errors


def summarize(data: dict) -> None:
    defaults = data.get("defaults") or {}
    services = data.get("services") or {}
    default_state = defaults.get("state", "active")
    default_deploy = defaults.get("deploy", "compose-pull")

    active = sum(1 for v in services.values() if (v or {}).get("state", default_state) == "active")
    inactive = len(services) - active

    deploy_counts: dict[str, int] = {}
    for v in services.values():
        deploy = (v or {}).get("deploy", default_deploy)
        deploy_counts[deploy] = deploy_counts.get(deploy, 0) + 1

    print(f"{GREEN}ok{OFF} {len(services)} services ({active} active, {inactive} inactive)")
    parts = ", ".join(f"{k}: {v}" for k, v in sorted(deploy_counts.items()))
    print(f"{DIM}  deploy patterns - {parts}{OFF}")

    # Every exception to the port rule is named on every run. A security rule
    # that can be switched off once and then forgotten is no rule, and a green
    # "ok" above a silent waiver reads as "checked, all closed".
    for name, spec in sorted(services.items()):
        reason = (spec or {}).get("published_port_ok")
        if reason:
            print(f"{DIM}  port exception - {name}: {reason}{OFF}")


def main() -> int:
    if not SERVICES_YML.is_file():
        print(f"{RED}error{OFF} {SERVICES_YML} is missing")
        return 1
    if not SCHEMA_JSON.is_file():
        print(f"{RED}error{OFF} {SCHEMA_JSON} is missing")
        return 1

    data = load_yaml(SERVICES_YML)
    schema = json.loads(SCHEMA_JSON.read_text())

    errors = schema_errors(data, schema)
    # Contract rules also run on schema errors (best effort over
    # `defaults`/`services`), so a single broken service does not hide the
    # remaining checks. Only a fundamentally broken structure (services missing
    # or not a dict) makes a second round pointless.
    services = data.get("services")
    if isinstance(services, dict):
        errors += contract_errors(data)

    if errors:
        print(f"{BOLD}{RED}{len(errors)} errors:{OFF}")
        for e in errors:
            print(f"  {RED}x{OFF} {e}")
        return 1

    summarize(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
