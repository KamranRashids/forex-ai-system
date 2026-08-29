"""Deterministic paper trading primitives (Phase 6).

These are PAPER/analysis-only: fills, costs (spread + slippage), positions, and
equity bookkeeping for backtests and (later) SAFE MODE paper trading. Nothing
here routes or describes a real order, and no broker/live-execution path exists.
SAFE MODE is preserved — backtests persist only to ``backtest_*`` tables and
never touch ``orders_paper``/``positions``.
"""
