"""SAFE MODE regression: backtest/broker path can never create live orders.

The backtest landscape is deliberately PAPER-ONLY:
- ``PaperBroker`` is an in-memory simulator with no engine/session and no
  "submit order" / live-execution method.
- The replay driver only calls broker + pure decision methods (no DB ledger,
  no ``orders_paper``/``positions`` writes).
- SAFE MODE L1 still pins the only permitted trading mode to ``safe``.
"""

from __future__ import annotations

from app.broker.paper import PaperBroker


def test_paper_broker_has_no_db_execution_surface():
    b = PaperBroker()
    assert not hasattr(b, "session")
    assert not hasattr(b, "engine")
    # No method that routes/submits to a broker.
    for name in ("submit_order", "route_order", "place_order", "create_live_order"):
        assert not hasattr(b, name)


def test_backtest_driver_only_uses_broker_and_engine():
    """Structural: the driver has no DB/execution surface.

    We inspect AST Name/Attribute nodes and imports (docstrings ignored). The
    driver legitimately reads ``broker.positions`` (the in-memory PaperBroker
    position set), so we assert on *DB/execution surfaces* rather than the word
    "positions": no order/execution imports, no AsyncSession, no DB write ops.
    """
    import ast
    import inspect

    import app.backtest.driver as driver_module

    tree = ast.parse(inspect.getsource(driver_module))
    names: set[str] = set()
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)

    forbidden_identifiers = {
        "orders_paper",
        "get_sessionmaker",
        "AsyncSession",
        "async_sessionmaker",
        "session",
        ".add(",
        "commit",
        "flush",
    }
    assert forbidden_identifiers.isdisjoint(names)

    forbidden_imports = {"app.execution", "app.orders", "app.positions", "app.api", "app.db"}
    assert forbidden_imports.isdisjoint(imports)


def test_safe_mode_still_only_permits_safe():
    from app.core.config import ALLOWED_TRADING_MODES
    from app.core.constants import SAFE_TRADING_MODE

    assert frozenset({SAFE_TRADING_MODE}) == ALLOWED_TRADING_MODES
    assert len(ALLOWED_TRADING_MODES) == 1
