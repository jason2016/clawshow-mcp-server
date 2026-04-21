# ClawShow Billing Tools — Week 3 (2026-04-21)
from __future__ import annotations

from typing import Callable

from tools.billing.create_plan import register as _register_create
from tools.billing.get_status import register as _register_get_status
from tools.billing.cancel_plan import register as _register_cancel


def register(mcp, record_call: Callable) -> None:
    _register_create(mcp, record_call)
    _register_get_status(mcp, record_call)
    _register_cancel(mcp, record_call)
