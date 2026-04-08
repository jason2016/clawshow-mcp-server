"""
Tool: manage_inventory
-----------------------
Universal inventory management. Add/remove/adjust stock, query levels,
get low-stock alerts. Storage: JSON files per namespace.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

_DATA_ROOT = Path(__file__).parent.parent / "data" / "inventory"


def _ns_dir(namespace: str) -> Path:
    d = _DATA_ROOT / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_sku(namespace: str) -> str:
    today = date.today().strftime("%Y%m%d")
    prefix = f"INV-{today}-"
    d = _ns_dir(namespace)
    existing = [f.stem for f in d.glob(f"{prefix}*.json")]
    if existing:
        nums = [int(name.split("-")[-1]) for name in existing]
        seq = max(nums) + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


def _status(quantity: int, min_stock: int) -> str:
    if quantity <= 0:
        return "out_of_stock"
    if quantity <= min_stock:
        return "low_stock"
    return "in_stock"


def _save(namespace: str, item: dict) -> None:
    path = _ns_dir(namespace) / f"{item['sku']}.json"
    path.write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")


def _load(namespace: str, sku: str) -> dict | None:
    path = _ns_dir(namespace) / f"{sku}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _load_all(namespace: str) -> list[dict]:
    d = _ns_dir(namespace)
    items = []
    for f in sorted(d.glob("INV-*.json")):
        try:
            items.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return items


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_add(namespace: str, p: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    sku = p.get("sku") or _next_sku(namespace)
    qty = p.get("quantity", 0)
    min_s = p.get("min_stock", 5)

    existing = _load(namespace, sku)
    if existing:
        existing["quantity"] += qty
        existing["status"] = _status(existing["quantity"], existing.get("min_stock", 5))
        existing["updated_at"] = now
        existing.setdefault("history", []).append({
            "action": "add", "quantity": qty, "date": now, "reason": "restock",
        })
        _save(namespace, existing)
        return existing

    item: dict = {
        "sku": sku,
        "item_name": p.get("item_name", ""),
        "quantity": qty,
        "unit": p.get("unit", "piece"),
        "category": p.get("category", ""),
        "unit_cost": p.get("unit_cost", 0),
        "location": p.get("location", ""),
        "min_stock": min_s,
        "status": _status(qty, min_s),
        "metadata": p.get("metadata", {}),
        "history": [{"action": "add", "quantity": qty, "date": now, "reason": "initial"}],
        "created_at": now,
        "updated_at": now,
    }
    _save(namespace, item)
    return item


def _action_remove(namespace: str, p: dict) -> dict:
    sku = p.get("sku", "")
    qty = p.get("quantity", 0)
    reason = p.get("reason", "sold")
    order_id = p.get("order_id", "")

    item = _load(namespace, sku)
    if not item:
        return {"status": "error", "message": f"SKU {sku} not found in '{namespace}'"}

    if item["quantity"] < qty:
        return {"status": "error", "message": f"Insufficient stock: {item['quantity']} available, {qty} requested"}

    now = datetime.now(timezone.utc).isoformat()
    item["quantity"] -= qty
    item["status"] = _status(item["quantity"], item.get("min_stock", 5))
    item["updated_at"] = now

    entry: dict = {"action": "remove", "quantity": qty, "date": now, "reason": reason}
    if order_id:
        entry["order_id"] = order_id
    item.setdefault("history", []).append(entry)

    _save(namespace, item)

    return {
        "sku": sku,
        "item_name": item["item_name"],
        "removed": qty,
        "remaining": item["quantity"],
        "status": item["status"],
        "reason": reason,
        "order_id": order_id,
        "updated_at": now,
    }


def _action_adjust(namespace: str, p: dict) -> dict:
    sku = p.get("sku", "")
    new_qty = p.get("new_quantity", 0)
    reason = p.get("reason", "manual adjustment")

    item = _load(namespace, sku)
    if not item:
        return {"status": "error", "message": f"SKU {sku} not found in '{namespace}'"}

    now = datetime.now(timezone.utc).isoformat()
    old_qty = item["quantity"]
    item["quantity"] = new_qty
    item["status"] = _status(new_qty, item.get("min_stock", 5))
    item["updated_at"] = now
    item.setdefault("history", []).append({
        "action": "adjust", "old": old_qty, "new": new_qty, "date": now, "reason": reason,
    })
    _save(namespace, item)

    return {
        "sku": sku,
        "item_name": item["item_name"],
        "old_quantity": old_qty,
        "new_quantity": new_qty,
        "difference": new_qty - old_qty,
        "reason": reason,
        "updated_at": now,
    }


def _action_query(namespace: str, p: dict) -> dict:
    items = _load_all(namespace)

    # Refresh status
    for item in items:
        item["status"] = _status(item["quantity"], item.get("min_stock", 5))

    sku = p.get("sku")
    if sku:
        item = next((i for i in items if i["sku"] == sku), None)
        if item:
            return {"total_items": 1, "items": [item], "summary": _summary([item])}
        return {"status": "error", "message": f"SKU {sku} not found"}

    category = p.get("category")
    keyword = p.get("keyword")
    below_min = p.get("below_min", False)

    if category:
        items = [i for i in items if i.get("category") == category]
    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i.get("item_name", "").lower()]
    if below_min:
        items = [i for i in items if i["quantity"] <= i.get("min_stock", 5)]

    return {"total_items": len(items), "items": items, "summary": _summary(items)}


def _action_alert(namespace: str, p: dict) -> dict:
    items = _load_all(namespace)
    alerts = []
    for i in items:
        min_s = i.get("min_stock", 5)
        if i["quantity"] <= min_s:
            alerts.append({
                "sku": i["sku"],
                "item_name": i["item_name"],
                "quantity": i["quantity"],
                "min_stock": min_s,
                "shortage": max(0, min_s - i["quantity"]),
            })
    return {"alerts": alerts, "total_alerts": len(alerts)}


def _summary(items: list[dict]) -> dict:
    return {
        "total_value": sum(i.get("quantity", 0) * i.get("unit_cost", 0) for i in items),
        "in_stock": len([i for i in items if _status(i["quantity"], i.get("min_stock", 5)) == "in_stock"]),
        "low_stock": len([i for i in items if _status(i["quantity"], i.get("min_stock", 5)) == "low_stock"]),
        "out_of_stock": len([i for i in items if i["quantity"] <= 0]),
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def manage_inventory(
        action: str,
        namespace: str,
        item_name: str = "",
        sku: str = "",
        quantity: int = 0,
        new_quantity: int = 0,
        unit: str = "piece",
        category: str = "",
        unit_cost: float = 0,
        location: str = "",
        min_stock: int = 5,
        metadata: dict | None = None,
        reason: str = "",
        order_id: str = "",
        below_min: bool = False,
        keyword: str = "",
    ) -> str:
        """
        Track inventory levels with add, remove, query, and low-stock alert
        capabilities. Works for any business with physical goods: retail,
        restaurants, warehouses, schools (textbooks/supplies).
        Input: action (add/remove/query/alert), item details, namespace.
        Output: current stock levels, or alert list for items below threshold.
        Supports batch updates and inventory snapshots.
        Namespace-isolated for multi-tenant use.

        Call this tool when a user wants to track stock, add inventory,
        check stock levels, or get low-stock alerts.

        Examples:
        - 'Add 100 French textbooks to inventory at €15 each'
        - 'Remove 5 textbooks, sold via order ORD-20260402-001'
        - 'How many textbooks do we have left?'
        - 'Show me all items below minimum stock'
        - 'Adjust towel inventory to 50 after stocktake'
        - 'Ajoute 200 serviettes au stock du restaurant'

        Args:
            action:       "add" | "remove" | "adjust" | "query" | "alert"
            namespace:    Business namespace, e.g. "florent", "school-paris"

            # add params:
            item_name:    Item name
            sku:          Optional SKU (auto-generated if empty)
            quantity:     Quantity to add
            unit:         "piece" | "kg" | "liter" | "box" | "pack"
            category:     "product" | "material" | "supply" | "ingredient" | "equipment"
            unit_cost:    Cost per unit
            location:     Storage location
            min_stock:    Minimum stock alert threshold (default 5)
            metadata:     Custom key-value pairs

            # remove params:
            sku:          SKU to deduct from
            quantity:     Amount to remove
            reason:       "sold" | "used" | "damaged" | "returned" | "other"
            order_id:     Optional linked order ID

            # adjust params:
            sku:          SKU to adjust
            new_quantity: Actual count after stocktake
            reason:       Adjustment reason

            # query params:
            sku:          Query specific SKU (optional)
            category:     Filter by category
            keyword:      Search by item name
            below_min:    True = only items below min_stock

            # alert: no extra params needed

        Returns:
            JSON with item details, query results, or alert list.
        """
        record_call("manage_inventory")

        p = {
            "item_name": item_name, "sku": sku, "quantity": quantity,
            "new_quantity": new_quantity, "unit": unit, "category": category,
            "unit_cost": unit_cost, "location": location, "min_stock": min_stock,
            "metadata": metadata or {}, "reason": reason, "order_id": order_id,
            "below_min": below_min, "keyword": keyword,
        }

        if action == "add":
            result = _action_add(namespace, p)
        elif action == "remove":
            result = _action_remove(namespace, p)
        elif action == "adjust":
            result = _action_adjust(namespace, p)
        elif action == "query":
            result = _action_query(namespace, p)
        elif action == "alert":
            result = _action_alert(namespace, p)
        else:
            result = {"status": "error", "message": f"Unknown action: {action}. Use add/remove/adjust/query/alert."}

        return json.dumps(result, indent=2, ensure_ascii=False)
