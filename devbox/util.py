"""Tiny shared helpers with no devbox dependencies."""
from __future__ import annotations

import sys


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)
