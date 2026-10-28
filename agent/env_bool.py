"""Helpers for parsing boolean environment variables."""

import os

_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def parse_bool_env(name: str, *, default: bool) -> bool:
    """Parse an environment variable as a bool.

    An unset or empty value uses ``default``. Accepted values are case-insensitive
    true/false, 1/0, yes/no, and on/off. Any other value raises ValueError.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default

    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False

    accepted = "true/false, 1/0, yes/no, or on/off"
    raise ValueError(f"{name} must be {accepted}; got {raw!r}")
