"""Гейт пиковых часов DeepSeek (по UTC — льготный тариф API, не локальное время).

Применяется только к DeepSeek-моделям: в пик запросы уходят на fallback-модель.
"""
from __future__ import annotations

from datetime import datetime


def is_peak(now_utc: datetime) -> bool:
    """Пик: пн–пт, [01:00,04:00) и [06:00,10:00) UTC."""
    if now_utc.weekday() >= 5:  # суббота=5, воскресенье=6
        return False
    h = now_utc.hour
    return (1 <= h <= 3) or (6 <= h <= 9)
