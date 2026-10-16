"""`env_from_secrets: {VAR: '[<file>:]<KEY>'}`: the grammar and why it is checked.

The reference is author-written in services.yml and ends up in two places that
both punish a malformed value quietly:

  * `REPO_ROOT / <file>` in ansible/lookup_plugins/sops_dotenv.py. pathlib
    DISCARDS the left operand when the right one is absolute, so
    `REPO_ROOT / "/etc/passwd"` is `/etc/passwd`: a relative-looking join that
    is not confinement. Hence an allowlist of file names rather than a path
    pattern.
  * the rendered `.env` on the host. An empty key makes the lookup report
    "key '' not found", which reads like a missing secret rather than a typo in
    the map.

Splitting at the LAST colon is what makes the notation unambiguous: a dotenv key
can never contain one, so everything before it is the file, and a reference with
no colon at all means the default secrets file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_services as vs  # noqa: E402


class TestSplit:
    def test_a_bare_key_defaults_to_the_default_file(self):
        assert vs.split_secret_ref("MINIFLUX_DB_PASSWORD") == \
            ("secrets/app.env", "MINIFLUX_DB_PASSWORD")

    def test_an_explicit_file_is_honoured(self):
        assert vs.split_secret_ref("secrets/app.env:GITHUB_TOKEN") == \
            ("secrets/app.env", "GITHUB_TOKEN")

    def test_it_splits_at_the_last_colon(self):
        # Not split(':', 1): that would hand back "weird" as the file and
        # "path:KEY" as the key, and the error message would then name the
        # wrong half.
        assert vs.split_secret_ref("weird:path:KEY") == ("weird:path", "KEY")


class TestErrors:
    def _errs(self, mapping):
        return vs.env_from_secrets_errors("svc", {"env_from_secrets": mapping})

    def test_the_example_shape_is_accepted(self):
        assert self._errs({
            "POSTGRES_PASSWORD": "MINIFLUX_DB_PASSWORD",
            "TOKEN": "secrets/app.env:GITHUB_TOKEN",
        }) == []

    def test_no_mapping_at_all_is_fine(self):
        assert vs.env_from_secrets_errors("svc", {}) == []

    def test_an_empty_key_is_named(self):
        errs = self._errs({"X": "secrets/app.env:"})
        assert len(errs) == 1 and "has no key" in errs[0]

    def test_an_unknown_file_is_refused(self):
        errs = self._errs({"X": "secrets/other.env:KEY"})
        assert len(errs) == 1 and "secrets/other.env" in errs[0]

    @pytest.mark.parametrize("ref", [
        "/etc/passwd:KEY",           # absolute: pathlib would drop REPO_ROOT
        "secrets/../../etc/x:KEY",   # traversal
        ":KEY",                      # empty file
    ])
    def test_paths_that_escape_the_repo_are_refused(self, ref):
        assert self._errs({"X": ref}), f"{ref} must not validate"

    def test_a_key_that_is_not_a_dotenv_name_is_refused(self):
        errs = self._errs({"X": "secrets/app.env:not-a-valid name"})
        assert any("dotenv name" in e for e in errs)


class TestAgainstTheRealFile:
    """The shipped entries must keep validating: the regression guard for the
    schema pattern."""

    def test_services_yml_has_no_env_from_secrets_errors(self):
        data = vs.load_yaml(SCRIPTS_DIR.parent / "services.yml")
        defaults = data.get("defaults") or {}
        errors = []
        for name, raw in (data.get("services") or {}).items():
            errors += vs.env_from_secrets_errors(name, {**defaults, **(raw or {})})
        assert errors == []

    def test_every_referenced_file_is_allowed(self):
        data = vs.load_yaml(SCRIPTS_DIR.parent / "services.yml")
        for name, raw in (data.get("services") or {}).items():
            for var, ref in ((raw or {}).get("env_from_secrets") or {}).items():
                file, _ = vs.split_secret_ref(ref)
                assert file in vs.SECRET_FILES, f"{name}.{var} -> {file}"
