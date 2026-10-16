#!/usr/bin/env bash
# Print this machine's age identity on stdout. Nothing else goes to stdout.
#
# SOPS reads it through SOPS_AGE_KEY_CMD, and ./j, the justfile and
# scripts/iac_secrets.py all point here, so "where does the private key live"
# is answered in ONE place instead of three that drift apart.
#
# Why this exists: SOPS does not look in ~/.config/sops/age/keys.txt on every
# platform. On macOS its default is ~/Library/Application Support/sops/age/
# keys.txt (Go's os.UserConfigDir), so the conventional path is missed and the
# error reads "no identity matched any of the recipients": a complaint about
# the recipient list for a fault in the lookup path.
#
# Lookup order:
#   1. $SOPS_AGE_KEY_FILE, if it names a readable file
#   2. ~/.config/sops/age/keys.txt
#
# It has to be a *command*, and that command must never be the empty string:
# an empty SOPS_AGE_KEY_CMD makes sops panic (slice bounds out of range in
# age.getOutputFromCmd), unlike an empty SOPS_AGE_KEY_FILE, which is merely
# skipped. So every call site defaults to this script unconditionally rather
# than to "" when it finds nothing.
#
# Under cron PATH is /usr/bin:/bin; nothing here needs more than `cat`.
# Verify a change with:  env -i PATH=/usr/bin:/bin HOME=$HOME scripts/age-key.sh >/dev/null
set -euo pipefail

key_file="${SOPS_AGE_KEY_FILE:-}"
default_file="$HOME/.config/sops/age/keys.txt"

if [ -n "$key_file" ] && [ -r "$key_file" ]; then
    exec cat -- "$key_file"
fi

if [ -r "$default_file" ]; then
    exec cat -- "$default_file"
fi

echo "age-key.sh: no age identity on this machine. Looked for:" >&2
[ -n "$key_file" ] && echo "  $key_file   (SOPS_AGE_KEY_FILE)" >&2
echo "  $default_file" >&2
echo "docs/secrets.md has the setup; the public half must also be listed" >&2
echo "in .sops.yaml, otherwise the key opens nothing." >&2
exit 1
