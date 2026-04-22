"""
Namespace → brand configuration.
MVP: hardcoded dict. Future: load from config/namespaces/*.yaml.

Brand fields:
  name           — display name shown on payment page
  logo_url       — brand logo (empty = show text name only)
  primary_color  — hex color for buttons/highlights
  support_email  — merchant support email shown to customer
  support_phone  — optional
"""
from __future__ import annotations

_BRANDS: dict[str, dict] = {
    "neige-rouge": {
        "name": "Restaurant Neige Rouge",
        "logo_url": "",
        "primary_color": "#C41E3A",
        "support_email": "contact@neige-rouge.fr",
        "support_phone": "",
    },
    "neige-rouge-e2e": {
        "name": "Restaurant Neige Rouge",
        "logo_url": "",
        "primary_color": "#C41E3A",
        "support_email": "contact@neige-rouge.fr",
        "support_phone": "",
    },
    "dragons-elysees": {
        "name": "Dragons des Champs-Élysées",
        "logo_url": "",
        "primary_color": "#D4AF37",
        "support_email": "contact@dragons-elysees.fr",
        "support_phone": "",
    },
    "ilci": {
        "name": "ILCI Paris",
        "logo_url": "",
        "primary_color": "#1A3C6E",
        "support_email": "scolarite@ilci.fr",
        "support_phone": "",
    },
    "ilci-william-sandbox": {
        "name": "ILCI Paris (Sandbox)",
        "logo_url": "",
        "primary_color": "#1A3C6E",
        "support_email": "test@ilci.fr",
        "support_phone": "",
    },
    "florent": {
        "name": "Florent Immobilier",
        "logo_url": "",
        "primary_color": "#2E5E4E",
        "support_email": "contact@florent.fr",
        "support_phone": "",
    },
    "uhtech": {
        "name": "UHTECH",
        "logo_url": "",
        "primary_color": "#0066CC",
        "support_email": "support@uhtech.com",
        "support_phone": "",
    },
}

_DEFAULT_BRAND = {
    "name": "ClawShow",
    "logo_url": "",
    "primary_color": "#6366F1",
    "support_email": "hello@clawshow.ai",
    "support_phone": "",
}


def get_brand(namespace: str) -> dict:
    """Return brand config for namespace. Falls back to ClawShow default."""
    return _BRANDS.get(namespace, {**_DEFAULT_BRAND})


def get_all_namespaces() -> list[str]:
    return list(_BRANDS.keys())
