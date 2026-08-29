"""Backtest orchestration package (Phase 6).

Replays the production decision path (BaseAgent.analyze -> compute_decision ->
risk assessment) over historical candles, driving a deterministic PaperBroker.
Read-only with respect to live/paper trading state: backtests persist only to
``backtest_*`` tables. SAFE MODE is preserved — nothing here can create a live
order or touch ``orders_paper``/``positions``.
"""
