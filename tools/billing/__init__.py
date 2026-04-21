# ClawShow Billing Tools — Week 1 (2026-04-21)
from __future__ import annotations

from typing import Callable

from tools.billing.create_plan import register as _register_create
from tools.billing.get_status import register as _register_get_status


def register(mcp, record_call: Callable) -> None:
    _register_create(mcp, record_call)
    _register_get_status(mcp, record_call)
