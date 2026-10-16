"""`changed` after a compose up must be measured, not read off the output.

A naive role decides it with
`sd_compose_up.stdout is search('Recreated|Started|Created|Pulled|Built')`.
That condition can never be true: docker compose writes its progress to stderr,
so a deploy reports `ok` while the container was in fact just recreated. The
recap cannot then distinguish "stack was already current" from "container
replaced", which is the first question anyone asks after a deploy.

The replacement compares the running container IDs before and after, because a
changed ID *is* what "recreated" means, whereas a marker word only describes it
and has moved between Compose versions.

These are structural tests against the role's YAML: they cost nothing, they run
without docker or a host, and they fail the moment someone reintroduces an
output-grep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "roles" / "service_deploy" / "tasks"
COMPOSE_UP = ROLE / "compose-up.yml"


def tasks(path: Path) -> list[dict]:
    return [t for t in (yaml.safe_load(path.read_text()) or []) if isinstance(t, dict)]


def changed_when(task: dict) -> str:
    return str(task.get("changed_when", ""))


def test_no_task_decides_changed_from_a_compose_up_output() -> None:
    """The regression itself: no `stdout`/`stderr` grep may drive `changed`."""
    for task in tasks(COMPOSE_UP):
        condition = changed_when(task)
        assert "stdout is search" not in condition, (
            f"'{task.get('name')}' is back to grepping output for `changed`. "
            "docker compose writes progress to stderr, so the condition is "
            "structurally false: compare container IDs instead."
        )
        # stderr would "work" today and still be the wrong shape: it depends on
        # wording that changes between Compose versions.
        assert "stderr is search" not in condition, (
            f"'{task.get('name')}' keys `changed` on stderr wording. The marker "
            "words are not API: compare container IDs."
        )


def test_the_up_task_never_reports_changed_by_itself() -> None:
    up = [t for t in tasks(COMPOSE_UP) if "up -d" in str(t.get("ansible.builtin.command", ""))]
    assert len(up) == 1, "expected exactly one `docker compose up` task"
    assert changed_when(up[0]) == "False", (
        "the up task must not claim `changed` on its own: its own output cannot "
        "tell whether anything moved."
    )


def test_force_recreate_is_opt_in() -> None:
    """`--force-recreate` must hang on a flag, never be unconditional.

    It exists because `up -d` compares desired config against the running
    container and does nothing when they match: correct for a deploy, useless
    when a DAEMON default changed (log rotation is fixed at container create).
    Unconditionally it would restart every container of every service on every
    deploy, which is the opposite of what a deploy should do.
    """
    up = [t for t in tasks(COMPOSE_UP) if "up -d" in str(t.get("ansible.builtin.command", ""))]
    cmd = str(up[0]["ansible.builtin.command"]["cmd"])
    assert "--force-recreate" in cmd, "the opt-in switch is missing entirely"
    assert "sd_force_recreate" in cmd, (
        "--force-recreate must be guarded by sd_force_recreate, not applied always"
    )
    # The guard has to be a real conditional, not a bare interpolation that would
    # render the flag for any truthy-looking string.
    assert "if sd_force_recreate | bool" in cmd


def test_force_recreate_defaults_to_false() -> None:
    defaults = yaml.safe_load((ROLE.parent / "defaults" / "main.yml").read_text())
    assert defaults.get("sd_force_recreate") is False, (
        "a deploy must not recreate containers unless asked to"
    )


def test_the_ids_are_sampled_before_and_after() -> None:
    ps_tasks = [
        t for t in tasks(COMPOSE_UP)
        if "ps -q" in str(t.get("ansible.builtin.command", ""))
    ]
    registers = [t.get("register") for t in ps_tasks]
    assert "sd_ps_before" in registers and "sd_ps_after" in registers, (
        f"the compose-up step needs a `ps -q` on both sides of the up, found: {registers}"
    )
    # Sampling must not itself count as a change, or every deploy is `changed`.
    for task in ps_tasks:
        assert changed_when(task) == "False", f"{task.get('name')} must be changed_when: false"


def test_exactly_one_task_compares_the_two_samples() -> None:
    comparing = [
        t for t in tasks(COMPOSE_UP)
        if "sd_ids_before" in changed_when(t) and "sd_ids_after" in changed_when(t)
    ]
    assert len(comparing) == 1, (
        "exactly one task should turn the two samples into `changed`; "
        f"found {len(comparing)}"
    )
    task = comparing[0]
    ids = task.get("vars", {})
    # Sorted, because `ps -q` gives no ordering guarantee and an order flip would
    # otherwise report a change that did not happen.
    assert "sort" in str(ids.get("sd_ids_before")), "compare sorted ID lists"
    assert "sort" in str(ids.get("sd_ids_after")), "compare sorted ID lists"


@pytest.mark.parametrize(
    "before,after,expected_change",
    [
        (["a", "b"], ["a", "b"], False),        # redeploy, nothing moved
        (["a"], ["b"], True),                   # recreated: new ID
        ([], ["a"], True),                      # stack did not exist yet
        (["a", "b"], ["a"], True),              # --remove-orphans took one away
        (["a", "b"], ["b", "a"], False),        # same set, different order
    ],
)
def test_the_comparison_semantics(before, after, expected_change) -> None:
    """The five cases the ID comparison has to get right.

    Mirrors the Jinja in the role (`sorted lists, !=`) so the intent is pinned
    even though the role itself is evaluated by Ansible, not here.
    """
    assert (sorted(before) != sorted(after)) is expected_change
