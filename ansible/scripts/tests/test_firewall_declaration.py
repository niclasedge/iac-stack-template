"""The firewall declaration must never lock anyone out.

`runbooks/firewall.yml` converges the host against `ufw_rules` in
group_vars/vps: what is missing gets added, what is not declared gets DELETED.
That is the point and also the danger: a list without an SSH rule deletes the
only way back in, and on a VPS that means a support ticket instead of a repair.

The playbook has three safeguards (an assert on the SSH rule, add before
delete, abort on an empty current state). These tests pin them without a host
and without ufw: they read declaration and playbook as data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
GROUP_VARS = REPO / "ansible" / "inventory" / "group_vars" / "vps" / "main.yml"
PLAYBOOK = REPO / "ansible" / "playbooks" / "runbooks" / "firewall.yml"


def rules() -> list[dict]:
    return yaml.safe_load(GROUP_VARS.read_text())["ufw_rules"]


def tasks() -> list[dict]:
    doc = yaml.safe_load(PLAYBOOK.read_text())
    return [t for t in (doc[0].get("tasks") or []) if isinstance(t, dict)]


def test_ssh_is_declared() -> None:
    """Without this rule the next run deletes the access."""
    specs = [r["spec"] for r in rules()]
    assert any("22/tcp" in s for s in specs), f"no SSH rule in {specs}"


def test_every_rule_says_why() -> None:
    """A firewall line without a reason is one nobody dares to delete.

    That is how stale rules survive for years: they stand there, nobody knows
    what they are for, and so they stay, although nothing listens behind them.
    """
    for rule in rules():
        assert rule.get("why", "").strip(), f"{rule['spec']} has no rationale"


def test_no_rule_opens_a_docker_published_port() -> None:
    """UFW does not govern Docker publications: a rule for one would be a lie.

    Docker writes its port publications ahead of the UFW chains. Whoever
    "opens" 8080 or 3000 here suggests control that does not exist; such a port
    is closed in the service's compose file (see `just validate`).
    """
    docker_published = {"80", "3000", "8080", "8081", "8082", "8888", "5432"}
    for rule in rules():
        spec = rule["spec"]
        # Ports as whole tokens, not as a substring: " 80" would otherwise sit
        # inside " port 8081". A CIDR like 172.18.0.0/16 survives the `replace`
        # and is not a plain number afterwards.
        tokens = spec.replace("/tcp", "").replace("/udp", "").split()
        ports = {t for t in tokens if t.isdigit()}
        collision = ports & docker_published
        assert not collision, (
            f"'{spec}' concerns {sorted(collision)}, port(s) published through "
            "docker-proxy: UFW does not apply there, it belongs in the "
            "service's compose file"
        )


def test_the_playbook_refuses_a_list_without_ssh() -> None:
    guard = next((t for t in tasks() if "assert" in str(t).lower() and "22/tcp" in str(t)), None)
    assert guard, "no assert that enforces the SSH rule"
    assert "lock" in str(guard).lower()


def test_the_playbook_refuses_an_empty_current_state() -> None:
    """A failed read looks like "no rules" and would delete everything. The two
    cases must stay distinguishable."""
    guard = next((t for t in tasks() if "fw_have | length > 0" in str(t)), None)
    assert guard, "no assert against an empty/failed `ufw show added`"


def test_rules_are_added_before_any_are_deleted() -> None:
    """The reverse order has a window in which neither the old nor the new rule
    exists. For a monitoring agent that is a gap, for SSH the end of the session."""
    names = [str(t.get("name", "")) for t in tasks()]
    add = next(i for i, n in enumerate(names) if "Add the declared" in n)
    delete = next(i for i, n in enumerate(names) if "Delete the rules" in n)
    assert add < delete, f"delete (position {delete}) runs before add ({add})"


def test_the_result_is_asserted_not_assumed() -> None:
    """Turn the action into a result: after converging, read back and compare,
    otherwise the run reports success for a `ufw` that silently rejected a rule."""
    verify = next((t for t in tasks() if "symmetric_difference" in str(t)), None)
    assert verify, "no comparison of the actual state against the declaration after the run"


@pytest.mark.parametrize("host_networked", ["9100"])
def test_host_networked_services_are_scoped(host_networked: str) -> None:
    """A service with `network_mode: host` is where UFW really applies, and
    exactly that is why it must not be `Anywhere`."""
    rule = next(r for r in rules() if host_networked in r["spec"])
    assert rule["spec"].startswith("allow from "), (
        f"'{rule['spec']}' is not restricted to a source network: for a "
        "host-networked service that means open to the internet"
    )
