"""Юнит-тесты инлайн-календаря и планирования публикации."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from publish_calendar import build_calendar, format_calgary, plan_publish_time

TZ = ZoneInfo("America/Edmonton")
FIXED_TODAY = date(2026, 9, 7)


def test_calendar_structure():
    markup = build_calendar(2026, 9, {15: "future"}, TZ, today=FIXED_TODAY)
    rows = markup.inline_keyboard
    # шапка + дни недели + недели + легенда
    assert len(rows) >= 8
    # первый ряд: prev / месяц-год / next
    header = rows[0]
    assert header[1].text == "September 2026"
    assert header[0].callback_data == "cal:nav:2026:8:0"
    assert header[2].callback_data == "cal:nav:2026:10:0"


def test_calendar_marked_day():
    markup = build_calendar(2026, 9, {15: "future"}, TZ, today=FIXED_TODAY)
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any(t == "🟢 15" for t in texts)
    assert any(t == "⚪ 20" for t in texts)


def test_calendar_published_day():
    markup = build_calendar(2026, 9, {18: "publish", 15: "future"}, TZ, today=FIXED_TODAY)
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any(t == "📄 18" for t in texts)
    assert any(t == "🟢 15" for t in texts)


def test_calendar_status_emojis():
    markup = build_calendar(
        2026, 9, {5: "draft", 6: "pending", 7: "private", 8: "future", 9: "publish"},
        TZ, today=FIXED_TODAY,
    )
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any(t == "📝 5" for t in texts)
    assert any(t == "⏳ 6" for t in texts)
    assert any(t == "🔒 7" for t in texts)
    assert any(t == "🟢 8" for t in texts)
    assert any(t == "📄 9" for t in texts)


def test_calendar_past_day_selectable():
    # Прошлые дни (с постом или без) — выбираемы (day), вперёд и назад
    markup = build_calendar(2026, 9, {1: "publish", 3: "draft"}, TZ, today=FIXED_TODAY)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    day1 = next(b for b in buttons if b.text == "📄 1")
    day3 = next(b for b in buttons if b.text == "📝 3")
    assert day1.callback_data.startswith("cal:day:")
    assert day3.callback_data.startswith("cal:day:")


def test_calendar_past_empty_day_x_selectable():
    markup = build_calendar(2026, 9, {1: "publish"}, TZ, today=FIXED_TODAY)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    # 2 сентября без поста — прошлый → ✖️, но выбираем
    day2 = next(b for b in buttons if b.text == "✖️ 2")
    assert day2.callback_data.startswith("cal:day:")


def test_calendar_custom_prefix():
    markup = build_calendar(2026, 9, {15: "future"}, TZ, today=FIXED_TODAY, prefix="ecal")
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    day15 = next(b for b in buttons if " 15" in b.text)
    assert day15.callback_data.startswith("ecal:day:")


def test_calendar_counts_mode():
    # режим отчёта: только ✍️ на днях работы, остальные дни — просто цифра (без эмодзи)
    markup = build_calendar(2026, 9, {}, TZ, today=FIXED_TODAY, counts={5: 14, 12: 3})
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "✍️ 5" in texts  # 5 сентября: работали → эмодзи
    assert "✍️ 12" in texts  # 12 сентября: работали → эмодзи
    assert any(t.strip() == "20" for t in texts)  # день без постов — просто цифра
    assert not any(t == "⚪ 20" for t in texts)  # без эмодзи ⚪


def test_calendar_legend():
    markup = build_calendar(2026, 9, {}, TZ, today=FIXED_TODAY)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    legend = next(b for b in buttons if "publish" in b.text and "future" in b.text)
    assert "draft" in legend.text and "pending" in legend.text


def test_plan_future_day_morning_window():
    # Будущий день → случайное время строго в [08:00, 12:00), status future
    d = date(2026, 9, 20)
    for _ in range(300):
        dt, status = plan_publish_time(d, TZ)
        assert status == "future"
        minutes = dt.hour * 60 + dt.minute
        assert 8 * 60 <= minutes < 12 * 60


def test_plan_today_after_noon_publish_now():
    # Сегодня, 14:00 → публикуем сразу (publish), время ≈ now
    now = datetime(2026, 9, 7, 14, 30, tzinfo=TZ)
    dt, status = plan_publish_time(FIXED_TODAY, TZ, now=now)
    assert status == "publish"
    assert dt == now.replace(second=0, microsecond=0)


def test_plan_today_morning_future_or_publish():
    # Сегодня, 09:00 → кандидат в [08:00,12:00); если он уже прошёл → publish, иначе future
    now = datetime(2026, 9, 7, 9, 0, tzinfo=TZ)
    for _ in range(300):
        dt, status = plan_publish_time(FIXED_TODAY, TZ, now=now)
        assert dt.date() == FIXED_TODAY
        minutes = dt.hour * 60 + dt.minute
        assert 8 * 60 <= minutes <= 12 * 60
        if status == "future":
            assert dt > now
        else:
            assert dt <= now


def test_format_calgary():
    dt = datetime(2026, 9, 20, 14, 5, tzinfo=TZ)
    assert format_calgary(dt) == "20.09.2026 14:05 Калгари"


if __name__ == "__main__":
    test_calendar_structure()
    test_calendar_marked_day()
    test_calendar_published_day()
    test_calendar_status_emojis()
    test_calendar_past_day_selectable()
    test_calendar_past_empty_day_x_selectable()
    test_calendar_counts_mode()
    test_calendar_custom_prefix()
    test_calendar_legend()
    test_plan_future_day_morning_window()
    test_plan_today_after_noon_publish_now()
    test_plan_today_morning_future_or_publish()
    test_format_calgary()
    print("calendar: все тесты OK")
