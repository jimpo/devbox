"""Tiny shared helpers with no devbox dependencies."""
from __future__ import annotations

import sys


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    """Non-fatal counterpart to `die`: print to stderr and keep going."""
    print(f"warning: {msg}", file=sys.stderr)
