"""A rollback must not leave a service worse off than it was.

A rollback that begins with `docker compose down --remove-orphans`,
unconditionally and before it has even looked for a snapshot, turns a failed
deploy into an outage: the deploy fails, the rollback stops the whole stack, and
the `up -d` that follows may fail as well. The report says so correctly, but by
then it has already happened.

The better order needs no `down`:

  - `up -d` after the restore replaces the containers whose config the failed
    deploy changed anyway.
  - `--remove-orphans` removes whatever the deploy additionally started anyway.
  - and if the `up` fails, the OLD containers are usually still running because
    they were never stopped. That is the difference between a bad deploy and an
    outage.

Without a snapshot there is nothing to restore (first deploy into an empty
directory). Stopping anything then would not be a rollback but a second failure,
so the file does nothing in that case.

The tests are structural: they need neither docker nor a host, and they trip as
soon as someone brings a `down` back or flips the order. A real failing deploy
as verification would be a production outage on trial; that is why this file
exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "roles" / "service_deploy" / "tasks"
ROLLBACK = ROLE / "rollback.yml"


def tasks() -> list[dict]:
    return [t for t in (yaml.safe_load(ROLLBACK.read_text()) or []) if isinstance(t, dict)]


def command_of(task: dict) -> str:
    spec = task.get("ansible.builtin.command") or task.get("command") or {}
    if isinstance(spec, dict):
        return str(spec.get("cmd", "") or spec.get("argv", ""))
    return str(spec)


def test_the_rollback_never_runs_compose_down() -> None:
    """The regression that causes the outage."""
    for task in tasks():
        cmd = command_of(task)
        assert "compose" not in cmd or " down" not in cmd, (
            f"'{task.get('name')}' brings `compose down` back. A rollback that "
            "stops first and must start afterwards turns a failed deploy into "
            "an outage. `up -d --remove-orphans` after the restore does both "
            "without the gap."
        )


def test_the_first_action_is_looking_for_a_snapshot() -> None:
    """Look at what can be restored first, then act."""
    first = tasks()[0]
    assert "stat" in str(first).lower() and "rollback" in str(first), (
        f"first task is '{first.get('name')}', expected the snapshot check"
    )


def test_nothing_touches_the_service_without_a_snapshot() -> None:
    """Without a snapshot there is nothing to restore, so nothing to do.

    Every task that changes the target state (restore, up) must hang on
    `sd_snapshot.stat.exists`. Otherwise the rollback intervenes in a service
    for which it knows no previous state.
    """
    for task in tasks():
        cmd = command_of(task)
        if not cmd:
            continue
        touches_service = "rsync" in cmd or "up -d" in cmd
        if not touches_service:
            continue
        when = str(task.get("when", ""))
        assert "sd_snapshot.stat.exists" in when, (
            f"'{task.get('name')}' changes the service but does not depend on "
            "an existing snapshot"
        )


def test_the_restart_removes_orphans() -> None:
    """This is the replacement for the dropped `down`: containers the failed
    deploy additionally started must disappear."""
    ups = [t for t in tasks() if "up -d" in command_of(t)]
    assert len(ups) == 1, f"expected exactly one `up -d` task, found {len(ups)}"
    assert "--remove-orphans" in command_of(ups[0])


def test_a_command_that_never_ran_is_not_reported_as_changed() -> None:
    """`rc | default(1)`, not `changed_when: true`.

    A `command` whose `chdir` does not exist is NOT skipped in check mode: the
    action plugin aborts earlier, `failed_when: false` swallows it, and a hard
    `changed_when: true` would then report `changed` for something that never
    ran.
    """
    for task in tasks():
        if "up -d" not in command_of(task):
            continue
        assert "rc | default(1)" in str(task.get("changed_when", "")), (
            "the restart task must evaluate its real rc"
        )


def test_it_still_fails_loudly() -> None:
    """A rollback is not a success. The last task must turn the run red, or
    `ignore_errors` in a job playbook swallows the failure completely."""
    last = tasks()[-1]
    # `fail` or `ansible.builtin.fail`: the module name counts, not the spelling.
    fail_key = next((k for k in last if k.split(".")[-1] == "fail"), None)
    assert fail_key, f"last task is '{last.get('name')}', expected `fail:`"
    msg = str(last[fail_key].get("msg", ""))
    assert "sd_rollback_note" in msg, "the failure message must carry the report"


@pytest.mark.parametrize("case", ["check-mode", "no snapshot", "restore ok", "up failed"])
def test_the_report_distinguishes_all_four_outcomes(case: str) -> None:
    """The report must describe what happened, not what was attempted.

    A report nobody can trust is worse than none: under `--check` nothing was
    touched, without a snapshot nothing is stopped on purpose, and a failed `up`
    must not pass as recovery.
    """
    note = next(
        (str(t) for t in tasks() if "sd_rollback_note" in str(t.get("ansible.builtin.set_fact", t.get("set_fact", "")))),
        "",
    )
    markers = {
        "check-mode": "ansible_check_mode",
        "no snapshot": "nothing to roll back to",
        "restore ok": "up again",
        "up failed": "FAILED",
    }
    assert markers[case] in note, f"the report does not distinguish '{case}'"
