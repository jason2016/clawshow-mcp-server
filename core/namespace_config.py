"""
Namespace configuration loader.

Loads per-namespace YAML files from config/namespaces/.
Falls back to core/brand_config.py data for namespaces without a YAML file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(__file__).parent.parent / "config" / "namespaces"

# Simple cached dict of loaded configs
_cache: dict[str, "_NamespaceConfig"] = {}


class _Obj:
    """Wraps a dict so attrs can be accessed as obj.key."""
    def __init__(self, d: dict):
        self._d = d or {}

    def __getattr__(self, key: str) -> Any:
        val = self._d.get(key)
        if isinstance(val, dict):
            return _Obj(val)
        return val

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


class _NamespaceConfig:
    def __init__(self, data: dict):
        self._data = data
        self.brand = _Obj(data.get("brand", {}))
        self.billing = _Obj(data.get("billing", {}))
        self.external = _Obj(data.get("external", {}))
        self.notification = _Obj(data.get("notification", {}))


def load_namespace_config(namespace: str) -> _NamespaceConfig:
    """
    Load namespace config from YAML file.
    Falls back to brand_config.py data if no YAML found.
    Results are cached for the process lifetime.
    """
    if namespace in _cache:
        return _cache[namespace]

    yaml_path = _CONFIG_DIR / f"{namespace}.yaml"
    if yaml_path.exists():
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    else:
        # Fallback: build from brand_config
        from core.brand_config import get_brand
        brand = get_brand(namespace)
        data = {
            "namespace": namespace,
            "brand": brand,
            "billing": {"default_gateway": "mollie", "commission_rate": 0.005},
            "external": {"platform": None},
            "notification": {
                "email_from": os.getenv("RESEND_FROM", "hello@clawshow.ai"),
                "email_from_name": brand.get("name", "ClawShow"),
                "language": "fr",
            },
        }

    cfg = _NamespaceConfig(data)
    _cache[namespace] = cfg
    return cfg


def clear_cache() -> None:
    """Clear the config cache (useful in tests)."""
    _cache.clear()
