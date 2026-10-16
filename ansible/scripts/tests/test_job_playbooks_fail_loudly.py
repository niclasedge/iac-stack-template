"""Every job playbook must end red when its payload crashed.

The shape all of these share: run a script with `register: result` +
`ignore_errors: yes`, then parse the output, write the structured result log,
ping Healthchecks. `ignore_errors` is what makes that tail run at all after a
crash, and it is also what leaves the recap at `failed=0`, so the run reports
success and any scheduler goes green. The result log says `level:error`, but
the run status lies.

The fix is a trailing `fail:` task, and this test is what keeps the next job
from being written without one. The property asserted is position, not mere
presence: the guard has to be the play's LAST task, because placed earlier it
skips the very logging and pinging `ignore_errors` was added to preserve. A
`fail:` somewhere in the middle (a pre-flight abort, say) does not count and
must not satisfy the test.

Findings are deliberately not covered here: these payload scripts call no
sys.exit, so a non-zero rc is an uncaught exception, never "a site is down".
A finding travels through the level:error result log to monitor-check.

Run:
    uvx --with pytest --with pyyaml pytest ansible/scripts/tests/test_job_playbooks_fail_loudly.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
JOBS_DIR = REPO / "ansible" / "playbooks" / "jobs"

PAYLOAD_MODULES = {
    "shell", "command", "script", "raw",
    "ansible.builtin.shell", "ansible.builtin.command",
    "ansible.builtin.script", "ansible.builtin.raw",
}
FAIL_MODULES = {"fail", "ansible.builtin.fail"}

# Exempt, with the reason, not a list of "not done yet". Empty today: add a
# playbook name here only if it has no single payload rc (a dozen independent
# best-effort steps, for example) AND the condition that matters is already a
# level:error result log.
EXEMPT: set[str] = set()


def playbooks() -> list[Path]:
    return sorted(p for p in JOBS_DIR.rglob("*.yml") if p.name not in EXEMPT)


def plays(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text())
    return [p for p in (doc or []) if isinstance(p, dict)]


def sections(play: dict) -> list[tuple[str, list]]:
    return [(k, play[k]) for k in ("pre_tasks", "tasks", "post_tasks")
            if isinstance(play.get(k), list)]


def all_tasks(play: dict) -> list[dict]:
    out = []
    for _, tasks in sections(play):
        out += [t for t in tasks if isinstance(t, dict)]
    return out


def module_of(task: dict) -> str | None:
    for key in task:
        if key in PAYLOAD_MODULES or key in FAIL_MODULES:
            return key
    return None


def has_ignored_payload(play: dict) -> bool:
    """A registered shell/command/script run under ignore_errors."""
    for task in all_tasks(play):
        if (module_of(task) in PAYLOAD_MODULES
                and task.get("register")
                and task.get("ignore_errors") in (True, "yes")):
            return True
    return False


def fail_tasks(play: dict) -> list[dict]:
    return [t for t in all_tasks(play) if module_of(t) in FAIL_MODULES]


IDS = [p.relative_to(JOBS_DIR).as_posix() for p in playbooks()]


def last_task(play: dict) -> tuple[str, dict | None]:
    section, tasks = sections(play)[-1]
    return section, next((t for t in reversed(tasks) if isinstance(t, dict)), None)


@pytest.mark.parametrize("path", playbooks(), ids=IDS)
def test_a_crashed_payload_makes_the_job_fail(path: Path):
    for play in plays(path):
        if not has_ignored_payload(play):
            continue
        section, last = last_task(play)
        assert last is not None and module_of(last) in FAIL_MODULES, (
            f"{path.name}: the payload runs under ignore_errors, so a crash "
            f"leaves the recap at failed=0 and the job reports success. End the "
            f"play with a `fail:` task guarded on the register's rc; "
            f"{section} currently ends on "
            f"'{(last or {}).get('name', '?')}'."
        )


@pytest.mark.parametrize("path", playbooks(), ids=IDS)
def test_the_guard_is_conditional(path: Path):
    """An unconditional `fail:` would make every run red, including good ones."""
    for play in plays(path):
        for guard in fail_tasks(play):
            assert guard.get("when"), (
                f"{path.name}: `{guard.get('name', 'fail')}` has no `when:`: "
                f"that fails the job on every run, successes included."
            )


def test_the_exempt_list_does_not_rot():
    for name in EXEMPT:
        matches = list(JOBS_DIR.rglob(name))
        assert matches, f"EXEMPT names {name}, which no longer exists"
