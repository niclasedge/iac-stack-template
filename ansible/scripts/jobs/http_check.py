#!/usr/bin/env python3
"""HTTP availability probe used by playbooks/jobs/http_check.yml.

Reads URLs from the HTTP_CHECK_URLS environment variable (comma separated),
requests each one and prints ONE JSON object on stdout:

    {"total": 2, "up": 1, "down": 1, "down_urls": [{"url": "...", "reason": "..."}]}

A URL that is unreachable or answers with a status >= 400 counts as down. That
is a FINDING, not a crash: the script still exits 0 and the finding travels
through the playbook's level:error result log to monitor-check. A non-zero exit
means the script itself broke (uncaught exception), which the playbook turns
into a failed job.

Stdlib only, so it runs on any host with python3.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 10


def probe(url: str) -> str | None:
    """Return None when the URL is up, else a short reason."""
    request = urllib.request.Request(url, headers={"User-Agent": "iac-stack-http-check"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                return f"HTTP {response.status}"
            return None
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"unreachable: {exc}"


def main() -> None:
    urls = [u.strip() for u in os.environ.get("HTTP_CHECK_URLS", "").split(",") if u.strip()]
    down = []
    for url in urls:
        reason = probe(url)
        if reason:
            down.append({"url": url, "reason": reason})
    print(
        json.dumps(
            {
                "total": len(urls),
                "up": len(urls) - len(down),
                "down": len(down),
                "down_urls": down,
            }
        )
    )


if __name__ == "__main__":
    main()
