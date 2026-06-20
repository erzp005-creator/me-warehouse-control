"""Boot-time config validation for the connector-publisher daemon.

Mirrors services/webhook_dispatcher/env_validator: every CONNECTOR_PUBLISHER_*
tunable has a documented [lo, hi] range and a default, read at use-time via
int_var so a value can change with a restart and no code edit. validate_or_die()
fails fast on a misconfiguration so a bad deploy crashes the container loudly
rather than running with surprising behavior.
"""

import logging
import os

LOGGER = logging.getLogger("connector_publisher.env")

# (name, lo, hi, default)
_RANGE_VARS = [
    ("CONNECTOR_PUBLISHER_FALLBACK_POLL_MS", 250, 60000, 2000),
    ("CONNECTOR_PUBLISHER_RECONCILE_INTERVAL_S", 5, 3600, 60),
    ("CONNECTOR_PUBLISHER_SHUTDOWN_DRAIN_S", 1, 300, 30),
    ("CONNECTOR_PUBLISHER_HTTP_CONNECT_TIMEOUT_MS", 500, 60000, 5000),
    ("CONNECTOR_PUBLISHER_HTTP_READ_TIMEOUT_MS", 500, 120000, 8000),
]
_DEFAULTS = {name: default for (name, _lo, _hi, default) in _RANGE_VARS}


class ConfigError(SystemExit):
    """Raised (as a SystemExit) when boot validation fails, so the container
    exits non-zero and deployment automation flags the step."""


def _read_str(name):
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def bool_var(name, default=False):
    raw = _read_str(name)
    return default if raw is None else raw.lower() == "true"


def int_var(name):
    """Always re-read from the environment (never cached at import) so a restart
    picks up a new value. Falls back to the documented default."""
    raw = _read_str(name)
    if raw is None:
        return _DEFAULTS[name]
    return int(raw)


def str_var(name, default=None):
    return _read_str(name) or default


def enabled():
    # Default ON. Only the explicit string "false" disables, so an unrelated
    # truthy value cannot silently engage the kill switch.
    return os.environ.get("CONNECTOR_PUBLISHER_ENABLED", "true").lower() != "false"


def _validate_range(name, lo, hi):
    raw = _read_str(name)
    if raw is None:
        return
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if not (lo <= val <= hi):
        raise ConfigError(f"{name}={val} is out of range [{lo}, {hi}]")


def validate_or_die():
    for (name, lo, hi, _default) in _RANGE_VARS:
        _validate_range(name, lo, hi)
    if enabled() and not (_read_str("PUBLISHER_DATABASE_URL") or _read_str("DATABASE_URL")):
        raise ConfigError(
            "connector-publisher needs PUBLISHER_DATABASE_URL or DATABASE_URL"
        )
    LOGGER.info("connector-publisher env validated")
