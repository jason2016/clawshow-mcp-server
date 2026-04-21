"""Namespace validation for ClawShow tools."""
from __future__ import annotations

import re

_VALID = re.compile(r'^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$')

RESERVED = {"admin", "root", "system", "clawshow", "internal"}


def validate_namespace(namespace: str) -> str:
    """Raise ValueError if namespace is invalid, else return it normalised."""
    if not namespace:
        raise ValueError("namespace is required")
    ns = namespace.strip().lower()
    if ns in RESERVED:
        raise ValueError(f"namespace '{ns}' is reserved")
    if not _VALID.match(ns):
        raise ValueError(
            f"namespace '{ns}' is invalid — use lowercase letters, digits and hyphens (2-64 chars)"
        )
    return ns
