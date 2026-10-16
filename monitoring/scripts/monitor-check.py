#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Monitor Check — central alert loop, driven by Elasticsearch.

Runs from cron (every 5 minutes) and checks:
  1. Errors: `level:error` in the last N minutes, one GitHub Issue per service.
  2. Heartbeats: a missing `event:heartbeat` per expected service (dead man's
     switch), one GitHub Issue per service.

No spam: a state file remembers which alert already has an open issue, and an
issue is closed again only after RESOLVE_AFTER_HOURS without a recurrence.

Usage:
    uv run monitoring/scripts/monitor-check.py              # single check
    uv run monitoring/scripts/monitor-check.py --watch      # loop (30 s)
    uv run monitoring/scripts/monitor-check.py --dry-run    # print only

Environment (from secrets/app.env, see .env.example):
    GITHUB_TOKEN (or GH_TOKEN, or a `gh auth login`), GITHUB_OWNER, GITHUB_REPO,
    optionally ES_URL (default http://localhost:9200) and KIBANA_URL.

Timeline:
  [T-5min]  error check: level:error in the last 5 minutes
  [T-Xmin]  heartbeat check: X = heartbeat interval + grace (hourly = 65 min)
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent

sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from iac_secrets import load_env  # noqa: E402  (needs PROJECT_DIR resolved first)

STATE_FILE = SCRIPT_DIR / "monitor-check.state"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
KIBANA_URL = os.environ.get("KIBANA_URL", "http://localhost:5601")

ERROR_WINDOW_MINUTES = 5            # how far back the error check looks
CHECK_INTERVAL_SECONDS = 30         # Watch-Mode polling

# How long a service must stay error-free before its issue is closed.
# Do not set this to less than a few hours: the error window is 5 minutes, so
# an hourly job drops out of the query minutes after it failed. Closing
# immediately would open and close issues every few minutes without anybody
# having fixed anything.
RESOLVE_AFTER_HOURS = 24

# Auto-close touches ONLY issues carrying this label. Everything else in the
# repo (planning tickets, hand-written bugs) is left alone, even if an id in
# the state file should point at it.
AUTO_LABEL = "auto"

# Expected heartbeats: {service_name: max_age_in_minutes}.
# A service that logs a heartbeat every 60 minutes gets 70 minutes of grace.
# Empty by default: no service emits `event:heartbeat` out of the box. Scheduled
# job freshness is watched through Healthchecks pings in the job playbooks; add
# entries here for scripts that write heartbeat lines themselves (see
# monitoring/scripts/example-heartbeat.sh).
EXPECTED_HEARTBEATS = {}

# Events that are NOT treated as errors (level=error but known)
IGNORED_ERROR_PATTERNS = [
    "test_error",                # the alert-chain test script
]


# ─── Setup ──────────────────────────────────────────────────────────────────
# load_env() comes from scripts/iac_secrets.py (SOPS + age): setdefault,
# tolerant of a missing file, but loud when the file is there and cannot be
# opened.


def gh_binary() -> str | None:
    """Absolute path to the gh CLI, or None if it is not installed.

    cron runs with a minimal PATH (/usr/bin:/bin), and Homebrew's gh is not in
    it, so the bare "gh" that resolves fine in a shell raises FileNotFoundError
    in every scheduled run. That would turn the CLI fallback into a no-op
    exactly where it is the last line of defence.

    ~/.local/bin is in the list for hosts without a package manager. Finding gh
    is only half of it: the fallback also needs `gh auth login` to have been
    run on that host, so on a machine where it has not, GITHUB_TOKEN from
    secrets/app.env is the only way through.
    """
    return shutil.which(
        "gh",
        path=os.environ.get("PATH", "")
        + ":/opt/homebrew/bin:/usr/local/bin:"
        + str(Path.home() / ".local" / "bin"),
    )


def token_works(token: str) -> bool:
    """Does GitHub still accept this token? One GET /user, ~200 ms."""
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def resolve_github_auth():
    """Get a GitHub token from the environment or `gh auth token`, and verify it.

    A token that is *set but rejected* is worse than no token at all: a plain
    `if not token` guard sees a non-empty string, never consults
    `gh auth token`, and every request goes out with credentials GitHub answers
    401 to, so no issue is ever created while a perfectly valid gh login sits
    unused. Validating costs one GET per run and is the difference between the
    fallback existing and the fallback working.
    """
    token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    )
    owner = os.environ.get("GITHUB_OWNER") or os.environ.get("GH_OWNER", "")
    # Bare repository name; the owner is joined in below. No default: alerts
    # must never land in a repository nobody chose.
    repo = os.environ.get("GITHUB_REPO", "")

    if token and not token_works(token):
        print("  [WARN] GITHUB_TOKEN was rejected by GitHub — falling back to gh auth")
        token = ""

    if not token:
        gh = gh_binary()
        if gh:
            result = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=10
            )
            token = result.stdout.strip() if result.returncode == 0 else ""

    return token, owner, repo


def state_load() -> dict:
    """Load the state file.

    Format: {"issue_map": {alert_id: {"url": ..., "last_seen": iso8601}}}.
    Older files held only the URL as a string, next to an alert_ids list that
    never shrank; both are migrated on load.
    """
    state = {"issue_map": {}}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"issue_map": {}}

    state.pop("alert_ids", None)
    now = datetime.now(timezone.utc).isoformat()
    migrated = {}
    for alert_id, entry in (state.get("issue_map") or {}).items():
        if isinstance(entry, str):
            migrated[alert_id] = {"url": entry, "last_seen": now}
        elif isinstance(entry, dict) and entry.get("url"):
            entry.setdefault("last_seen", now)
            migrated[alert_id] = entry
    state["issue_map"] = migrated
    return state


def state_save(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── ES Queries ─────────────────────────────────────────────────────────────
def es_get(path: str) -> dict | None:
    try:
        req = urllib.request.Request(f"{ES_URL}/{path}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [WARN] ES: {e}")
        return None


def es_search(index: str, body: dict) -> dict | None:
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{ES_URL}/{index}/_search", data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [WARN] ES Search: {e}")
        return None


def find_recent_errors(window_minutes: int = ERROR_WINDOW_MINUTES) -> list[dict]:
    """Find level:error documents of the last N minutes, grouped by service."""
    result = es_search("logs-*", {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {
                        "gte": f"now-{window_minutes}m"
                    }}},
                    {"term": {"level.keyword": "error"}}
                ]
            }
        },
        "aggs": {
            "by_service": {
                "terms": {"field": "service.keyword", "size": 50},
                "aggs": {
                    "latest": {
                        "top_hits": {
                            "size": 1,
                            "sort": [{"@timestamp": "desc"}],
                            "_source": ["service", "message", "@timestamp", "event"]
                        }
                    }
                }
            }
        }
    })
    if not result:
        return []

    errors = []
    for bucket in result.get("aggregations", {}).get("by_service", {}).get("buckets", []):
        service = bucket["key"]
        count = bucket["doc_count"]
        latest = bucket.get("latest", {}).get("hits", {}).get("hits", [{}])[0].get("_source", {})
        event = latest.get("event", "")

        # Skip known test events
        if any(p in event for p in IGNORED_ERROR_PATTERNS):
            continue

        # Stable per-service key, deliberately NOT time-stamped. An id that
        # carries the check's own wall clock mints a fresh id every 5-minute
        # run for an unchanged problem and opens yet another issue. One open
        # issue per service is the invariant, and resolve_stale() closes it.
        alert_id = f"error-{service}"
        errors.append({
            "alert_id": alert_id,
            "service": service,
            "count": count,
            "message": latest.get("message", "")[:200],
            "timestamp": latest.get("@timestamp", ""),
            "event": event,
            "type": "error",
        })
    return errors


def find_missing_heartbeats() -> list[dict]:
    """Check that the expected heartbeats arrived in time."""
    missing = []

    for service, max_age_minutes in EXPECTED_HEARTBEATS.items():
        result = es_search("logs-*", {
            "size": 1,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"service.keyword": service}},
                        {"term": {"event.keyword": "heartbeat"}},
                        {"range": {"@timestamp": {
                            "gte": f"now-{max_age_minutes}m"
                        }}}
                    ]
                }
            },
            "sort": [{"@timestamp": "desc"}],
            "_source": ["@timestamp"]
        })
        hits = result.get("hits", {}).get("hits", []) if result else []
        if not hits:
            alert_id = f"heartbeat-{service}"   # stable key, see find_recent_errors
            missing.append({
                "alert_id": alert_id,
                "service": service,
                "max_age_minutes": max_age_minutes,
                "type": "missing_heartbeat",
            })
    return missing


# ─── GitHub Issue Creation ──────────────────────────────────────────────────
def create_issue(repo: str, title: str, body: str, labels: list[str],
                 token: str) -> str | None:
    """Create a GitHub issue through the API."""
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps({
        "title": title,
        "body": body,
        "labels": labels,
    }).encode()
    req = urllib.request.Request(url, data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get("html_url")
    except Exception as e:
        print(f"  [ERROR] GitHub API: {e}")
        # Fallback: gh CLI, with its own login and not the token that just
        # failed. Passing the same token on would repeat the very request that
        # raised, so it would be useless against an auth error and only ever
        # help against a network blip. Both variables have to go: gh reads
        # GITHUB_TOKEN as well, and load_env() puts it in os.environ.
        gh = gh_binary()
        if not gh:
            print("  [ERROR] gh CLI not found — no fallback available")
            return None
        try:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
            result = subprocess.run(
                [gh, "issue", "create", "--repo", repo, "--title", title,
                 "--label", ",".join(labels), "--body", body],
                capture_output=True, text=True, timeout=30, env=env
            )
            if result.returncode == 0:
                return result.stdout.strip()
            print(f"  [ERROR] gh CLI: {result.stderr.strip()}")
        except Exception as e2:
            print(f"  [ERROR] gh fallback: {e2}")
        return None


def gh_api(url: str, token: str, method: str = "GET",
           payload: dict | None = None) -> dict | None:
    """Call the GitHub API, return the parsed body or None."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [WARN] GitHub API {method} {url}: {e}")
        return None


def resolve_stale(repo: str, token: str, state: dict, active_ids: set[str],
                  dry_run: bool) -> int:
    """Close issues whose underlying problem has stayed away long enough.

    Two things happen here, and both are needed. Closing the issue is the
    visible half; dropping the id from the state is the half that matters for
    the next outage, because an id that lingers suppresses every future alert
    for that service — the old code kept ids forever and thereby muted itself.

    Only issues that still carry the `auto` label are touched. Anything a human
    has re-labelled, or any id that turns out to point at a hand-written ticket,
    is left alone and simply forgotten from the state.
    """
    issue_map = state["issue_map"]
    now = datetime.now(timezone.utc)
    closed = 0

    for alert_id in list(issue_map):
        entry = issue_map[alert_id]
        if alert_id in active_ids:
            entry["last_seen"] = now.isoformat()
            continue

        try:
            last_seen = datetime.fromisoformat(entry["last_seen"])
        except (KeyError, ValueError):
            entry["last_seen"] = now.isoformat()
            continue
        if (now - last_seen).total_seconds() < RESOLVE_AFTER_HOURS * 3600:
            continue

        number = entry["url"].rstrip("/").rsplit("/", 1)[-1]
        if not number.isdigit():
            print(f"  [WARN] {alert_id}: cannot read issue number from "
                  f"{entry['url']} — dropping from state")
            del issue_map[alert_id]
            continue

        # Read the issue before deciding anything, dry run included — a dry run
        # that reported "would close" without checking the label would say the
        # opposite of what the real run does for a protected issue.
        api = f"https://api.github.com/repos/{repo}/issues/{number}"
        issue = gh_api(api, token)
        if issue is None:
            continue                       # transient — retry next run
        if issue.get("state") == "closed":
            if not dry_run:
                del issue_map[alert_id]
            continue
        labels = {lbl.get("name") for lbl in issue.get("labels", [])}
        if AUTO_LABEL not in labels:
            print(f"    [SKIP] #{number} is not labelled '{AUTO_LABEL}' — "
                  f"leaving it open, dropping {alert_id} from state")
            if not dry_run:
                del issue_map[alert_id]
            continue

        if dry_run:
            print(f"    [DRY] would close #{number} ({alert_id})")
            closed += 1
            continue

        gh_api(f"{api}/comments", token, "POST", {
            "body": (
                f"Resolved automatically: no `level:error` seen for this "
                f"service in the last {RESOLVE_AFTER_HOURS}h.\n\n"
                f"Closed by `monitor-check.py`. It will reopen a new issue if "
                f"the problem comes back."
            )
        })
        if gh_api(api, token, "PATCH", {"state": "closed",
                                        "state_reason": "completed"}):
            print(f"    ✅ Closed #{number} ({alert_id})")
            del issue_map[alert_id]
            closed += 1

    return closed


# ─── Main ───────────────────────────────────────────────────────────────────
def check_once(token: str, owner: str, repo: str, state: dict, dry_run: bool):
    """One check pass."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_repo = f"{owner}/{repo}"
    issue_map = state["issue_map"]
    known_ids = set(issue_map)
    active_ids: set[str] = set()          # problems seen in THIS run
    new_count = 0
    dropped = 0                           # alerts we found but could not file

    print(f"\n[{now}] Monitor check ({full_repo})")

    # ── 1. Check errors ──
    print("  [1/2] Checking errors...")
    error_alerts = find_recent_errors()
    print(f"    Found {len(error_alerts)} error groups")

    for alert in error_alerts:
        active_ids.add(alert["alert_id"])
        if alert["alert_id"] in known_ids:
            continue
        if dry_run:
            print(f"    [DRY] issue for {alert['service']}: {alert['message'][:60]}")
            new_count += 1
            continue

        title = f"🚨 [{alert['service']}] Error detected"
        body = (
            f"## Error Alert\n\n"
            f"**Service:** `{alert['service']}`\n"
            f"**Time:** {alert['timestamp']}\n"
            f"**Count:** {alert['count']} errors in last {ERROR_WINDOW_MINUTES}min\n"
            f"**Event:** {alert['event']}\n"
            f"**Message:** `{alert['message']}`\n\n"
            f"Check Kibana: {KIBANA_URL}/app/discover"
        )
        url = create_issue(full_repo, title, body,
                           ["alert", "error", AUTO_LABEL], token)
        if url:
            print(f"    ✅ Issue: {url}")
            issue_map[alert["alert_id"]] = {
                "url": url,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
            new_count += 1
        else:
            dropped += 1

    # ── 2. Check heartbeats ──
    print("  [2/2] Checking heartbeats...")
    missing = find_missing_heartbeats()
    print(f"    {len(missing)} services with missing heartbeat")

    for alert in missing:
        active_ids.add(alert["alert_id"])
        if alert["alert_id"] in known_ids:
            continue
        if dry_run:
            print(f"    [DRY] issue for {alert['service']}: no heartbeat for >{alert['max_age_minutes']}min")
            new_count += 1
            continue

        title = f"💀 [{alert['service']}] Heartbeat missing"
        body = (
            f"## Dead Man's Switch\n\n"
            f"**Service:** `{alert['service']}`\n"
            f"**Expected heartbeat interval:** {alert['max_age_minutes']}min\n"
            f"**Last heartbeat:** >{alert['max_age_minutes']}min ago or never\n\n"
            f"Action: Check if the service is running.\n"
            f"Kibana: {KIBANA_URL}/app/discover"
        )
        url = create_issue(full_repo, title, body,
                           ["alert", "heartbeat", AUTO_LABEL], token)
        if url:
            print(f"    ✅ Issue: {url}")
            issue_map[alert["alert_id"]] = {
                "url": url,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
            new_count += 1
        else:
            dropped += 1

    # ── 3. Close resolved alerts ──
    closed = resolve_stale(full_repo, token, state, active_ids, dry_run)
    if closed:
        print(f"  🧹 Closed {closed} resolved issue(s)")

    if not dry_run:
        state_save(state)

    # A failed create must never fall through to "All clear": that would be the
    # alert chain reporting success for the one thing it exists to do, and a
    # dead token would stay invisible for days. Say it, and exit non-zero so the
    # caller notices: the crontab line is
    # `monitor-check.py && curl .../ping/<uuid>`, so a non-zero exit withholds
    # the heartbeat and Healthchecks raises the alarm about the alarm chain
    # itself.
    if dropped:
        print(f"  ❌ {dropped} alert(s) found but NOT filed — check auth above.")
    elif new_count == 0:
        print("  ✅ All clear — no new issues.")
    return new_count, dropped


def main():
    load_env()
    dry_run = "--dry-run" in sys.argv
    watch_mode = "--watch" in sys.argv

    token, owner, repo = resolve_github_auth()
    if not token:
        print("GITHUB_TOKEN is not set. Put it in secrets/app.env or run `gh auth login`.")
        sys.exit(1)
    if not owner:
        print("GITHUB_OWNER is not set.")
        sys.exit(1)
    if not repo:
        print("GITHUB_REPO is not set (bare repository name; alerts land there).")
        sys.exit(1)

    state = state_load()

    if watch_mode:
        print(f"Watching every {CHECK_INTERVAL_SECONDS}s...")
        while True:
            try:
                check_once(token, owner, repo, state, dry_run)
                time.sleep(CHECK_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        _, dropped = check_once(token, owner, repo, state, dry_run)
        # Withholds the crontab's `&& curl .../ping/<uuid>`, see check_once.
        if dropped:
            sys.exit(1)


if __name__ == "__main__":
    main()
