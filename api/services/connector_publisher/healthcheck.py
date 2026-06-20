"""Healthcheck CLI for the docker-compose ``healthcheck`` directive.

Invoked as ``python -m services.connector_publisher.healthcheck``. Exits 0 when
the heartbeat file is newer than the staleness threshold, non-zero otherwise.
Mirrors the webhook-dispatcher healthcheck so the daemon's liveness logic is
version-controlled alongside it.

Threshold defaults to 30s -- six times the 5s HEARTBEAT_INTERVAL_S. A miss means
the loop has been wedged for several intervals and docker-compose should restart
the container.
"""

import os
import sys
import time

from . import HEARTBEAT_FILE_DEFAULT

STALENESS_THRESHOLD_S = 30


def is_healthy(heartbeat_file=None, threshold_s=STALENESS_THRESHOLD_S, now_fn=time.time):
    """True when the heartbeat file's mtime is within threshold_s of now_fn().
    now_fn is injectable so a test can assert at controlled timestamps."""
    path = heartbeat_file or os.environ.get(
        "CONNECTOR_PUBLISHER_HEARTBEAT_FILE", HEARTBEAT_FILE_DEFAULT
    )
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    return (now_fn() - mtime) <= threshold_s


def main():
    sys.exit(0 if is_healthy() else 1)


if __name__ == "__main__":
    main()
