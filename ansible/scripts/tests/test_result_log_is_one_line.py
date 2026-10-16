"""A structured result log must be a single line, or Filebeat drops it.

Filebeat reads `{{ es_log_dir }}/*.log` with the `ndjson` parser, which is
line-based: one JSON object per line. A result log whose object spans two lines
is two invalid lines, so the document never reaches Elasticsearch — and with it
never reaches monitor-check or a GitHub issue. The job still goes red in
the scheduler, so the failure is not invisible; the *alert* is.

How the newline gets in: these tasks write the JSON as a YAML folded scalar
(`content: >`). Folding joins lines with a space, but it PRESERVES a newline
before a line that is more indented than the block. So splitting one long Jinja
expression across two lines for readability — the natural thing to do — puts a
literal \\n in the middle of the object.

Why this needs a test rather than care: the file still parses. `json.load()`
accepts newlines inside an object, so opening the log and reading `level` works
perfectly, which is exactly the check a person writes to verify their change.

The assertion is on the template, not on output: yaml.safe_load already performs
the folding, so a `\\n` in the loaded string is the defect itself — no rendering,
no Jinja evaluation needed.

Two things survive folding but never reach the file, and both had to be modelled
or this test reports defects that measurement disproves:

  - `{# ... #}` renders to nothing, and Ansible templates with `trim_blocks=True`,
    so the newline immediately after `#}` goes with it.
  - a newline *inside* `{{ ... }}` is part of the expression, not of the output.

What is left after removing those is the literal JSON text, and a newline there
is the defect. The typical defect is neither inside the expression nor inside a comment: it is
the break YAML keeps *after* a more-indented continuation line, landing between
`"level":"...",` and `"event":"result"`.

Run:
    uvx --with pytest --with pyyaml pytest \\
        ansible/scripts/tests/test_result_log_is_one_line.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
PLAYBOOKS = REPO / "ansible" / "playbooks"

COPY_MODULES = ("copy", "ansible.builtin.copy")

# `{# ... #}` renders to nothing, and Ansible sets trim_blocks, so the newline
# right after `#}` is dropped too. The same holds for `{% ... %}` block tags:
# that is what lets a `{% for %}` loop emit one record per line without leaving
# blank lines behind.
JINJA_COMMENT = re.compile(r"\{#.*?#\}\n?", re.S)
JINJA_BLOCK = re.compile(r"\{%.*?%\}\n?", re.S)
# The body of an expression is evaluated; its own line breaks are not output.
JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)


def rendered_shape(content: str) -> str:
    """The literal text that will reach the file, Jinja blanked out."""
    text = JINJA_COMMENT.sub("", content)
    text = JINJA_BLOCK.sub("", text)
    return JINJA_EXPR.sub("", text)


def playbooks() -> list[Path]:
    return sorted(PLAYBOOKS.rglob("*.yml"))


def is_result_log(dest: str, content: str) -> bool:
    """Is this copy task writing a structured result log, or just a file?

    The one-line rule comes from Filebeat's ndjson parser, so it applies to what
    Filebeat reads and to nothing else. A test that took EVERY inline `content:`
    write would fail on `runbooks/docker_log_rotation.yml`, a pretty-printed
    /etc/docker/daemon.json that no parser of ours ever sees, with a message
    about the alert chain. A test that fires on the wrong file teaches people to
    work around it.

    Two ways in, because either one alone has a hole:

      - `dest` under `es_log_dir`: the documented destination, which is what
        Filebeat actually tails.
      - a body that looks like a log record (`@timestamp`) — so a result log
        written to the WRONG place is still checked here rather than silently
        dropping out of this test along with its destination bug.
    """
    return "es_log_dir" in dest or "@timestamp" in content


def content_writes(path: Path) -> list[tuple[str, str]]:
    """(task name, content) for every copy task writing a structured result log."""
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # a malformed playbook is a different test's job
        pytest.fail(f"{path.relative_to(REPO)} does not parse: {exc}")
    out = []
    for play in doc or []:
        if not isinstance(play, dict):
            continue
        for section in ("pre_tasks", "tasks", "post_tasks"):
            for task in play.get(section) or []:
                if not isinstance(task, dict):
                    continue
                for key in COPY_MODULES:
                    args = task.get(key)
                    if not (isinstance(args, dict) and isinstance(args.get("content"), str)):
                        continue
                    if not is_result_log(str(args.get("dest", "")), args["content"]):
                        continue
                    out.append((task.get("name", "<unnamed>"), args["content"]))
    return out


def test_the_filter_still_sees_the_result_logs() -> None:
    """The guard against a narrowing that quietly disables this test.

    A filter that matches nothing makes every parametrized case pass: the test
    would stay green while checking air. So assert the population directly: the
    check playbooks, the job playbook and the ingest probe each write a result
    log.
    """
    found = {p.name: content_writes(p) for p in playbooks()}
    total = sum(len(v) for v in found.values())
    assert total >= 6, (
        f"only {total} result-log writes found across {len(found)} playbooks — "
        "is_result_log() is too strict and this test is checking nothing."
    )


def test_the_filter_separates_a_log_from_a_config_file() -> None:
    log = '{"@timestamp":"2026-01-01T00:00:00Z","service":"x","level":"info","event":"result"}'
    assert is_result_log("{{ es_log_dir }}/result_x.log", log)
    # Same body, wrong destination: still checked, because the one-line defect
    # and the destination defect are independent bugs.
    assert is_result_log("/tmp/somewhere.log", log)
    # A pretty-printed daemon.json is not a log record and no parser reads it.
    assert not is_result_log(
        "/etc/docker/daemon.json",
        '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m"}\n}',
    )


@pytest.mark.parametrize("path", playbooks(), ids=lambda p: p.name)
def test_every_line_is_a_whole_json_object(path: Path) -> None:
    """One record per line — not necessarily one record per file.

    A `{% for %}` loop of per-item metrics is many objects and correct. The
    invariant Filebeat needs is not "one line" but "no object split across
    lines", so the check is per line: it must open with `{` and close with `}`.
    """
    for name, content in content_writes(path):
        for n, line in enumerate(rendered_shape(content).splitlines(), 1):
            body = line.strip()
            if not body:
                continue
            assert body.startswith("{") and body.endswith("}"), (
                f"{path.relative_to(REPO)} -> task {name!r}, line {n} is not a "
                f"complete JSON object. Filebeat's ndjson parser reads one object "
                f"per line and drops both halves of a split one: the job goes red "
                f"in the scheduler and the alert chain never hears about it. Move the "
                f"multi-line Jinja into a `set_fact` and reference the variable.\n"
                f"  line: {body}"
            )


@pytest.mark.parametrize("path", playbooks(), ids=lambda p: p.name)
def test_the_last_line_is_terminated(path: Path) -> None:
    """A result log must end with a newline, or Filebeat never sends its last line.

    `filestream` is line-based on the way IN as well as on the way out: it
    treats the newline as the end of a record and holds back anything after the
    last one, waiting for the line to be completed. A file whose final byte is
    `}` therefore has its only record sit in the reader forever.

    Nothing about this failure is visible from either end. Filebeat opens the
    file and logs a reader for it, Elasticsearch is never asked to index
    anything so it reports no error, and the play that wrote the file is green.
    `od -c` on such a file shows it ending on `}`.

    In YAML this is one character. `content: >` (folded, *clip*) keeps the
    trailing newline; `content: >-` (folded, *strip*) removes it. Both render
    identical-looking JSON, which is why this needs a test rather than care.
    """
    for name, content in content_writes(path):
        assert content.endswith("\n"), (
            f"{path.relative_to(REPO)} -> task {name!r} writes a result log with "
            f"no trailing newline. Filebeat's filestream input waits for the end "
            f"of the line and never ships it: no error, no document, green play. "
            f"Use `content: >` instead of `content: >-`."
        )
