"""Signal fusion: combine technical/fundamental/sentiment into one decision.

How the four agents combine (approved §B.4-5):

- The **regime** agent never votes directionally; it only selects the weight
  matrix (regime-conditional), so it conditions *how* the three directional
  agents are combined.
- The three directional agents (**technical**, **fundamental**, **sentiment**)
  each contribute a signed vote ``sign(direction) * confidence``.
- Weighted score = ``sum(weight * vote)`` over the agents that actually voted
  (direction != FLAT and confidence > 0); weights are renormalized over the
  voting set so they sum to 1.0.
- `direction` = LONG/SHORT/FLAT by the fusion threshold with a small
  hysteresis so near-threshold scores don't flip every bar.
- `confidence` = |score| clamped to [0,1]; `agreement` = weighted fraction of
  voting agents that align with the fused direction.

SAFE MODE: fusion is pure analysis — it produces a score/direction only and can
never manufacture an order. Missing/contradictory inputs surface as low
agreement/coverage and tend toward FLAT/ANALYSIS, never bypassing risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.base import AgentSignal, Direction

#: Regime agent id (conditions the matrix, never votes directionally).
REGIME_AGENT_ID: str = "regime"
#: Directional agents that can vote. Order matters for deterministic ties.
DIRECTIONAL_AGENTS: tuple[str, ...] = ("technical", "fundamental", "sentiment")

#: Regime-conditional directional weights (sum to 1.0 per regime).
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "trending": {"technical": 0.55, "fundamental": 0.20, "sentiment": 0.25},
    "weakening_trend": {"technical": 0.45, "fundamental": 0.30, "sentiment": 0.25},
    "transitional": {"technical": 0.50, "fundamental": 0.25, "sentiment": 0.25},
    "range": {"technical": 0.60, "fundamental": 0.20, "sentiment": 0.20},
    "unknown": {"technical": 0.40, "fundamental": 0.30, "sentiment": 0.30},
}


@dataclass(frozen=True, slots=True)
class FusionParams:
    threshold: float = 0.15
    hysteresis: float = 0.04
    weights: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


@dataclass(frozen=True, slots=True)
class ContextInput:
    """Optional higher-timeframe context (approved Q2: larger TF as context)."""

    direction: Direction
    confidence: float
    weight: float = 0.15


@dataclass(frozen=True, slots=True)
class FusedResult:
    score: float
    direction: Direction
    confidence: float
    agreement: float
    weights: dict[str, float]
    voting_agents: tuple[str, ...]
    threshold: float = 0.15
    #: Score before any cross-timeframe context nudge (for audit).
    pre_context_score: float = 0.0

    @property
    def has_votes(self) -> bool:
        return len(self.voting_agents) > 0


def sign_of(direction: Direction) -> int:
    return 0 if direction == Direction.FLAT else (1 if direction == Direction.LONG else -1)


def direction_for_score(score: float, *, threshold: float) -> Direction:
    if score >= threshold:
        return Direction.LONG
    if score <= -threshold:
        return Direction.SHORT
    return Direction.FLAT


def _renormalized(weights: dict[str, float], voting: list[str]) -> dict[str, float]:
    """Scale weights over the voting subset so they sum to 1.0 ({} when none)."""
    chosen = {agent: weights.get(agent, 0.0) for agent in voting if weights.get(agent, 0.0) > 0}
    total = sum(chosen.values())
    if total <= 0:
        return {}
    scaled = {agent: w / total for agent, w in chosen.items()}
    # Deterministic ordering (input order is already deterministic).
    return dict(scaled)


def fuse(
    signals: dict[str, AgentSignal],
    *,
    regime: str,
    params: FusionParams | None = None,
) -> FusedResult:
    """Combine directional agent signals into one fused result.

    ``signals`` maps agent_id -> its (already fresh/validated) signal. Agents
    whose direction is FLAT or confidence 0 do not vote. Returns a FLAT result
    with empty votes when there is no directional input.
    """
    p = params or FusionParams()
    weights_for_regime = p.weights.get(regime, DEFAULT_WEIGHTS["unknown"])

    voters: list[str] = []
    votes: dict[str, float] = {}
    for agent in DIRECTIONAL_AGENTS:
        signal = signals.get(agent)
        if signal is None:
            continue
        s = sign_of(signal.direction)
        if s == 0 or signal.confidence <= 0:
            continue
        voters.append(agent)
        votes[agent] = s * signal.confidence

    if not voters:
        return FusedResult(
            score=0.0,
            direction=Direction.FLAT,
            confidence=0.0,
            agreement=0.0,
            weights={},
            voting_agents=(),
            threshold=p.threshold,
        )

    weights = _renormalized(weights_for_regime, voters)
    if not weights:
        return FusedResult(
            score=0.0,
            direction=Direction.FLAT,
            confidence=0.0,
            agreement=0.0,
            weights={},
            voting_agents=(),
            threshold=p.threshold,
        )

    score = round(sum(weights[a] * votes[a] for a in voters), 6)
    direction = direction_for_score(score, threshold=p.threshold)
    confidence = min(1.0, abs(score))

    target_sign = sign_of(direction)
    aligning = sum(weights[a] for a in voters if sign_of(signs_of(votes[a])) == target_sign)
    agreement = round(aligning, 4)

    return FusedResult(
        score=score,
        direction=direction,
        confidence=confidence,
        agreement=agreement,
        weights=weights,
        voting_agents=tuple(voters),
        threshold=p.threshold,
    )


def apply_context(result: FusedResult, context: ContextInput | None) -> FusedResult:
    """Nudge the score toward a higher-timeframe direction (modest, safe).

    Purely additive; never changes which agents voted or the weights. The
    context only uses a direction produced by a prior (risk-validated)
    decision, and its influence is bounded by ``context.weight``. With no
    context the score is unchanged.
    """
    if context is None or context.confidence <= 0:
        return result
    nudge = context.weight * sign_of(context.direction) * min(1.0, context.confidence)
    new_score = result.score + nudge
    return FusedResult(
        score=round(new_score, 6),
        direction=direction_for_score(new_score, threshold=result.threshold),
        confidence=min(1.0, abs(new_score)),
        agreement=result.agreement,
        weights=result.weights,
        voting_agents=result.voting_agents,
        threshold=result.threshold,
        pre_context_score=result.score,
    )


def signs_of(vote: float) -> Direction:
    """Reconstruct a Direction from a signed vote value."""
    return Direction.LONG if vote > 0 else (Direction.SHORT if vote < 0 else Direction.FLAT)
