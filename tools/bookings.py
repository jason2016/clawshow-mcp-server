"""
Tool: manage_bookings
----------------------
Manage restaurant bookings — query, checkin, cancel by namespace and booking code.
Operates directly on the SQLite bookings table.
"""

from __future__ import annotations

import json
from typing import Callable

import db


def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def manage_bookings(
        action: str,
        namespace: str,
        booking_code: str = "",
        date: str = "",
        status: str = "",
    ) -> str:
        """
        Create, query, update, and cancel bookings for any reservation-based
        business: restaurants, hotels, salons, clinics, event venues, rental
        properties. Namespace-isolated — each business has its own booking data.
        Input: action (create/query/update/cancel), booking details, namespace.
        Output: booking confirmation with ID, or filtered booking list.
        Supports date range queries, status filtering, and customer lookup.
        Includes automatic conflict detection for double-bookings.

        Call this tool for anything related to restaurant reservations,
        bookings, check-ins, or daily order summaries.

        Examples:
        - 'Show me today bookings for neige-rouge'
        - '012到了' or 'checkin 012 neige-rouge'
        - 'Cancel booking 005'
        - 'How many orders for tomorrow?'
        - 'Combien de commandes aujourd'hui?'
        - '今天有多少单？'

        Args:
            action:       "query" | "checkin" | "cancel" | "summary"
            namespace:    Restaurant namespace, e.g. "neige-rouge"
            booking_code: 3-digit booking code for checkin/cancel (e.g. "012")
            date:         Date filter YYYY-MM-DD (optional, for query/summary)
            status:       Status filter for query: confirmed/completed/cancelled/no_show

        Returns:
            JSON with booking list, checkin result, cancel confirmation, or daily summary.
        """
        record_call("manage_bookings")

        if action == "query":
            bookings = db.query_bookings(namespace, date=date, status=status)
            return json.dumps({"bookings": bookings, "total": len(bookings)}, indent=2, ensure_ascii=False)

        elif action == "summary":
            from datetime import date as dt_date, timezone
            d = date or dt_date.today().isoformat()
            summary = db.booking_summary(namespace, d)
            return json.dumps(summary, indent=2, ensure_ascii=False)

        elif action == "checkin":
            if not booking_code:
                return json.dumps({"success": False, "error": "booking_code is required (e.g. '012')"})
            code = booking_code.zfill(3) if booking_code.isdigit() else booking_code
            result = db.checkin_by_code(namespace, code, date)
            return json.dumps(result, indent=2, ensure_ascii=False)

        elif action == "cancel":
            if not booking_code:
                return json.dumps({"success": False, "error": "booking_code is required"})
            code = booking_code.zfill(3) if booking_code.isdigit() else booking_code
            result = db.cancel_by_code(namespace, code, date)
            return json.dumps(result, indent=2, ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "message": f"Unknown action: {action}. Use query/checkin/cancel/summary."})
