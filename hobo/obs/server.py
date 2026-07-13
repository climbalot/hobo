"""Cold-path HTTP server (/dashboard, /metrics, /status, /healthz): one stdlib
ThreadingHTTPServer on METRICS_PORT, in its own daemon thread so it never touches
the asyncio hot path; it only reads state, never mutates it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from hobo.risk.model import Limit
from hobo.risk.state import State

_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_bytes()


def _status_payload(state: State, trades: list[dict], exchange: dict | None) -> dict:
    """Raw state dump plus a `summary` of derived values the dashboard needs (per-book
    attribution, per-book positions, per-instrument roll-up). Computed here, never stored."""
    from hobo.risk.math import notional as _notional
    from hobo.risk.math import unrealized_pnl as _upnl

    books: dict[str, dict] = {}
    instruments: dict[str, dict] = {
        iid: {
            "mark": state.mark(iid),
            "mark_ts_ns": state.mark_ts_ns.get(iid),
            "funding_rate": state.funding_rates.get(iid),
            "net_position": 0.0,
            "notional_usdt": 0.0,
            "unrealized_pnl": 0.0,
        }
        for iid in state.instruments
    }

    for book_id, book in state.desk.books.items():
        positions = {}
        for iid, p in book.positions.items():
            instr, mark = state.instruments[iid], state.mark(iid)
            pos_notional = _notional(p.qty, mark, instr)
            pos_upnl = _upnl(p.qty, p.avg_entry_price, mark, instr)
            positions[iid] = {"qty": p.qty, "avg_entry_price": p.avg_entry_price, "mark": mark, "notional_usdt": pos_notional, "unrealized_pnl": pos_upnl}
            roll = instruments[iid]
            roll["net_position"] += p.qty
            roll["notional_usdt"] += pos_notional
            roll["unrealized_pnl"] += pos_upnl
        books[book_id] = {
            "positions": positions,
            "notional_usdt": state.book_notional(book),
            "realized_pnl": state.book_realized_pnl(book),
            "unrealized_pnl": state.book_unrealized_pnl(book),
            "funding_pnl": state.book_funding_pnl(book),
            "fees_paid": state.book_fees_paid(book),
            "total_pnl": state.book_total_pnl(book),
            "drawdown": state.book_drawdown(book),
            "kill_switch": book.kill_switch,
            "limits": {
                "position_limit": book.limits[Limit.POSITION],
                "notional_limit_usdt": book.limits[Limit.NOTIONAL],
                "drawdown_limit_usdt": book.limits[Limit.DRAWDOWN],
            },
        }

    payload = state.to_dict()
    payload["summary"] = {
        "instruments": instruments,
        "books": books,
        "desk": {
            "total_pnl": state.desk_total_pnl(),
            "realized_pnl": sum(b["realized_pnl"] for b in books.values()),
            "unrealized_pnl": sum(b["unrealized_pnl"] for b in books.values()),
            "funding_pnl": sum(b["funding_pnl"] for b in books.values()),
            "fees_paid": sum(b["fees_paid"] for b in books.values()),
            "drawdown": state.desk_drawdown(),
            "gross_notional_usdt": state.desk_gross_notional(),
            "notional_limit_usdt": state.desk.limits[Limit.NOTIONAL],
            "drawdown_limit_usdt": state.desk.limits[Limit.DRAWDOWN],
            "kill_switch": state.desk.kill_switch,
            "kill_switch_reason": state.desk.kill_switch_reason,
        },
        "recent_trades": trades,
        "exchange": exchange,
    }
    return payload


def _make_handler(
    registry: CollectorRegistry,
    state_provider: Callable[[], State],
    trades_provider: Callable[[], list[dict]],
    exchange_provider: Callable[[], dict | None],
    order_submitter: Callable[[str, str, str, float], dict],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            pass  # cold path stays quiet; nothing here is on the hot path

        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")  # always live; never a stale dashboard
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path in ("/", "/dashboard"):
                self._send(200, "text/html; charset=utf-8", _DASHBOARD_HTML)
            elif self.path == "/metrics":
                self._send(200, CONTENT_TYPE_LATEST, generate_latest(registry))
            elif self.path == "/status":
                payload = json.dumps(_status_payload(state_provider(), trades_provider(), exchange_provider())).encode("utf-8")
                self._send(200, "application/json", payload)
            elif self.path == "/healthz":
                self._send(200, "application/json", b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            if self.path != "/order":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
                result = order_submitter(req["book"], req["instrument"], req["side"], float(req["qty"]))
            except Exception as exc:
                self._send(400, "application/json", json.dumps({"ok": False, "error": str(exc)[:200]}).encode())
                return
            self._send(200, "application/json", json.dumps(result).encode())

    return Handler


class ColdPathServer:
    def __init__(
        self,
        port: int,
        registry: CollectorRegistry,
        state_provider: Callable[[], State],
        trades_provider: Callable[[], list[dict]] | None = None,
        exchange_provider: Callable[[], dict | None] | None = None,
        order_submitter: Callable[[str, str, str, float], dict] | None = None,
    ) -> None:
        handler_cls = _make_handler(
            registry,
            state_provider,
            trades_provider or (lambda: []),
            exchange_provider or (lambda: None),
            order_submitter or (lambda *_: {"ok": False, "error": "order entry unavailable"}),
        )
        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "ColdPathServer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
