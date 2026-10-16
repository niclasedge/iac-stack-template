# =============================================================================
# iac-stack-template: just recipes
# =============================================================================
# Usage:
#   just                       # list all recipes
#   just test                  # unit tests (no hosts needed)
#   just validate              # services.yml against schema + contract rules
#   ./j deploy whoami          # deploy one service (needs the secrets)
#
# Recipes are grouped with [group(...)] and labelled with [doc(...)] so that
# `just --list` reads as a menu: run, check, release, setup.
#
# Tools: just, uv, ansible, docker, sops + age. Terraform is optional.

# Secrets come from `secrets/app.env` (SOPS + age), decrypted into the process
# environment by the `./j` wrapper, NOT from a plaintext `.env` on disk.
#
# Do not flip this to true. `set dotenv-load` needs a readable plaintext file,
# which is exactly what this repo must never contain; leaving it on would make
# a stray `.env` shadow the encrypted source. The inventory and the playbooks
# resolve hosts and credentials through lookup('env', ...); they inherit them
# from `./j`.
set dotenv-load := false

# Ansible observability: use the repo ansible.cfg + es_logger callback (traces)
# regardless of the current working directory.
export ANSIBLE_CONFIG := justfile_directory() / "ansible" / "ansible.cfg"
export ANSIBLE_CALLBACK_PLUGINS := justfile_directory() / "ansible" / "callback_plugins"
export ANSIBLE_CALLBACKS_ENABLED := "es_logger"
export IAC_LOG_DIR := justfile_directory() / "monitoring" / "logs"

# SOPS does not find the age key on every platform on its own: on macOS its
# default path is ~/Library/Application Support/sops/age/keys.txt, not
# ~/.config. scripts/age-key.sh is the single place that knows the difference.
# The default is unconditional on purpose: an empty SOPS_AGE_KEY_CMD makes sops
# panic, unlike an empty _FILE, which is merely skipped.
export SOPS_AGE_KEY_CMD := env('SOPS_AGE_KEY_CMD', justfile_directory() / "scripts/age-key.sh")

# The real inventory (gitignored) wins; the committed example is the fallback,
# which is what lets a fresh clone run `just test` and the syntax checks.
inventory := if path_exists(justfile_directory() / "ansible/inventory/inventory.yml") == "true" { "ansible/inventory/inventory.yml" } else { "ansible/inventory/inventory.example.yml" }

# The first recipe is what a bare `just` runs, so this has to stay at the top.
[private]
default:
    @just --list --unsorted

# Guard for recipes that need the decrypted secrets, i.e. that must run through
# ./j. Without it the failure is silent and misleading: ansible's
# lookup('env', ...) returns an empty string, the inventory falls back to its
# defaults, and a play meant for the VPS either reports "unreachable" or, worse,
# runs against this machine. IAC_SECRETS is exported by ./j only after sops has
# actually decrypted, so this checks delivery, not intent.
[private]
_secrets:
    #!/usr/bin/env bash
    if [ -z "${IAC_SECRETS:-}" ]; then
        echo "error: this recipe needs the repo secrets, and plain \`just\` has none." >&2
        echo "  Run it through the wrapper instead:  ./j <recipe> [args]" >&2
        echo "  (decrypts secrets/app.env into this process only, see docs/secrets.md)" >&2
        exit 1
    fi

# -- run ----------------------------------------------------------------------
#
# Start something, open something, look at output. Nothing here is a gate; the
# things that answer "is it still correct" live under [check].

# Passes any docker compose subcommand through. No argument means `ps`: the
# question you ask most often is "is it up", and it changes nothing.
#
# This drives a LOCAL stack on this machine (development convenience). The
# production stack on the log host is deployed by `just monitoring-stack`.
[group('run')]
[doc('Drive the LOCAL Elastic Stack: just monitoring [up -d|down|logs -f|ps]')]
monitoring *ARGS:
    cd monitoring && docker compose {{ if ARGS == "" { "ps" } else { ARGS } }}

# What is live locally is ansible.log, the es_logger callback's own output for
# runs started on this machine. For anything about jobs, hosts or heartbeats,
# ask Elasticsearch (Kibana): the structured result logs are written on the
# log host (es_log_host), not here.
[group('run')]
[doc('Tail the local ansible callback log (job/host truth lives in Kibana)')]
logs:
    @tail -f monitoring/logs/ansible.log 2>/dev/null || echo "monitoring/logs/ansible.log does not exist yet; it appears on the first local ansible run."

# Run one job by its id from monitoring/config/jobs.json: just job http_check
#
# It resolves the playbook and the job's Healthchecks uuid and then `exec`s, so
# cron sees ansible-playbook's own exit status with nothing in between. That
# `exec` is the point: a wrapper that reads the exit code itself would see 0
# after a crash (job payloads run under `ignore_errors`) and report success.
# The ping stays with the playbook for the same reason.
[group('run')]
[doc('Run one job from monitoring/config/jobs.json (no id lists the choices)')]
job ID="": _secrets
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ justfile_directory() }}"
    # cron hands out PATH=/usr/bin:/bin, where neither `uv` nor
    # `ansible-playbook` lives. Verify with `env -i PATH=/usr/bin:/bin HOME=$HOME`,
    # never in an interactive shell.
    export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
    JOB_ID="{{ ID }}"
    if [ -z "$JOB_ID" ]; then
        # Exit 1 with the list, never 0: `until just job "$EMPTY"` would
        # otherwise report instant success without running anything.
        uv run python3 -c 'import json; print("usage: just job <id>\navailable: " + ", ".join(sorted(j["id"] for j in json.load(open("monitoring/config/jobs.json"))["jobs"])))' >&2
        exit 1
    fi
    PLAYBOOK="$(uv run python3 -c 'import json,sys; b={j["id"]: j for j in json.load(open("monitoring/config/jobs.json"))["jobs"]}; sys.exit("unknown job id: " + sys.argv[1] + "\navailable: " + ", ".join(sorted(b))) if sys.argv[1] not in b else print(b[sys.argv[1]]["playbook"])' "$JOB_ID")"
    HC_UUID="$(uv run python3 -c 'import json,os,sys; p="monitoring/config/healthcheck_uuids.json"; print(json.load(open(p)).get(sys.argv[1], "") if os.path.exists(p) else "")' "$JOB_ID")"
    # IAC_RUN_ID becomes the trace_id, IAC_JOB_ID the service, so a task span
    # still correlates with its job run in ES.
    export IAC_JOB_ID="$JOB_ID"
    export IAC_RUN_ID="$(uuidgen | tr 'A-Z' 'a-z' | tr -d '-' | cut -c1-16)"
    exec ansible-playbook -i {{ inventory }} "ansible/playbooks/$PLAYBOOK" -e "hc_uuid=$HC_UUID"

[group('run')]
[doc('List every Ansible playbook under ansible/playbooks/')]
ansible-list:
    @find ansible/playbooks -name '*.yml' | sort

[group('run')]
[doc('Errors + missing heartbeats -> GitHub Issues. Flags: --dry-run, --watch')]
monitor-check *ARGS:
    uv run monitoring/scripts/monitor-check.py {{ ARGS }}

# Exercise the alert chain end to end: log -> Filebeat -> ES -> monitor-check ->
# GitHub Issue.
#
# Under [run] and not [check] on purpose: it writes a real error into the live
# chain and files a real GitHub Issue. A side effect like that must not sit in
# the column a human skims for "what is safe to click".
#
# EVENT is an argument with a non-ignored default, not a constant: the event
# `test_error` is on monitor-check's IGNORED_ERROR_PATTERNS, so a probe using it
# ends in ES and never produces an issue, testing everything except the part
# that matters.
[group('run')]
[doc('Write a real error into the alert chain and expect a GitHub Issue')]
test-chain EVENT="alert_chain_probe":
    EVENT={{ EVENT }} ./monitoring/scripts/test-error.sh

[group('run')]
[doc("Write a heartbeat sample: the other half of the dead man's switch")]
test-heartbeat:
    ./monitoring/scripts/example-heartbeat.sh

# -- check --------------------------------------------------------------------
#
# Everything here answers "is it still correct" and changes nothing that a
# rerun would not change again. `test`, `lint` and `validate` are the three a
# commit needs.

# `uvx`, not `uv run --with`: with a venv active, uv run picks that venv's
# python and then cannot import pytest at all. ansible-core is needed because
# the callback and lookup plugin tests import ansible. If a script gains a
# dependency, this fails loudly with ModuleNotFoundError naming it, and it
# belongs here too.
[group('check')]
[doc('Python unit tests: ansible/, monitoring/, scripts/ (must stay green)')]
test:
    uvx --with pytest --with ansible-core --with 'pyyaml>=6.0' --with 'jinja2>=3.1' \
        --with 'jsonschema>=4.0' --with 'requests>=2.31.0' \
        pytest ansible/scripts/tests/ monitoring/scripts/tests/ scripts/tests/ -q

# --with ansible bundles the collections, so syntax-check can resolve their
# modules without hardcoding a venv path.
[group('check')]
[doc('ansible-lint gate over ansible/ (skip_list + reasons in .ansible-lint)')]
lint:
    uvx --with ansible ansible-lint --offline ansible/

[group('check')]
[doc('Syntax-check every playbook against the example inventory')]
syntax:
    #!/usr/bin/env bash
    set -euo pipefail
    export ANSIBLE_ROLES_PATH="{{ justfile_directory() }}/ansible/roles"
    cd ansible
    for f in $(find playbooks -name '*.yml' | sort); do
        echo "== $f"
        ansible-playbook -i inventory/inventory.example.yml --syntax-check "$f" -e service=whoami >/dev/null
    done

[group('check')]
[doc('Validate services.yml against services.schema.json + the contract rules')]
validate:
    ./scripts/validate_services.py

# Stays its own recipe: two commands with an `&&`, not one subcommand, and it is
# the one a CI gate calls. Needs the terraform binary.
[group('check')]
[doc('Terraform fmt -check + validate (the gate, not a subcommand)')]
tf-check:
    cd terraform && terraform fmt -check && terraform validate

[group('check')]
[doc('Table of every service: host, state, deploy pattern, domain, health')]
status *ARGS:
    ./scripts/services.py status {{ ARGS }}

[group('check')]
[doc('Ping every host in the inventory (needs ./j)')]
ansible-ping: _secrets
    ansible all -i {{ inventory }} -m ping

[group('check')]
[doc('Connectivity-only playbook against every host (needs ./j)')]
ansible-connectivity: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/checks/test_connectivity.yml

[group('check')]
[doc('Run all check playbooks (needs ./j)')]
ansible-checks: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/checks/run_all_checks.yml

# Can this machine decrypt the secrets? Prints the key count, never a value.
[group('check')]
[doc('Can this machine decrypt the secrets file? Key count, never a value')]
secrets-check:
    #!/usr/bin/env bash
    set -euo pipefail
    for f in secrets/*.env; do
        case "$f" in secrets/example.env) continue ;; esac
        n=$(sops -d "$f" | grep -cE '^[A-Za-z_][A-Za-z0-9_]*=')
        echo "ok $f: $n keys"
    done

# -- release ------------------------------------------------------------------
#
# Everything that changes a host. All of it needs the secrets, so all of it
# runs through `./j`; the `_secrets` dependency says so at the prompt.

[group('release')]
[doc('Deploy one service from services.yml to its host: just deploy <name>')]
deploy name: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/deploy.yml -e service={{name}}

[group('release')]
[doc('Dry-run the same deploy: changes nothing, prints the diff')]
deploy-check name: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/deploy.yml -e service={{name}} --check --diff

# Same deploy, but recreate the containers even when nothing in the compose file
# changed: just deploy-force <name>
#
# A separate recipe rather than an argument on `deploy`, because just takes
# arguments POSITIONALLY: `just deploy whoami force=true` would pass the literal
# string "force=true", Ansible would receive sd_force_recreate=force=true, `| bool`
# would turn that into false, and the run would report success while recreating
# nothing. An interface that fails silently on a plausible spelling is worse
# than one flag more.
#
# When it is needed: the desired state lives in the DAEMON, not in the compose
# file. Log rotation is the case: LogConfig is fixed at container create, so
# `up -d` sees nothing to do and the container keeps growing its log.
[group('release')]
[doc('Deploy and recreate the containers even when the compose file is unchanged')]
deploy-force name: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/deploy.yml -e service={{name}} -e sd_force_recreate=true

[group('release')]
[doc('Converge the VPS firewall against ufw_rules, dry run (prints the diff)')]
firewall-check: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/runbooks/firewall.yml --check

[group('release')]
[doc('Converge the VPS firewall against ufw_rules (adds first, then deletes)')]
firewall: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/runbooks/firewall.yml

[group('release')]
[doc('Docker log rotation on the VPS, dry run')]
docker-log-rotation-check: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/runbooks/docker_log_rotation.yml --check

# restart_docker stops every container for the length of the daemon restart;
# pass it deliberately: just docker-log-rotation -e restart_docker=true
[group('release')]
[doc('Docker log rotation on the VPS (extra args pass through, e.g. -e restart_docker=true)')]
docker-log-rotation *ARGS: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/runbooks/docker_log_rotation.yml {{ ARGS }}

[group('release')]
[doc('Deploy the Elastic stack on the log host (add --check for a dry run)')]
monitoring-stack *ARGS: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/runbooks/monitoring_stack.yml {{ ARGS }}

[group('release')]
[doc('Schedule monitor-check in cron on the log host (needs IAC_REPO_URL)')]
monitor-check-cron *ARGS: _secrets
    ansible-playbook -i {{ inventory }} ansible/playbooks/runbooks/monitor_check_cron.yml {{ ARGS }}

# Passthrough, like `monitoring`: init/plan/apply are subcommands of the same
# binary in the same directory.
[group('release')]
[doc('Run terraform in terraform/: just tf [init|plan|apply|show ...]')]
tf *ARGS:
    cd terraform && terraform {{ if ARGS == "" { "version" } else { ARGS } }}

# -- setup --------------------------------------------------------------------

# Bootstrap a fresh clone. Secrets: the encrypted `secrets/app.env` is created
# by you (copy secrets/example.env, encrypt it), and a new machine needs its own
# age private key to open it.
[group('setup')]
[doc('Bootstrap a fresh clone: tools, log dir, and the age key checklist')]
init:
    uv tool install --force ansible-core
    mkdir -p monitoring/logs
    @echo ""
    @echo "Secrets: secrets/app.env is encrypted (SOPS + age) and not part of this template."
    @echo "  1. Create your age key:  age-keygen -o ~/.config/sops/age/keys.txt"
    @echo "  2. Put its public key into .sops.yaml"
    @echo "  3. cp secrets/example.env secrets/app.env && just secrets-edit  # fill in values, sops encrypts on save"
    @echo "  4. Verify:  just secrets-check"
    @echo "  Then use ./j instead of just for recipes that need secrets (docs/secrets.md)"

# -- Secrets (SOPS + age) -----------------------------------------------------
#
# secrets/app.env is the CONTROLLER's environment: ./j pours all of it into every
# command through `sops exec-env`. secrets/example.env documents the keys with
# placeholder values and is the only plaintext file allowed under secrets/.

[group('setup')]
[doc('Edit encrypted secrets in $EDITOR (sops re-encrypts on save)')]
secrets-edit FILE="secrets/app.env":
    sops {{ FILE }}

# Print decrypted secrets, for eyeballing. Never redirect this to a file.
[group('setup')]
[doc('Print decrypted secrets for eyeballing. Never redirect this to a file')]
secrets-view FILE="secrets/app.env":
    @sops -d {{ FILE }}

[group('setup')]
[doc('Which keys exist, without decrypting anything')]
secrets-keys FILE="secrets/app.env":
    @grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' {{ FILE }} | tr -d '=' | grep -v '^sops_'

# Re-encrypt every secrets file to the recipient set currently in .sops.yaml:
# run after adding a machine's age public key there, then commit.
[group('setup')]
[doc('Re-encrypt every secrets file to the recipients in .sops.yaml')]
secrets-updatekeys:
    #!/usr/bin/env bash
    set -euo pipefail
    for f in secrets/*.env; do
        case "$f" in secrets/example.env) continue ;; esac
        sops updatekeys --yes "$f"
        echo "ok $f"
    done
