"""Deterministic input hashing for decision auditability (Phase 5).

``inputs_hash`` lets any decision be reproduced later: identical bucket,
symbol, timeframe, agent identities, agent versions, and fused weights produce
an identical hash. It covers *what the pipeline consumed*, not the raw candle
bytes — replayability is guaranteed by the persisted signals themselves.
"""

from __future__ import annotations

import hashlib
import json


def inputs_hash(
    *,
    symbol: str,
    timeframe: str,
    bucket_ts: str,
    agent_versions: dict[str, str],
    weights: dict[str, float],
) -> str:
    """Return a hex sha256 over the stable input identity set."""
    payload = {
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "bucket_ts": bucket_ts,
        "agent_versions": dict(sorted((k, str(v)) for k, v in agent_versions.items())),
        "weights": dict(sorted((k, round(float(v), 6)) for k, v in weights.items())),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
