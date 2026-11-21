"""Exception types shared across ai-router modules."""

from __future__ import annotations


class AiRouteError(Exception):
    """User-facing error that aborts the current command.

    Raised from library and command code instead of calling ``sys.exit()`` directly.
    ``cli.main()`` catches it, prints ``[error] <message>`` to stderr, and exits with code 1.
    """


class ConfigError(AiRouteError):
    """Raised when configuration file parsing or validation fails."""

