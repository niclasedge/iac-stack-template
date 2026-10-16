# Secrets: SOPS + age

All secrets live in one encrypted dotenv file, `secrets/app.env`, committed to
git. SOPS encrypts the **values** and leaves the **keys** readable, so a diff
shows which variable changed without revealing the new value. The private half
of the encryption key (an [age](https://github.com/FiloSottile/age) key) never
enters the repository.

`secrets/example.env` documents the keys with placeholder values. It is the only
plaintext file allowed under `secrets/`.

## First-time setup (one machine)

```bash
# 1. Install the tools (macOS shown; any package manager works)
brew install sops age just uv

# 2. Create an age key. The public key is printed to the terminal.
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt
#   Public key: age1qq...    <- copy this

# 3. Put the public key into .sops.yaml (replace the placeholder recipient)

# 4. Create the real secrets file from the example and encrypt it in place
cp secrets/example.env secrets/app.env
sops --encrypt --in-place secrets/app.env

# 5. Edit values (sops decrypts into $EDITOR and re-encrypts on save)
just secrets-edit

# 6. Check that this machine can decrypt (prints key counts, never values)
just secrets-check
```

Commit `.sops.yaml` and `secrets/app.env`. Never commit `keys.txt`.

## Using the secrets

Plain `just` has no secrets. Recipes that need them depend on a guard and fail
with a pointer to the wrapper:

```bash
./j deploy whoami        # == just deploy whoami, with secrets/app.env decrypted
```

`./j` runs `sops exec-env secrets/app.env just ...`: the values exist in the
environment of that one process tree and nowhere else. No plaintext file is
written, and nothing remains when the command exits. Ansible reads them through
`lookup('env', ...)`; the `sops_dotenv` lookup plugin reads a single key
directly for per-service `.env` files (`env_from_secrets` in `services.yml`).

## Adding a machine

1. On the new machine: `age-keygen -o ~/.config/sops/age/keys.txt` and note the
   public key. Generate the key **on that machine**; never copy a private key
   between hosts.
2. On a machine that can already decrypt: add the public key to `.sops.yaml`
   (under `keys:` and in the `creation_rules` group), then run
   `just secrets-updatekeys` and commit.
3. Until step 2 is committed, the new machine is locked out, and nothing says so.

## Finding the key

SOPS looks for the age key in a platform-specific place (on macOS:
`~/Library/Application Support/sops/age/keys.txt`, not `~/.config`). The
repository avoids the difference: `scripts/age-key.sh` prints the identity from
`$SOPS_AGE_KEY_FILE` or `~/.config/sops/age/keys.txt`, and `./j` and the
justfile point `SOPS_AGE_KEY_CMD` at it. Without that, the error reads "no
identity matched any of the recipients", which blames the recipient list for a
lookup-path problem.

## Rules that keep this safe

- Placeholders only in `secrets/example.env` and `.env.example`.
- Back up the private key (password manager). Losing every copy means losing
  every secret: the encrypted file has no other way in.
- `just secrets-view` prints to stdout; never redirect it into a file. The
  `.gitignore` blocks `*.decrypted` and `*.plaintext` for the case where someone
  does anyway.
- Run `gitleaks dir .` before pushing (`.gitleaks.toml` is included).
- Rotate any value that has ever been pasted into a chat, a ticket or a log.
