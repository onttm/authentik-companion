"""Docker healthcheck — is the poll loop still completing cycles?

main.py rewrites HEARTBEAT_FILE after every successful poll. The loop catches all
exceptions and never exits, so without this a container whose token was revoked
looks identical to a healthy one: running, quiet, and doing nothing.

Unhealthy if the heartbeat is older than three poll intervals (floor 90s).
"""

import os
import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/data/heartbeat"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
MAX_AGE = max(POLL_INTERVAL * 3, 90)


def main() -> int:
    if os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on"):
        return 0  # dry runs deliberately write nothing

    try:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    except OSError:
        print(f"no heartbeat at {HEARTBEAT_FILE} yet")
        return 1

    if age > MAX_AGE:
        print(f"heartbeat is {age:.0f}s old (max {MAX_AGE}s)")
        return 1

    print(f"ok — heartbeat {age:.0f}s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())
