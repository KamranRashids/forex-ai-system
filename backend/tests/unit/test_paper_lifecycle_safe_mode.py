"""SAFE MODE regression: the Phase 13D paper lifecycle cannot execute or route.

The lifecycle domain (``app/broker/lifecycle.py``) is persistence /
paper-simulation orchestration only:
- It never opens a DB session and carries no session factory / engine.
- Its only broker surface is the paper-only ``LedgerBroker``; it exposes no
  submit/route/place/live-order method itself.
- It imports no execution / routing / live venue, and nothing from the API /
  workers / services layers.
- The single writer is the orchestrator's token-guarded worker (out of scope
  here); nothing in this module can send a real order.

(Phase 13D, Phase A)
"""

from __future__ import annotations

import ast
import inspect

import app.broker.lifecycle as lifecycle_module
from app.broker.adapter import BrokerAdapter
from app.broker.ledger import LedgerBroker


def _imports_of(module: object) -> list[str]:
    tree = ast.parse(inspect.getsource(module))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    return imports


def test_paper_lifecycle_conforms_to_paper_only_broker_surface():
    """The lifecycle drives the paper broker through the adapter contract."""
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


def test_paper_lifecycle_has_no_db_or_live_execution_imports():
    """Structural: the lifecycle imports no session factory or live surface."""
    imports = _imports_of(lifecycle_module)

    forbidden_imports = {
        "app.execution",
        "app.orders",
        "app.api",
        "app.db",
        "app.workers",
        "app.services",
    }
    assert forbidden_imports.isdisjoint(imports)

    live = [i for i in imports if any(tok in i.lower() for tok in ("oanda", "mt5", "fix", "live"))]
    assert not live


def test_paper_lifecycle_has_no_session_or_commit_identifiers():
    """The lifecycle never opens a DB session or commits/flushes a transaction."""
    tree = ast.parse(inspect.getsource(lifecycle_module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)

    forbidden_identifiers = {
        "AsyncSession",
        "async_sessionmaker",
        "get_sessionmaker",
        "session",
        "commit",
        "flush",
    }
    assert forbidden_identifiers.isdisjoint(names)


def test_paper_lifecycle_exposes_no_live_order_methods():
    """The lifecycle adds no order-routing surface of its own."""
    for name in ("submit_order", "route_order", "place_order", "create_live_order"):
        assert not hasattr(lifecycle_module, name)


def test_paper_lifecycle_keeps_paperbroker_math_delegation_imports_only():
    """The only broker modules it may reference are the paper ledger/math."""
    imports = _imports_of(lifecycle_module)
    broker_modules = [m for m in imports if m.startswith("app.broker")]
    assert broker_modules == [
        "app.broker.ledger",
    ]
    # The engine itself stays broker-free (PAPER decisions do not execute).
    import app.decisions.engine as engine_module

    engine_imports = _imports_of(engine_module)
    assert not any(m.startswith("app.broker") for m in engine_imports)


def test_safe_mode_still_only_permits_safe():
    from app.core.config import ALLOWED_TRADING_MODES
    from app.core.constants import SAFE_TRADING_MODE

    assert frozenset({SAFE_TRADING_MODE}) == ALLOWED_TRADING_MODES
    assert len(ALLOWED_TRADING_MODES) == 1
