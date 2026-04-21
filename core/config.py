"""
Gateway mode configuration — Week 4.

Allows switching between test and live per namespace.
Safe default: all namespaces in test mode.
"""
from __future__ import annotations

import os
from typing import Dict

# Override via env var: CLAWSHOW_{NAMESPACE}_{GATEWAY}_MODE = test|live
# Example: CLAWSHOW_ILCI_WILLIAM_MOLLIE_MODE=live

_NAMESPACE_CONFIG: Dict[str, Dict[str, str]] = {
    "default": {"mollie": "test", "stripe": "test"},
}


def get_gateway_mode(namespace: str, gateway: str = "mollie") -> str:
    """
    Return 'test' or 'live' for a given namespace + gateway.

    Priority:
    1. Env var CLAWSHOW_{NS}_{GW}_MODE
    2. In-memory config
    3. 'test' (safe default)
    """
    env_key = f"CLAWSHOW_{namespace.upper().replace('-', '_')}_{gateway.upper()}_MODE"
    env_mode = os.environ.get(env_key)
    if env_mode in ("test", "live"):
        return env_mode

    ns_config = _NAMESPACE_CONFIG.get(namespace, _NAMESPACE_CONFIG["default"])
    return ns_config.get(gateway, "test")


def is_live_mode(namespace: str, gateway: str = "mollie") -> bool:
    return get_gateway_mode(namespace, gateway) == "live"
