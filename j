#!/usr/bin/env bash
# ./j — run a just recipe with the repo's secrets in its environment.
#
#     ./j deploy miniflux      ==  just deploy miniflux, plus secrets/app.env
#
# The secrets are decrypted by SOPS into the environment of this one process
# tree and nowhere else: no plaintext file is written, and `just`, ansible and
# every python script below inherit them the same way they used to inherit
# `set dotenv-load := true`. When the process exits, they are gone.
#
# Recipes that need secrets depend on the private `_secrets` recipe, which
# checks the IAC_SECRETS marker exported below and fails with a pointer here.
# Recipes that do not (lint, test, monitoring-up, …) keep working under plain
# `just` — that is deliberate: a guard on everything would also block
# `just --list`, and a guard nobody can run around is a guard people disable.
set -euo pipefail

cd -- "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# cron hands out PATH=/usr/bin:/bin, where Homebrew's sops does not live: the
# same trap that made gh_binary() necessary in monitor-check.py. Verify any
# change here with `env -i PATH=/usr/bin:/bin HOME=$HOME ./j ...`, never in an
# interactive shell, where the bug is invisible.
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"

command -v sops >/dev/null || {
    echo "./j: sops not found; install sops and age first (see README)" >&2; exit 127; }

SECRETS="secrets/app.env"
[ -f "$SECRETS" ] || {
    echo "./j: $SECRETS missing; create it from secrets/example.env, see docs/secrets.md" >&2; exit 1; }

# SOPS does not find the age key on every platform by itself: on macOS its
# default path is ~/Library/Application Support/sops/age/keys.txt (Go's
# os.UserConfigDir), NOT ~/.config. scripts/age-key.sh answers that in one
# place and prints the identity; naming it here also makes cron and any
# non-login shell work, neither of which reads ~/.zshrc. The default is
# unconditional because an empty SOPS_AGE_KEY_CMD panics sops, see the
# script's header.
export SOPS_AGE_KEY_CMD="${SOPS_AGE_KEY_CMD:-$PWD/scripts/age-key.sh}"

# One sh -c string for sops exec-env, so every argument has to be re-quoted:
# without this, `./j job "nightly check"` arrives as two words (textual
# splitting of arguments that contain spaces).
cmd="just"
for a in "$@"; do
    cmd+=" '${a//\'/\'\\\'\'}'"
done

export IAC_SECRETS=1
exec sops exec-env "$SECRETS" "$cmd"
