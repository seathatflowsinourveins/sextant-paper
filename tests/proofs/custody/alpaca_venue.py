"""Deterministic venue witness from Alpaca orders/streaming docs, read 2026-11-10.

It models the named venue rules, not market quality or broker qualification.
"""

from copy import deepcopy
from decimal import Decimal
from itertools import count
from uuid import UUID

from nautilus_trader.core import dt_to_unix_nanos

STAMP = "2026-11-10T15:00:00Z"
# Alpaca order statuses (orders-at-alpaca): terminal states leave the open listing.
CLOSED = frozenset({"filled", "canceled", "expired", "replaced", "rejected"})


def _ns(stamp: str) -> int:
    return dt_to_unix_nanos(stamp)


class Venue:
    def __init__(self):
        self.next_id = count(1)
        self.rows = {}
        self.positions: list[dict] = []
        self.activities: list[dict] = []
        self.until_exclusive = False  # Models the other reading of ``until`` (unverified).
        self.requests = []
        self.regular = True
        self.pending = None

    def post(self, path, body):
        assert path == "/orders"
        self.requests.append(("POST", path, deepcopy(body)))
        if body["extended_hours"] and body["type"] != "limit":
            raise ValueError("extended hours requires LIMIT")
        # Alpaca user-protection docs (read 2026-11-11): trailing stops are exempt from
        # wash-trade protection, including protection while a BUY remainder still rests.
        if body["type"] != "trailing_stop" and any(
            r["symbol"] == body["symbol"]
            and r["side"] != body["side"]
            and r["status"] not in ("filled", "canceled", "replaced")
            for r in self.rows.values()
        ):
            raise ValueError("potential wash trade")
        ident = str(UUID(int=next(self.next_id)))
        row = (
            dict(
                id=ident,
                asset_id=str(UUID(int=100)),
                asset_class="us_equity",
                created_at=STAMP,
                updated_at=STAMP,
                submitted_at=STAMP,
                status="new",
                filled_qty="0",
                filled_avg_price=None,
                replaces=None,
                replaced_by=None,
                stop_price=None,
            )
            | body
        )
        self.rows[ident] = row
        return deepcopy(row)

    def listing(self, params):
        """GET /orders parameters, alpaca-py requests.py:202-213 (GetOrdersRequest).

        status open|closed|all (default open), after/until on submission time
        (until inclusive unless ``until_exclusive``), symbols, limit (default 50,
        capped at the venue's 500), direction desc (default) or asc.
        """
        status = params.get("status", "open")
        symbols = params.get("symbols")
        if isinstance(symbols, str):
            symbols = symbols.split(",")
        rows = [
            r
            for r in self.rows.values()
            if status == "all" or (status == "open") == (r["status"] not in CLOSED)
            if symbols is None or r["symbol"] in symbols
            if "after" not in params or _ns(r["submitted_at"]) > _ns(params["after"])
            if "until" not in params
            or _ns(r["submitted_at"]) < _ns(params["until"])
            or (not self.until_exclusive and _ns(r["submitted_at"]) == _ns(params["until"]))
        ]
        rows.sort(key=lambda r: _ns(r["submitted_at"]), reverse=params.get("direction") != "asc")
        return deepcopy(rows[: min(int(params.get("limit", 50)), 500)])

    def get(self, path, params):
        self.requests.append(("GET", path, deepcopy(params)))
        if path == "/orders":
            return self.listing(params)
        if path == "/positions":
            return deepcopy(self.positions)
        if path == "/account/activities/FILL":
            # Account activities reference (read 2026-11-11): page_size default and maximum
            # 100; page_token is the id of the previous page's last row. Kept newest first.
            ids = [row.get("id") for row in self.activities]
            start = ids.index(params["page_token"]) + 1 if "page_token" in params else 0
            return deepcopy(self.activities[start : start + int(params.get("page_size", 100))])
        if path == "/orders:by_client_order_id":
            return deepcopy(
                next(
                    r
                    for r in self.rows.values()
                    if r["client_order_id"] == params["client_order_id"]
                )
            )
        return deepcopy(self.rows[path.rsplit("/", 1)[1]])

    def patch(self, path, body):
        self.requests.append(("PATCH", path, deepcopy(body)))
        old = self.rows[path.rsplit("/", 1)[1]]
        assert not Decimal(old["filled_qty"])
        successor = (
            deepcopy(old)
            | body
            | dict(id=str(UUID(int=next(self.next_id))), replaces=old["id"], status="pending_new")
        )
        if "trail" in body:
            successor["trail_percent"] = body["trail"]
        old["status"] = "pending_replace"  # Order statuses: waiting to be replaced.
        self.pending = (old, successor)
        return deepcopy(successor)  # Proposal only; fill can still win.

    def confirm(self):
        assert self.pending is not None
        old, successor = self.pending
        old.update(status="replaced", replaced_by=successor["id"])
        successor["status"] = "new"
        self.rows[successor["id"]] = successor
        self.pending = None
        return deepcopy(successor)

    def delete(self, path, body):
        self.requests.append(("DELETE", path, deepcopy(body)))
        self.rows[path.rsplit("/", 1)[1]]["status"] = "canceled"

    def quote(self, bid):
        for row in self.rows.values():
            if row["type"] == "trailing_stop" and row["status"] == "new":
                high = max(Decimal(row.get("hwm", bid)), Decimal(bid))
                row["hwm"] = str(high)
                stop = high * (1 - Decimal(row["trail_percent"]) / 100)
                row["stop_price"] = str(stop)
                if self.regular and Decimal(bid) <= stop:
                    row["status"] = "stopped"
        return deepcopy(list(self.rows.values()))
