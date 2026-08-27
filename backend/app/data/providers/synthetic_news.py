"""Deterministic synthetic news provider (default; zero-key)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.data.content_types import NormalizedNewsItem
from app.data.providers.synthetic_content import synthetic_news_items


class SyntheticNewsProvider:
    """Adapts the deterministic generator to the NewsProvider interface."""

    name = "synthetic"

    async def fetch_news(
        self,
        *,
        since: datetime,
        until: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[NormalizedNewsItem]:
        return synthetic_news_items(since, until)

    async def aclose(self) -> None:
        return None
