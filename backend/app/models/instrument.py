"""Tradable instrument universe (FX spot pairs)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def pip_size_for(symbol: str) -> Decimal:
    """Standard pip size: 0.01 for JPY quote pairs, else 0.0001."""
    return Decimal("0.01") if symbol.endswith("JPY") else Decimal("0.0001")


def price_decimals_for(symbol: str) -> int:
    """Fractional digits used when storing prices for the pair."""
    return 3 if symbol.endswith("JPY") else 5


class Instrument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    base: Mapped[str] = mapped_column(String(8), nullable=False)
    quote: Mapped[str] = mapped_column(String(8), nullable=False)
    pip_size: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    price_decimals: Mapped[int] = mapped_column(default=5, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Instrument {self.symbol}>"
