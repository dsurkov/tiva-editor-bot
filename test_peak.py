"""Юнит-тесты гейта пиковых часов (границы)."""
from datetime import datetime

from peak import is_peak


def _utc(weekday: int, hour: int, minute: int = 0) -> datetime:
    # 2026-09-07 — понедельник
    base = datetime(2026, 9, 7, hour, minute)
    return base.replace(day=base.day + weekday)


def test_weekday_boundaries():
    # понедельник (0)
    assert is_peak(_utc(0, 0, 59)) is False
    assert is_peak(_utc(0, 1, 0)) is True
    assert is_peak(_utc(0, 3, 59)) is True
    assert is_peak(_utc(0, 4, 0)) is False
    assert is_peak(_utc(0, 5, 59)) is False
    assert is_peak(_utc(0, 6, 0)) is True
    assert is_peak(_utc(0, 9, 59)) is True
    assert is_peak(_utc(0, 10, 0)) is False
    assert is_peak(_utc(0, 12, 0)) is False
    assert is_peak(_utc(0, 23, 59)) is False


def test_weekend_never_peak():
    # суббота (5), воскресенье (6)
    assert is_peak(_utc(5, 8, 0)) is False
    assert is_peak(_utc(6, 8, 0)) is False
    assert is_peak(_utc(5, 2, 0)) is False
    assert is_peak(_utc(6, 2, 0)) is False


if __name__ == "__main__":
    test_weekday_boundaries()
    test_weekend_never_peak()
    print("is_peak: все границы OK")
