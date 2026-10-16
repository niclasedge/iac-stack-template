"""The remaining contract rules: deployability, patterns, domains.

  * a service that cannot be deployed must fail validation, not the deploy
    (`deployable_error`): compose-pull/rsync-build need services/<name>/ here;
  * compose-pull cannot carry a `build:` context, rsync-build is capped by size;
  * resolved domains must not collide, `health` needs a `domain`, git-pull needs
    a `repo`;
  * the shipped services.yml also passes the JSON schema.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module():
    path = REPO_ROOT / "scripts" / "validate_services.py"
    spec = importlib.util.spec_from_file_location("validate_services_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_services_contract"] = module
    spec.loader.exec_module(module)
    return module


vs = _load_module()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "REPO", tmp_path)

    def add(name: str, compose: str, files: dict[str, str] | None = None):
        root = tmp_path / "services" / name
        root.mkdir(parents=True)
        (root / "docker-compose.yml").write_text(compose)
        for rel, body in (files or {}).items():
            (root / rel).write_text(body)
        return root

    return add


class TestDeployable:
    def test_an_active_service_without_a_directory_is_an_error(self, repo):
        err = vs.deployable_error("nope", {"state": "active"}, "compose-pull")
        assert err and "does not exist" in err

    def test_an_inactive_service_is_a_parked_declaration(self, repo):
        assert vs.deployable_error("nope", {"state": "inactive"}, "compose-pull") is None

    def test_git_pull_needs_no_local_directory(self, repo):
        assert vs.deployable_error("nope", {"state": "active"}, "git-pull") is None

    def test_a_present_compose_file_passes(self, repo):
        repo("here", "services: {}\n")
        assert vs.deployable_error("here", {"state": "active"}, "compose-pull") is None


class TestPattern:
    def test_compose_pull_with_build_is_an_error(self, repo):
        repo("svc", "services:\n  svc:\n    build: .\n")
        err = vs.compose_pattern_error("svc", {}, "compose-pull")
        assert err and "build:" in err

    def test_rsync_build_with_build_is_fine(self, repo):
        repo("svc", "services:\n  svc:\n    build: .\n", {"Dockerfile": "FROM scratch\n"})
        assert vs.compose_pattern_error("svc", {}, "rsync-build") is None

    def test_rsync_build_over_the_size_cap_is_an_error(self, repo, monkeypatch):
        repo("svc", "services:\n  svc:\n    image: x\n", {"blob.bin": "x" * 2048})
        monkeypatch.setattr(vs, "RSYNC_BUILD_MAX_BYTES", 1024)
        err = vs.compose_pattern_error("svc", {}, "rsync-build")
        assert err and "WHOLE working tree" in err


class TestDomains:
    def test_a_domain_collision_is_reported_across_aliases(self):
        data = {
            "defaults": {"domain_suffix": "example.com"},
            "services": {
                "a": {"domain": "app", "state": "inactive"},
                "b": {"domain": "other", "aliases": ["app"], "state": "inactive"},
            },
        }
        errors = vs.contract_errors(data)
        assert any("Domain collision 'app.example.com'" in e for e in errors)

    def test_the_dot_rule(self):
        assert vs.resolve_domain("app", "example.com") == "app.example.com"
        assert vs.resolve_domain("app.other.org", "example.com") == "app.other.org"

    def test_health_without_a_domain_is_an_error(self):
        data = {"services": {"a": {"health": "/", "state": "inactive"}}}
        assert any("no domain" in e for e in vs.contract_errors(data))

    def test_git_pull_without_a_repo_is_an_error(self):
        data = {"services": {"a": {"deploy": "git-pull", "state": "inactive"}}}
        assert any("without repo" in e for e in vs.contract_errors(data))


class TestShippedFiles:
    def test_services_yml_matches_the_schema(self):
        data = vs.load_yaml(REPO_ROOT / "services.yml")
        schema = json.loads((REPO_ROOT / "services.schema.json").read_text())
        assert vs.schema_errors(data, schema) == []

    def test_every_active_service_has_a_directory_with_a_compose_file(self):
        data = vs.load_yaml(REPO_ROOT / "services.yml")
        defaults = data.get("defaults") or {}
        for name, raw in data["services"].items():
            spec = {**defaults, **(raw or {})}
            if spec.get("state", "active") != "active" or spec.get("deploy") == "git-pull":
                continue
            assert (REPO_ROOT / "services" / name / spec.get("compose_file", "docker-compose.yml")).is_file(), name

    def test_every_host_is_in_the_example_inventory(self):
        import yaml

        data = vs.load_yaml(REPO_ROOT / "services.yml")
        inventory = yaml.safe_load(
            (REPO_ROOT / "ansible" / "inventory" / "inventory.example.yml").read_text()
        )
        hosts: set[str] = set()

        def walk(node):
            for key, value in (node or {}).items():
                if key == "hosts":
                    hosts.update(value)
                elif isinstance(value, dict):
                    walk(value)

        walk(inventory)
        defaults = data.get("defaults") or {}
        for name, raw in data["services"].items():
            host = {**defaults, **(raw or {})}.get("host")
            assert host in hosts, f"{name}: host {host!r} is not in the example inventory"
