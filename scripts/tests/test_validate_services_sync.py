"""sync: the positive list of what reaches the host next to the compose file.

The rule these tests pin down is the one that is easy to get subtly wrong: a
`./` mount must be declared when its source lives in the service directory, and
must NOT be declared when it does not. Both halves matter, and they fail in
opposite directions: an undeclared sidecar makes docker create a directory where
the container expects a file, while a declared runtime path pushes a committed
snapshot over the live database.

The tests build a throwaway repo layout and point the module's REPO at it, so
nothing here depends on the shipped services.yml staying the way it is today.
One test does read the real file: that `just validate` stays green.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module():
    """Import validate_services.py by path: it is a uv script, not a package."""
    path = REPO_ROOT / "scripts" / "validate_services.py"
    spec = importlib.util.spec_from_file_location("validate_services", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_services"] = module
    spec.loader.exec_module(module)
    return module


vs = _load_module()


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A minimal services/<name> layout with a compose file.

    Returns a helper that writes the service directory and points the module at it.
    """

    def build(compose_body: str, files: dict[str, str] | None = None, name: str = "svc"):
        root = tmp_path / "services" / name
        root.mkdir(parents=True)
        (root / "docker-compose.yml").write_text(compose_body)
        for rel, body in (files or {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        monkeypatch.setattr(vs, "REPO", tmp_path)
        return root

    return build


COMPOSE_SIDECAR = """\
services:
  svc:
    image: example/svc:latest
    volumes:
      - ./data:/data
      - ./init-user.py:/opt/svc/init-user.py
"""


def test_undeclared_sidecar_is_an_error(fake_repo):
    """The source exists in the service directory, so it has to be declared."""
    fake_repo(COMPOSE_SIDECAR, {"init-user.py": "print('hi')\n"})
    errors = vs.sync_errors("svc", {"host": "edge-vps"}, "compose-pull")
    assert len(errors) == 1
    assert "init-user.py" in errors[0]
    # The message has to say what goes wrong on the host, not just "missing".
    assert "directory" in errors[0]


def test_declared_sidecar_is_accepted(fake_repo):
    fake_repo(COMPOSE_SIDECAR, {"init-user.py": "print('hi')\n"})
    spec = {"host": "edge-vps", "sync": ["init-user.py"]}
    assert vs.sync_errors("svc", spec, "compose-pull") == []


def test_runtime_mount_absent_from_the_repo_is_not_demanded(fake_repo):
    """`./data` is the live database on the host: declaring it would overwrite it.

    This is the half that must stay silent. It is also why the check asks the
    filesystem instead of parsing intent out of the path name.
    """
    fake_repo(COMPOSE_SIDECAR, {"init-user.py": "print('hi')\n"})
    spec = {"host": "edge-vps", "sync": ["init-user.py"]}
    assert vs.sync_errors("svc", spec, "compose-pull") == []
    # And it flips as soon as the path DOES exist in the repo, because then a
    # deploy really would have to carry it.
    (vs.REPO / "services" / "svc" / "data").mkdir()
    assert any("./data" in e for e in vs.sync_errors("svc", spec, "compose-pull"))


def test_sync_on_a_non_compose_pull_service_is_rejected(fake_repo):
    """rsync-build ships the whole tree; an entry there would look load-bearing
    and do nothing."""
    fake_repo(COMPOSE_SIDECAR, {"init-user.py": "print('hi')\n"})
    spec = {"host": "edge-vps", "sync": ["init-user.py"]}
    errors = vs.sync_errors("svc", spec, "rsync-build")
    assert len(errors) == 1
    assert "compose-pull" in errors[0]


def test_a_typo_in_sync_is_an_error(fake_repo):
    fake_repo(COMPOSE_SIDECAR, {"init-user.py": "print('hi')\n"})
    spec = {"host": "edge-vps", "sync": ["init-usr.py"]}
    errors = vs.sync_errors("svc", spec, "compose-pull")
    assert any("init-usr.py" in e and "does not exist" in e for e in errors)


def test_a_service_without_a_directory_stays_silent(fake_repo, tmp_path, monkeypatch):
    """The missing directory is deployable_error's finding, not this rule's."""
    monkeypatch.setattr(vs, "REPO", tmp_path)
    assert vs.sync_errors("absent", {"host": "edge-vps"}, "compose-pull") == []


def test_compose_file_itself_needs_no_sync_entry(fake_repo):
    """It is copied unconditionally, so demanding it would be noise."""
    fake_repo(
        "services:\n  svc:\n    image: x\n    volumes:\n      - ./docker-compose.yml:/tmp/c.yml\n"
    )
    assert vs.sync_errors("svc", {"host": "edge-vps"}, "compose-pull") == []


def test_long_syntax_and_nested_paths(fake_repo):
    """A nested source is reported with its path, not just its basename."""
    fake_repo(
        "services:\n  svc:\n    image: x\n    volumes:\n      - ./config/app.yml:/etc/app.yml:ro\n",
        {"config/app.yml": "key: value\n"},
    )
    errors = vs.sync_errors("svc", {"host": "edge-vps"}, "compose-pull")
    assert any("./config/app.yml" in e for e in errors)
    spec = {"host": "edge-vps", "sync": ["config/app.yml"]}
    assert vs.sync_errors("svc", spec, "compose-pull") == []


def test_the_real_services_yml_has_no_contract_errors():
    """Reads the repo's own services.yml: the same thing `just validate` does."""
    data = vs.load_yaml(REPO_ROOT / "services.yml")
    assert vs.contract_errors(data) == []
