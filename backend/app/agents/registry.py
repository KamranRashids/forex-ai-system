"""Agent registry: stable lookup of available analysis agents."""

from __future__ import annotations

from app.agents.base import BaseAgent


class AgentRegistry:
    """Simple id -> agent map with duplicate protection."""

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        if not agent.id:
            raise ValueError("agent.id must be a non-empty string")
        if agent.id in self._agents:
            raise ValueError(f"agent id {agent.id!r} already registered")
        self._agents[agent.id] = agent

    def get(self, agent_id: str) -> BaseAgent | None:
        return self._agents.get(agent_id)

    def all(self) -> list[BaseAgent]:
        return list(self._agents.values())

    def __len__(self) -> int:
        return len(self._agents)


def default_registry() -> AgentRegistry:
    """Technical/regime (P3) + fundamental/sentiment (P4) agents."""
    from app.agents.fundamental import FundamentalAgent
    from app.agents.regime import RegimeAgent
    from app.agents.sentiment import SentimentAgent
    from app.agents.technical import TechnicalAgent

    registry = AgentRegistry()
    registry.register(TechnicalAgent())
    registry.register(RegimeAgent())
    registry.register(FundamentalAgent())
    registry.register(SentimentAgent())
    return registry
