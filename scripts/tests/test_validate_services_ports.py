"""Behind the reverse proxy, a service needs no port of its own.

A published port is a second door, and the worse one: Docker writes its port
publications ahead of the UFW chains, so the host firewall does not govern
them. On a host with a default-deny firewall, a published 8080 or 5432 is
reachable from the internet anyway. On top of that, the port bypasses everything
the proxy applies (TLS, authentication middlewares).

The rule hangs on `domain:`, because that is what means "runs through the
proxy". A service without a domain may well need its port, which is
deliberately not an error here.

The two non-hits are as important as the hit: a port on 127.0.0.1 is the *fix*
and must not come back as an error, and a volume line must not be read as a port.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module():
    path = REPO_ROOT / "scripts" / "validate_services.py"
    spec = importlib.util.spec_from_file_location("validate_services_ports", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_services_ports"] = module
    spec.loader.exec_module(module)
    return module


vs = _load_module()


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Create services/svc/docker-compose.yml and point REPO there."""

    def build(compose_body: str, name: str = "svc"):
        root = tmp_path / "services" / name
        root.mkdir(parents=True)
        (root / "docker-compose.yml").write_text(compose_body)
        monkeypatch.setattr(vs, "REPO", tmp_path)
        return root

    return build


def compose(ports: str = "", extra: str = "") -> str:
    return f"""\
services:
  svc:
    image: example/svc:latest
{ports}{extra}    networks:
      - proxy
"""


def test_a_published_port_next_to_a_domain_is_an_error(service):
    service(compose(ports='    ports:\n      - "8888:8080"\n'))
    err = vs.published_port_error("svc", {"host": "edge-vps", "domain": "svc"})
    assert err is not None
    assert "8888:8080" in err
    # The message must carry the reason, not just the finding.
    assert "UFW" in err


def test_loopback_is_not_a_finding(service):
    """That is the fix for a public port: it must not come back as an error."""
    service(compose(ports='    ports:\n      - "127.0.0.1:5432:5432"\n'))
    assert vs.published_port_error("svc", {"host": "edge-vps", "domain": "svc"}) is None


def test_explicit_all_interfaces_counts_as_public(service):
    service(compose(ports='    ports:\n      - "0.0.0.0:3000:3000"\n'))
    err = vs.published_port_error("svc", {"host": "edge-vps", "domain": "svc"})
    assert err is not None and "3000:3000" in err


def test_a_service_without_a_domain_keeps_its_port(service):
    """No proxy labels: the port is the only way in."""
    service(compose(ports='    ports:\n      - "8082:8080"\n'))
    assert vs.published_port_error("svc", {"host": "edge-vps"}) is None


def test_the_proxys_own_entrypoints_are_allowed(service):
    service(compose(ports='    ports:\n      - "80:80"\n      - "443:443"\n'))
    assert vs.published_port_error("svc", {"host": "edge-vps", "domain": "traefik"}) is None


def test_a_waiver_with_a_reason_silences_the_rule(service):
    service(compose(ports='    ports:\n      - "8888:8080"\n'))
    spec = {
        "host": "edge-vps",
        "domain": "svc",
        "published_port_ok": "Needed by a host-level agent; firewall-restricted.",
    }
    assert vs.published_port_error("svc", spec) is None


def test_a_volume_line_is_not_mistaken_for_a_port(service):
    """`- ./data:/data` and `- /etc/localtime:/etc/localtime:ro` are not ports."""
    service(compose(extra=(
        "    volumes:\n"
        "      - ./data:/data\n"
        "      - /etc/localtime:/etc/localtime:ro\n"
        "      - ./config:/app/config:ro\n"
    )))
    assert vs.published_port_error("svc", {"host": "edge-vps", "domain": "svc"}) is None


def test_a_service_without_a_directory_stays_silent(tmp_path, monkeypatch):
    """The missing directory is deployable_error's finding, not this rule's."""
    monkeypatch.setattr(vs, "REPO", tmp_path)
    assert vs.published_port_error("absent", {"host": "edge-vps", "domain": "x"}) is None


def test_the_real_services_yml_stays_green():
    data = vs.load_yaml(REPO_ROOT / "services.yml")
    assert vs.contract_errors(data) == []


def test_no_shipped_service_publishes_a_wildcard_port_behind_a_domain():
    """The shipped examples practice what the rule preaches."""
    data = vs.load_yaml(REPO_ROOT / "services.yml")
    defaults = data.get("defaults") or {}
    for name, raw in (data.get("services") or {}).items():
        spec = {**defaults, **(raw or {})}
        if not spec.get("domain"):
            continue
        compose_path = REPO_ROOT / "services" / name / spec.get("compose_file", "docker-compose.yml")
        assert vs.public_port_publications(compose_path) == [], name
