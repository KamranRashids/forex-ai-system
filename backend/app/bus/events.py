"""Event envelope shared by all producers/consumers on the bus."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.bus.topics import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class Event:
    """Versioned envelope; consumers must tolerate unknown extra fields."""

    event_type: str
    payload: dict[str, Any]
    producer: str
    produced_at: datetime
    schema_version: int = SCHEMA_VERSION
    #: Correlation id linking related events across pipeline stages.
    correlation_id: str = field(default="")

    def to_json(self) -> str:
        data = asdict(self)
        data["produced_at"] = self.produced_at.isoformat()
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Event:
        data = json.loads(raw)
        produced_at = datetime.fromisoformat(data["produced_at"])
        return cls(
            event_type=data["event_type"],
            payload=data["payload"],
            producer=data["producer"],
            produced_at=produced_at,
            schema_version=int(data.get("schema_version", 1)),
            correlation_id=data.get("correlation_id", ""),
        )
