"""Инлайн-календарь для выбора даты публикации (без сторонних библиотек)."""
from __future__ import annotations

import calendar as _calendar
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CAL_PREFIX = "cal:"

# Окно публикации — первая половина дня
PUBLISH_START_MIN = 8 * 60  # 08:00
PUBLISH_END_MIN = 12 * 60  # 12:00 (обед)

# Эмодзи по статусу поста (все статусы CPT journal)
STATUS_EMOJI = {
    "publish": "📄",
    "future": "🟢",
    "draft": "📝",
    "pending": "⏳",
    "private": "🔒",
    "inherit": "📄",
}

LEGEND = " ".join(f"{STATUS_EMOJI[s]} {s}" for s in ("publish", "future", "draft", "pending", "private"))


def _status_emoji(status: str | None) -> str:
    return STATUS_EMOJI.get(status or "", "📄")


def build_calendar(
    year: int,
    month: int,
    day_status: dict[int, str],
    timezone: ZoneInfo,
    today: date | None = None,
    prefix: str = "cal",
) -> InlineKeyboardMarkup:
    """Сетка календаря на месяц.

    day_status — {день: статус поста} для этого месяца. Каждый статус — свой эмодзи
    (📄 published, 🟢 future, 📝 draft, ⏳ pending, 🔒 private).
    - Любой день выбираем (вперёд и назад). Прошлые дни с постами показывают эмодзи
      статуса, прошлые пустые — ✖️ (но оба выбираемы).
    - Сегодня/будущее с постами: эмодзи статуса. Свободные будущие дни: ⚪.
    Callback-формат: <prefix>:<action>:<year>:<month>:<day>, action ∈ {nav, day, ignore}
    """
    if today is None:
        today = datetime.now(timezone).date()
    cal = _calendar.Calendar()  # неделя начинается с понедельника
    rows: list[list[InlineKeyboardButton]] = []

    # Шапка: месяц год
    month_name = _calendar.month_name[month]
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows.append(
        [
            InlineKeyboardButton(
                f"‹ {_calendar.month_abbr[prev_month]}",
                callback_data=f"{prefix}:nav:{prev_year}:{prev_month}:0",
            ),
            InlineKeyboardButton(f"{month_name} {year}", callback_data=f"{prefix}:ignore:0:0:0"),
            InlineKeyboardButton(
                f"{_calendar.month_abbr[next_month]} ›",
                callback_data=f"{prefix}:nav:{next_year}:{next_month}:0",
            ),
        ]
    )

    # Дни недели
    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    rows.append([InlineKeyboardButton(w, callback_data=f"{prefix}:ignore:0:0:0") for w in weekday_names])

    for week in cal.monthdayscalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}:ignore:0:0:0"))
            else:
                cell_date = date(year, month, day)
                status = day_status.get(day)
                if status:
                    marker = _status_emoji(status)
                elif cell_date < today:
                    marker = "✖️"
                else:
                    marker = "⚪"
                callback = f"{prefix}:day:{year}:{month}:{day}"  # любой день выбираем (вперёд и назад)
                row.append(InlineKeyboardButton(f"{marker} {day}", callback_data=callback))
        rows.append(row)

    # Легенда
    rows.append([InlineKeyboardButton(LEGEND, callback_data=f"{prefix}:ignore:0:0:0")])

    return InlineKeyboardMarkup(rows)


def plan_publish_time(day: date, timezone: ZoneInfo, now: datetime | None = None) -> tuple[datetime, str]:
    """Планирование публикации: (datetime, status).

    Все статьи публикуются в первой половине дня (08:00–12:00).
    - День в будущем → случайное время в [08:00, 12:00), status=future.
    - Сегодня и уже после обеда (≥12:00) → публикуем сразу: time=now, status=publish.
    - Сегодня до обеда → случайное время в [08:00, 12:00); если оно уже прошло
      (время ≤ now) → publish сразу, иначе future на это время.
    """
    if now is None:
        now = datetime.now(timezone)
    if day == now.date():
        if now.hour * 60 + now.minute >= PUBLISH_END_MIN:
            # После обеда — публикуем немедленно
            return now.replace(second=0, microsecond=0), "publish"
        minutes = random.randint(PUBLISH_START_MIN, PUBLISH_END_MIN - 1)
        candidate = datetime.combine(day, time(0, 0), tzinfo=timezone) + timedelta(minutes=minutes)
        if candidate <= now:
            return now.replace(second=0, microsecond=0), "publish"
        return candidate, "future"
    minutes = random.randint(PUBLISH_START_MIN, PUBLISH_END_MIN - 1)
    return datetime.combine(day, time(0, 0), tzinfo=timezone) + timedelta(minutes=minutes), "future"


def format_calgary(dt: datetime) -> str:
    """ДД.ММ.ГГГГ ЧЧ:ММ Калгари"""
    return dt.strftime("%d.%m.%Y %H:%M Калгари")
