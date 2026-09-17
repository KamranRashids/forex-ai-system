"""SAFE MODE regression: the Phase A ledger foundation cannot execute or route.

The paper-ledger foundation is persistence-only:
- ``LedgerBroker`` performs no live execution: its simulation math is the
  in-memory ``PaperBroker``, and it has no session/engine and no
  submit/route/place/live-order method.
- The decision engine (the thing that would normally sponsor a trade) is NOT
  wired to any broker/adapter/ledger yet — a PAPER decision is not
  auto-subscribed into ``orders_paper``.
- There is still no ``executor`` worker role.

(Phase 13, Phase A)
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

from app.broker.adapter import BrokerAdapter
from app.broker.ledger import LedgerBroker
from app.broker.paper import PaperBroker


class _NullStore:
    """Minimal store stub so the broker can be constructed for inspection."""

    async def save_order(self, order: object) -> None: ...

    async def save_position(self, position: object) -> None: ...

    async def get_open_position(self, symbol: str) -> None:
        return None

    async def list_open_positions(self) -> list[object]:
        return []

    async def load_open_position_rows(self) -> list[object]:
        return []

    async def load_realized_pnl_since(self, start_ts: object, end_ts: object) -> Decimal:
        return Decimal("0")

    async def list_pending_expired(self, now: object) -> list[object]:
        return []

    async def list_closed_positions(self) -> list[object]:
        return []

    async def update_position(self, position: object) -> None: ...

    async def save_snapshot(self, snapshot: object) -> None: ...

    async def latest_snapshot(self) -> None:
        return None


def test_ledger_broker_conforms_to_broker_adapter():
    """LedgerBroker structurally satisfies the BrokerAdapter paper-only contract."""
    required = {
        "submit_paper_order",
        "fill_pending",
        "cancel_pending",
        "evaluate_exit",
        "close_on_signal",
        "mark_position",
        "equity_snapshot",
        "restore_state",
    }
    assert required.issubset(set(dir(BrokerAdapter)))
    assert required.issubset(set(dir(LedgerBroker)))


def test_ledger_broker_has_no_execution_surface():
    broker = LedgerBroker(store=_NullStore())
    assert isinstance(broker._paper, PaperBroker)
    assert not hasattr(broker, "session")
    assert not hasattr(broker, "engine")
    for name in ("submit_order", "route_order", "place_order", "create_live_order"):
        assert not hasattr(broker, name)


def test_ledger_broker_has_no_db_or_live_execution_imports():
    """Structural: the broker imports no DB session or live-execution surface."""
    import app.broker.ledger as ledger_module

    tree = ast.parse(inspect.getsource(ledger_module))
    imports: list[str] = []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)

    forbidden_imports = {
        "app.execution",
        "app.orders",
        "app.api",
        "app.db",
        "app.workers",
        "app.services",
    }
    assert forbidden_imports.isdisjoint(imports)

    forbidden_identifiers = {
        "AsyncSession",
        "async_sessionmaker",
        "get_sessionmaker",
        "session",
        "commit",
        "flush",
    }
    assert forbidden_identifiers.isdisjoint(names)


def test_decision_engine_is_not_wired_to_a_broker():
    """PAPER decisions remain non-executing: no broker import in the engine."""
    import app.decisions.engine as engine_module

    tree = ast.parse(inspect.getsource(engine_module))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(module.startswith("app.broker") for module in imports)


def test_no_executor_worker_role_yet():
    """The executor role is still refused by the worker entrypoint."""
    import app.worker_main as worker_main

    tree = ast.parse(inspect.getsource(worker_main))
    literal_roles: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal_roles.add(node.value)
    assert "executor" not in literal_roles
    # Unimplemented roles exit non-zero.
    assert "worker_role_not_available_yet" in literal_roles


def test_safe_mode_still_only_permits_safe():
    from app.core.config import ALLOWED_TRADING_MODES
    from app.core.constants import SAFE_TRADING_MODE

    assert frozenset({SAFE_TRADING_MODE}) == ALLOWED_TRADING_MODES
    assert len(ALLOWED_TRADING_MODES) == 1
