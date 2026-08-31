"""Telegram-бот «Редакция»: сбор контента → AI-редактура → дата → публикация в WP.

Флоу:
- CONTENT: тексты + фото + файлы-картинки вперемешку, кнопка «✅ Finish Content»
  закреплена внизу (reply-клавиатура) и видна после каждого сообщения.
- EDIT: команда редактуры или «✅ Редактировать» → AI (выбирает раздел сам).
- DATE: календарь с отметками занятых дней (🟢 запланировано, 📄 опубликовано).
- Edit Article: календарь → день → список статей → правка/новый текст/удаление.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import time
from datetime import date, datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    TypeHandler,
    filters,
)

from ai_editor import AIError, PeakTimeError, edit_article, edit_existing
from auth import AuthStore
from config import Config, load_config
from publish_calendar import build_calendar, format_calgary, plan_publish_time
from wordpress import WordPressClient, WordPressError

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("bot")

# Состояния диалога
CONTENT, EDIT, DATE, SUBMIT = range(4)
EDAY, ELIST, EREQ, ECONFIRM = range(4, 8)

PEAK_MESSAGE = (
    "⚠️ Сейчас пиковые часы DeepSeek (01:00–04:00 и 06:00–10:00 UTC, пн–пт). "
    "Редактура/заголовок недоступны. Приходите в непиковое время."
)

MAX_PHOTOS = 3
FINISH_CONTENT = "✅ Finish Content"
FINISH_EDIT = "✅ Finish Edit"
NEW_ARTICLE = "📝 New Article"
EDIT_ARTICLE = "✏️ Edit Article"
JOURNAL_URL = "https://tivabeauty.ca/journal/"
CANCEL = "❌ Отмена"


def set_debug(enabled: bool) -> None:
    """Переключение уровня логов на лету (без рестарта)."""
    logging.getLogger("bot").setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.getLogger("wordpress").setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.getLogger("ai_editor").setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.getLogger("auth").setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.DEBUG if enabled else logging.WARNING)
    logger.info("Дебаг-логи %s", "включены" if enabled else "выключены")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует любые исключения из хендлеров (для отладки)."""
    logger.error("Исключение в хендлере: %s", context.error, exc_info=context.error)


# ---------- утилиты ----------

async def reply(update: Update, text: str, kb: InlineKeyboardMarkup | None = None):
    """Ответить в чат (работает и для сообщений, и для callback-запросов)."""
    if update.callback_query:
        await update.callback_query.answer()
        return await update.callback_query.message.reply_text(text, reply_markup=kb)
    return await update.effective_message.reply_text(text, reply_markup=kb)


def get_wp(context: ContextTypes.DEFAULT_TYPE) -> WordPressClient:
    return context.application.wp  # type: ignore[attr-defined]


async def get_services_cached(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[str, str]]:
    """Список услуг с кэшем на 1 час (не дёргать WP на каждый шаг)."""
    cache = context.bot_data.get("services_cache")
    now = time.time()
    if cache and now - cache[0] < 3600:
        logger.debug("get_services: из кэша (%d записей)", len(cache[1]))
        return cache[1]
    services = await get_wp(context).get_services()
    context.bot_data["services_cache"] = (now, services)
    logger.debug("get_services: свежая загрузка (%d записей)", len(services))
    return services


async def get_sections_cached(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[int, str]]:
    cache = context.bot_data.get("sections_cache")
    now = time.time()
    if cache and now - cache[0] < 3600:
        return cache[1]
    sections = await get_wp(context).get_sections()
    context.bot_data["sections_cache"] = (now, sections)
    return sections


async def get_day_status(
    context: ContextTypes.DEFAULT_TYPE, year: int, month: int
) -> dict[int, str]:
    """{день: статус поста} для месяца — реальные посты CPT journal во всех статусах."""
    try:
        dates = await get_wp(context).get_post_dates()
    except WordPressError as exc:
        logger.warning("get_post_dates failed: %s", exc)
        return {}
    prefix = f"{year:04d}-{month:02d}-"
    day_status: dict[int, str] = {}
    for d, status in dates:
        if not d.startswith(prefix):
            continue
        day = int(d[8:10])
        day_status[day] = status  # последний пост дня (WP вернёт по дате)
    return day_status


def _join_text(parts: list[str]) -> str:
    return "\n\n".join(p for p in parts if p.strip())


# ---------- авторизация ----------

async def auth_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Гейт (группа -1): авторизованных пропускает к хендлерам, неавторизованных блокирует.

    В PTB v21 в группе обрабатывается максимум 1 хендлер и дальше break — поэтому гейт
    вынесен в отдельную группу с приоритетом, а неавторизованных останавливает через
    ApplicationHandlerStop (иначе cmd_start/ConversationHandler не сработают).
    """
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    store = AuthStore()
    cfg: Config = context.bot_data["config"]
    store.ensure_admin_by_username(user.id, user.username, cfg.admin_usernames)

    if store.is_admin(user.id):
        logger.debug("auth: user %s (id %s) — админ, пропущен", user.username, user.id)
        return
    if store.is_allowed(user.id):
        logger.debug("auth: user %s (id %s) — разрешён, пропущен", user.username, user.id)
        return

    # Неавторизованный
    logger.info("auth: user %s (id %s) — нет доступа, обработка запроса", user.username, user.id)
    if store.is_pending(user.id):
        await reply(update, "Ваш запрос на доступ ещё не одобрен. Ожидайте.")
    else:
        store.add_pending(user.id, user.username)
        await reply(update, "Запрос на доступ отправлен администратору. Ожидайте одобрения.")
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Разрешить", callback_data=f"allow:{user.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{user.id}"),
                ]
            ]
        )
        nick = f"@{user.username}" if user.username else "(без ника)"
        for admin_id in store.list_admins():
            try:
                await context.bot.send_message(
                    admin_id, f"⚠️ Запрос доступа: {nick} (id {user.id})", reply_markup=kb
                )
            except Exception:  # noqa: BLE001 — админ мог заблокировать бота
                logger.warning("Не удалось уведомить админа %s", admin_id)
    raise ApplicationHandlerStop  # блокируем дальнейшие хендлеры


# ---------- админ-команды ----------

async def show_menu(message, logger_ctx: str = "") -> None:
    kb = ReplyKeyboardMarkup(
        [[NEW_ARTICLE, EDIT_ARTICLE]],
        resize_keyboard=True,
    )
    journal_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📂 Открыть Journal", url=JOURNAL_URL)]]
    )
    try:
        sent = await message.reply_text(
            "Привет! Я «Редакция» — помогу опубликовать статью в журнале TIVA BEAUTY.\n\n"
            "Выберите действие кнопками внизу.",
            reply_markup=journal_kb,
        )
        await message.reply_text(
            "Журнал — приоритетный раздел, сюда можно зайти в любой момент.",
            reply_markup=kb,
        )
        logger.info("cmd_start: reply sent id=%s", sent.message_id)
    except Exception:  # noqa: BLE001
        logger.exception("cmd_start: reply FAILED")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("cmd_start: user %s (id %s)", update.effective_user.username, update.effective_user.id)
    await show_menu(update.effective_message)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = AuthStore()
    if not store.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Нет доступа.")
        return
    arg = (context.args[0] if context.args else "").lower()
    if arg == "on":
        set_debug(True)
        await update.effective_message.reply_text("🔍 Дебаг-логи включены.")
    elif arg == "off":
        set_debug(False)
        await update.effective_message.reply_text("Дебаг-логи выключены.")
    else:
        await update.effective_message.reply_text("Использование: /debug on|off")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = AuthStore()
    if not store.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Нет доступа.")
        return
    allowed = store.list_allowed()
    pending = store.list_pending()
    lines = ["✅ Разрешённые:"]
    lines += [f"• id {uid}" for uid in allowed] or ["• (никого)"]
    lines.append("⏳ Ожидают:")
    lines += [f"• id {uid} (@{rec.get('username') or '—'})" for uid, rec in pending.items()] or ["• (никого)"]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = AuthStore()
    if not store.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Нет доступа.")
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /allow <id|@ник>")
        return
    uid = store.resolve_user(context.args[0])
    if uid is None:
        await update.effective_message.reply_text("Пользователь не найден.")
        return
    store.add_allowed(uid)
    logger.info("admin %s выдал доступ id %s", update.effective_user.id, uid)
    await update.effective_message.reply_text(f"✅ Доступ выдан: id {uid}")
    try:
        await context.bot.send_message(uid, "✅ Ваш доступ к боту одобрен!")
    except Exception:  # noqa: BLE001
        pass


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = AuthStore()
    if not store.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Нет доступа.")
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /remove <id|@ник>")
        return
    uid = store.resolve_user(context.args[0])
    if uid is None:
        await update.effective_message.reply_text("Пользователь не найден.")
        return
    store.remove_user(uid)
    logger.info("admin %s закрыл доступ id %s", update.effective_user.id, uid)
    await update.effective_message.reply_text(f"🚫 Доступ закрыт: id {uid}")
    try:
        await context.bot.send_message(uid, "🚫 Ваш доступ к боту закрыт.")
    except Exception:  # noqa: BLE001
        pass


async def cb_allow_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = AuthStore()
    if not store.is_admin(q.from_user.id):
        logger.warning("cb allow/deny: не-админ %s пытался нажать %s", q.from_user.id, q.data)
        await q.message.reply_text("Только администратор может это сделать.")
        return
    action, uid_str = q.data.split(":")
    uid = int(uid_str)
    logger.info("cb %s для id %s (нажал админ %s)", action, uid, q.from_user.id)
    if action == "allow":
        store.add_allowed(uid)
        await q.edit_message_text(f"✅ Доступ выдан: id {uid}")
        try:
            await context.bot.send_message(uid, "✅ Ваш доступ к боту одобрен!")
        except Exception:  # noqa: BLE001
            pass
    else:
        store.remove_user(uid)
        await q.edit_message_text(f"🚫 Доступ отклонён: id {uid}")
        try:
            await context.bot.send_message(uid, "🚫 Ваш запрос доступа отклонён.")
        except Exception:  # noqa: BLE001
            pass


async def cb_stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устаревшая inline-кнопка (диалог уже завершён/шаг сменился)."""
    q = update.callback_query
    logger.warning("STALE callback: %s от user %s", q.data, q.from_user.id)
    await q.answer("Это действие уже неактуально.", show_alert=False)


# ---------- новый пост: CONTENT (тексты + фото + файлы) ----------

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    logger.debug("user %s начал новую статью", update.effective_user.id)
    kb = ReplyKeyboardMarkup([[FINISH_CONTENT]], resize_keyboard=True)
    await update.effective_message.reply_text(
        "📝 Присылайте текст и фото в любом порядке, хоть несколько сообщений.\n"
        "Файлы-картинки тоже подойдут. Когда всё прислали — нажмите «✅ Finish Content».",
        reply_markup=kb,
    )
    return CONTENT


async def cb_new_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await cmd_new(update, context)


async def content_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    if text == FINISH_CONTENT:
        return await finish_content(update, context)
    if text in (FINISH_EDIT, NEW_ARTICLE, EDIT_ARTICLE, CANCEL):
        await update.effective_message.reply_text("Это действие сейчас неактивно.")
        return CONTENT
    parts = context.user_data.setdefault("article_text", [])
    parts.append(text)
    total = sum(len(p) for p in parts)
    await update.effective_message.reply_text(
        f"Текст принят (всего {total} симв.). Присылайте ещё или жмите «✅ Finish Content»."
    )
    return CONTENT


async def content_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await update.effective_message.reply_text(f"Максимум {MAX_PHOTOS} изображений — лишние не учитываются.")
        return CONTENT
    file_id = update.effective_message.photo[-1].file_id
    photos.append({"file_id": file_id, "mime": None})
    await update.effective_message.reply_text(
        f"Изображение {len(photos)}/{MAX_PHOTOS} принято (первое — обложка)."
    )
    return CONTENT


async def content_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("photos", [])
    doc = update.effective_message.document
    mime = doc.mime_type or ""
    if not mime.startswith("image/"):
        await update.effective_message.reply_text("Принимаются только файлы-картинки (JPG/PNG).")
        return CONTENT
    if len(photos) >= MAX_PHOTOS:
        await update.effective_message.reply_text(f"Максимум {MAX_PHOTOS} изображений — лишние не учитываются.")
        return CONTENT
    photos.append({"file_id": doc.file_id, "mime": mime})
    await update.effective_message.reply_text(
        f"Файл-картинка {len(photos)}/{MAX_PHOTOS} принята (первое — обложка)."
    )
    return CONTENT


async def finish_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = _join_text(context.user_data.get("article_text", []))
    if not text.strip():
        await update.effective_message.reply_text("Сначала пришлите текст статьи.")
        return CONTENT
    # Убираем reply-клавиатуру «Finish Content», дальше — inline-кнопки
    await update.effective_message.reply_text(
        "Готово, контент принят.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.effective_message.reply_text(
        "✍️ Отправьте команду для редактуры (например: «перелинкуй на услуги», "
        "«сделай короче») или нажмите «✅ Редактировать».",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Редактировать", callback_data="edit_run")]]
        ),
    )
    return EDIT


async def cb_edit_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _run_edit(update, context, context.user_data.get("edit_command"), force=False)


async def cb_regen_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _run_edit(update, context, context.user_data.get("edit_command"), force=True)


# Тексты кнопок, которые НЕ должны уходить в AI как команды (двойные нажатия,
# устаревшие кнопки, «New Article» посреди диалога)
BUTTON_TEXTS = {FINISH_CONTENT, FINISH_EDIT, NEW_ARTICLE, EDIT_ARTICLE, CANCEL}


async def edit_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.effective_message.text
    if command in BUTTON_TEXTS:
        await update.effective_message.reply_text("Это действие сейчас неактивно.")
        return EDIT
    return await _run_edit(update, context, command)


async def _run_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str | None, force: bool = False):
    # Двойное нажатие «Редактировать»/«Finish» с тем же командо-состоянием → перепоказ кэша
    cached = context.user_data.get("edit_preview_cache")
    if not force and cached and cached.get("command") == command:
        logger.debug("edit: повторное нажатие — показываю кэшированное превью")
        await reply(update, cached["text"], cached.get("kb"))
        return EDIT
    cfg: Config = context.bot_data["config"]
    text = _join_text(context.user_data.get("article_text", []))
    if not text.strip():
        await reply(update, "Текст статьи пуст — начните заново: /new")
        return EDIT
    logger.debug("edit: запуск DeepSeek (команда=%r, текст %d симв.)", command, len(text))
    try:
        services = await get_services_cached(context)
        sections = await get_sections_cached(context)
        result = await edit_article(text, command, services, sections, cfg.deepseek_api_key)
    except PeakTimeError:
        logger.info("edit: пиковые часы — отказ (user %s)", update.effective_user.id)
        await reply(update, PEAK_MESSAGE)
        return EDIT
    except AIError as exc:
        logger.error("edit: AIError: %s", exc)
        await reply(update, f"❌ Ошибка AI: {exc}")
        return EDIT
    except Exception as exc:  # noqa: BLE001 — сеть/парсинг
        logger.exception("edit_article failed")
        await reply(update, f"❌ Не удалось отредактировать: {exc}")
        return EDIT

    # Валидация выбранного раздела
    valid_ids = {sid for sid, _ in sections}
    section_id = result.get("section_id")
    if section_id not in valid_ids:
        logger.warning("AI вернул неверный section_id=%r, fallback на первый", section_id)
        section_id = sections[0][0]

    ud = context.user_data
    ud["title"] = result["title"]
    ud["seo_title"] = result["seo_title"]
    ud["seo_description"] = result["seo_description"]
    ud["edited_text"] = result["edited_text"]
    ud["changes"] = result.get("changes", [])
    ud["section_id"] = section_id
    ud["edit_command"] = command
    section_name = dict(sections).get(section_id, section_id)
    logger.info("edit: успех — заголовок «%s», текст %d симв., раздел %s",
                result["title"], len(result["edited_text"]), section_name)

    preview = (
        f"📝 Заголовок: {result['title']}\n"
        f"📂 Раздел: {section_name}\n\n"
        f"✏️ Изменения:\n" + "\n".join(f"• {c}" for c in result.get("changes", []))
        + f"\n\n🔍 SEO title: {result['seo_title']}\n"
        f"📄 SEO desc: {result['seo_description']}\n\n"
        f"Текст ({len(result['edited_text'])} симв.) готов."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_edit"),
                InlineKeyboardButton("🔄 Перегенерировать", callback_data="regen_edit"),
            ]
        ]
    )
    context.user_data["edit_preview_cache"] = {"command": command, "text": preview, "kb": kb}
    await reply(update, preview, kb)
    return EDIT


async def cb_confirm_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tz = context.bot_data["config"].timezone
    today = datetime.now(tz)
    year, month = today.year, today.month
    day_status = await get_day_status(context, year, month)
    kb = build_calendar(year, month, day_status, tz, today=today.date())
    await q.message.reply_text(
        "📅 Выберите дату публикации (📄 published, 🟢 future, 📝 draft, ⏳ pending, 🔒 private):",
        reply_markup=kb,
    )
    return DATE


async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action, year, month, day = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
    tz = context.bot_data["config"].timezone

    if action == "nav":
        day_status = await get_day_status(context, year, month)
        kb = build_calendar(year, month, day_status, tz)
        await q.edit_message_reply_markup(reply_markup=kb)
        return DATE

    if action == "ignore":
        return DATE

    # action == "day"
    chosen = date(year, month, day)
    publish_dt, status = plan_publish_time(chosen, tz)
    context.user_data["date_iso"] = publish_dt.isoformat()
    context.user_data["status"] = status
    logger.info("user %s выбрал дату %s (status=%s)", q.from_user.id, publish_dt.isoformat(), status)
    await q.edit_message_text(f"📅 Выбрано: {format_calgary(publish_dt)}\n⏳ Публикую…")
    return await do_submit(update, context)


async def do_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    cfg: Config = context.bot_data["config"]
    wp = get_wp(context)
    try:
        cover_id: int | None = None
        extra_urls: list[str] = []
        photos = ud.get("photos", [])
        logger.debug("submit: загрузка %d изображений в WP", len(photos))
        for i, item in enumerate(photos):
            f = await context.bot.get_file(item["file_id"])
            data = await f.download_as_bytearray()
            ext = os.path.splitext(f.file_path or "")[1] or ".jpg"
            mime = item.get("mime") or mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"
            mid, url = await wp.upload_media(bytes(data), f"article_{i}{ext}", mime)
            logger.debug("submit: изображение %d → media id %s", i, mid)
            if i == 0:
                cover_id = mid
            else:
                extra_urls.append(url)

        content = ud["edited_text"] + "".join(f'<p><img src="{u}" alt=""></p>' for u in extra_urls)
        post_id, link = await wp.create_post(
            title=ud["title"],
            content=content,
            status=ud["status"],
            date_iso=ud["date_iso"],
            section_id=ud["section_id"],
            featured_media_id=cover_id,
            seo_title=ud["seo_title"],
            seo_description=ud["seo_description"],
        )
        logger.info("submit: пост создан id=%s status=%s link=%s", post_id, ud["status"], link)
    except WordPressError as exc:
        logger.error("submit: WordPressError: %s", exc)
        await reply(
            update,
            f"❌ Ошибка WordPress: {exc}\nЧерновик сохранён — данные не потеряны.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Повторить публикацию", callback_data="retry_submit")]]
            ),
        )
        return SUBMIT
    except Exception as exc:  # noqa: BLE001
        logger.exception("submit failed")
        await reply(
            update,
            f"❌ Ошибка публикации: {exc}\nЧерновик сохранён.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Повторить публикацию", callback_data="retry_submit")]]
            ),
        )
        return SUBMIT

    # Успех
    changes = "\n".join(f"• {c}" for c in ud.get("changes", []))
    article_url = link if ud["status"] == "publish" else f"{cfg.wp_base_url}/wp-admin/post.php?post={post_id}&action=edit"
    result_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐 Перейти к статье", url=article_url)],
            [InlineKeyboardButton("📂 Открыть Journal", url=JOURNAL_URL)],
        ]
    )
    menu_kb = ReplyKeyboardMarkup([[NEW_ARTICLE, EDIT_ARTICLE]], resize_keyboard=True)
    if ud["status"] == "future":
        await reply(
            update,
            f"✅ Статья запланирована на {format_calgary(datetime.fromisoformat(ud['date_iso']))}.\n\n"
            f"Изменения:\n{changes}",
            kb=result_kb,
        )
    else:
        await reply(
            update,
            f"✅ Статья опубликована!\n\nИзменения:\n{changes}",
            kb=result_kb,
        )
    if update.callback_query:
        await update.callback_query.message.reply_text("Меню:", reply_markup=menu_kb)
    else:
        await update.effective_message.reply_text("Меню:", reply_markup=menu_kb)
    ud.clear()
    return ConversationHandler.END


async def retry_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await do_submit(update, context)


# ---------- Edit Article: календарь → день → статья → правка ----------

async def cmd_edit_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.effective_message
    tz = context.bot_data["config"].timezone
    today = datetime.now(tz)
    year, month = today.year, today.month
    day_status = await get_day_status(context, year, month)
    kb = build_calendar(
        year, month, day_status, tz, today=today.date(), prefix="ecal",
    )
    await msg.reply_text(
        "✏️ Выберите день: покажу статьи этого дня.",
        reply_markup=kb,
    )
    return EDAY


async def ecal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action, year, month, day = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
    tz = context.bot_data["config"].timezone

    if action == "nav":
        day_status = await get_day_status(context, year, month)
        kb = build_calendar(year, month, day_status, tz, prefix="ecal")
        await q.edit_message_reply_markup(reply_markup=kb)
        return EDAY

    if action == "ignore":
        return EDAY

    chosen_day = f"{year:04d}-{month:02d}-{day:02d}"
    try:
        articles = await get_wp(context).get_articles_on_day(chosen_day)
    except WordPressError as exc:
        await q.message.reply_text(f"❌ Не удалось загрузить статьи: {exc}")
        return EDAY
    if not articles:
        await q.message.reply_text("В этот день нет статей. Выберите другой день.")
        return EDAY
    cfg: Config = context.bot_data["config"]
    rows: list[list[InlineKeyboardButton]] = []
    for a in articles:
        url = a.get("link")
        if not url:
            url = f"{cfg.wp_base_url}/?post_type=journal&p={a['id']}"
        rows.append(
            [
                InlineKeyboardButton(
                    f"✏️ {a['date'][11:16]} · {a['status']} · {a['title'][:35]}",
                    callback_data=f"epick:{a['id']}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("🌐 Открыть в браузере", url=url),
            ]
        )
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="ecancel")])
    kb = InlineKeyboardMarkup(rows)
    await q.message.reply_text(
        f"📄 Статьи за {chosen_day}: нажмите ✏️ чтобы редактировать, 🌐 чтобы открыть.",
        reply_markup=kb,
    )
    return ELIST


async def epick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    logger.info("epick callback: %s (user %s)", q.data, q.from_user.id)
    await q.answer()
    post_id = int(q.data.split(":")[1])
    try:
        post = await get_wp(context).get_post_raw(post_id)
    except WordPressError as exc:
        await q.message.reply_text(f"❌ Не удалось загрузить статью: {exc}")
        return ELIST
    context.user_data["edit_post_id"] = post_id
    context.user_data["edit_post"] = post
    kb = ReplyKeyboardMarkup([[FINISH_EDIT, CANCEL]], resize_keyboard=True)
    await q.message.reply_text(
        "✏️ Что исправить в статье? Пришлите текст, фото или комментарий.\n"
        "Можно прислать целиком новый текст и фото. Для удаления напишите «удали статью».\n"
        "Готово — «✅ Finish Edit».",
        reply_markup=kb,
    )
    return EREQ


async def ereq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    if text == FINISH_EDIT:
        return await finish_edit(update, context)
    if text == CANCEL:
        context.user_data.pop("edit_post_id", None)
        context.user_data.pop("edit_post", None)
        await update.effective_message.reply_text(
            "Отменено.", reply_markup=ReplyKeyboardMarkup([[NEW_ARTICLE, EDIT_ARTICLE]], resize_keyboard=True)
        )
        return ConversationHandler.END
    if text in (NEW_ARTICLE, EDIT_ARTICLE):
        await update.effective_message.reply_text("Это действие сейчас неактивно.")
        return EREQ
    parts = context.user_data.setdefault("edit_text", [])
    parts.append(text)
    await update.effective_message.reply_text("Принято. Ещё или «✅ Finish Edit».")
    return EREQ


async def ereq_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("edit_photos", [])
    if len(photos) >= MAX_PHOTOS:
        await update.effective_message.reply_text(f"Максимум {MAX_PHOTOS} изображений.")
        return EREQ
    file_id = update.effective_message.photo[-1].file_id
    photos.append({"file_id": file_id, "mime": None})
    await update.effective_message.reply_text(f"Фото {len(photos)}/{MAX_PHOTOS} принято.")
    return EREQ


async def ereq_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("edit_photos", [])
    doc = update.effective_message.document
    mime = doc.mime_type or ""
    if not mime.startswith("image/"):
        await update.effective_message.reply_text("Принимаются только файлы-картинки.")
        return EREQ
    if len(photos) >= MAX_PHOTOS:
        await update.effective_message.reply_text(f"Максимум {MAX_PHOTOS} изображений.")
        return EREQ
    photos.append({"file_id": doc.file_id, "mime": mime})
    await update.effective_message.reply_text(f"Файл-картинка {len(photos)}/{MAX_PHOTOS} принята.")
    return EREQ


async def finish_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    instruction = _join_text(ud.get("edit_text", []))
    post = ud.get("edit_post")
    if not post:
        await update.effective_message.reply_text("Сначала выберите статью.")
        return EREQ
    if not instruction.strip() and not ud.get("edit_photos"):
        await update.effective_message.reply_text(
            "Сначала напишите, что исправить (или пришлите новые текст/фото)."
        )
        return EREQ

    # Удаление по запросу
    low = instruction.lower()
    if any(w in low for w in ("удали статью", "delete", "remove this article")):
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🗑 Да, удалить", callback_data=f"edel:{post['id']}"),
                    InlineKeyboardButton("❌ Нет", callback_data="ecancel"),
                ]
            ]
        )
        await update.effective_message.reply_text("Точно удалить эту статью?", reply_markup=kb)
        return ECONFIRM

    cfg: Config = context.bot_data["config"]
    current_text = post.get("content", {}).get("raw", "")
    current_title = post.get("title", {}).get("raw", "")
    try:
        services = await get_services_cached(context)
        sections = await get_sections_cached(context)
        result = await edit_existing(
            current_text, current_title, instruction, services, sections, cfg.deepseek_api_key
        )
    except PeakTimeError:
        await reply(update, PEAK_MESSAGE)
        return EREQ
    except AIError as exc:
        logger.error("edit_existing: AIError: %s", exc)
        await reply(update, f"❌ Ошибка AI: {exc}")
        return EREQ
    except Exception as exc:  # noqa: BLE001
        logger.exception("edit_existing failed")
        await reply(update, f"❌ Не удалось применить правку: {exc}")
        return EREQ

    valid_ids = {sid for sid, _ in sections}
    section_id = result.get("section_id")
    if section_id not in valid_ids:
        section_id = sections[0][0]

    ud["edit_result"] = result
    ud["edit_section_id"] = section_id
    preview = (
        f"📝 Заголовок: {result['title']}\n\n"
        f"✏️ Изменения:\n" + "\n".join(f"• {c}" for c in result.get("changes", []))
        + f"\n\nТекст ({len(result['edited_text'])} симв.) готов."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Применить", callback_data="eapply"),
                InlineKeyboardButton("🔄 Перегенерировать", callback_data="eregen"),
            ]
        ]
    )
    await reply(update, preview, kb)
    return ECONFIRM


async def eapply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ud = context.user_data
    post_id = ud.get("edit_post_id")
    result = ud.get("edit_result")
    if not post_id or not result:
        await q.message.reply_text("Данные правки потеряны. Начните заново: Edit Article.")
        return ConversationHandler.END
    wp = get_wp(context)
    try:
        # Новые фото: первое — обложка, остальные — в конец контента
        cover_id: int | None = None
        extra_urls: list[str] = []
        for i, item in enumerate(ud.get("edit_photos", [])):
            f = await context.bot.get_file(item["file_id"])
            data = await f.download_as_bytearray()
            ext = os.path.splitext(f.file_path or "")[1] or ".jpg"
            mime = item.get("mime") or mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"
            mid, url = await wp.upload_media(bytes(data), f"edit_{i}{ext}", mime)
            if i == 0:
                cover_id = mid
            else:
                extra_urls.append(url)

        content = result["edited_text"] + "".join(f'<p><img src="{u}" alt=""></p>' for u in extra_urls)
        body: dict = {
            "title": result["title"],
            "content": content,
            "article-section": [ud["edit_section_id"]],
            "meta": {
                "slim_seo": {
                    "title": result["seo_title"],
                    "description": result["seo_description"],
                },
                "_slim_seo_primary_term_article-section": ud["edit_section_id"],
            },
        }
        if cover_id is not None:
            body["featured_media"] = cover_id
        pid, link = await wp.update_post(post_id, body)
        logger.info("edit: пост %s обновлён → %s", pid, link)
    except WordPressError as exc:
        logger.error("edit apply: WordPressError: %s", exc)
        await q.message.reply_text(f"❌ Ошибка WordPress: {exc}")
        return ECONFIRM
    except Exception as exc:  # noqa: BLE001
        logger.exception("edit apply failed")
        await q.message.reply_text(f"❌ Ошибка сохранения: {exc}")
        return ECONFIRM

    await q.message.reply_text(
        f"✅ Статья обновлена: {link}",
        reply_markup=ReplyKeyboardMarkup([[NEW_ARTICLE, EDIT_ARTICLE]], resize_keyboard=True),
    )
    ud.clear()
    return ConversationHandler.END


async def eregen_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await finish_edit(update, context)


async def edel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    post_id = int(q.data.split(":")[1])
    try:
        ok = await get_wp(context).delete_post(post_id)
        logger.info("edit: пост %s удалён ok=%s", post_id, ok)
    except WordPressError as exc:
        await q.message.reply_text(f"❌ Ошибка удаления: {exc}")
        return ECONFIRM
    await q.message.reply_text(
        "🗑 Статья удалена.",
        reply_markup=ReplyKeyboardMarkup([[NEW_ARTICLE, EDIT_ARTICLE]], resize_keyboard=True),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def ecancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "Отменено.",
        reply_markup=ReplyKeyboardMarkup([[NEW_ARTICLE, EDIT_ARTICLE]], resize_keyboard=True),
    )
    context.user_data.clear()
    return ConversationHandler.END


# ---------- общие ----------

async def cmd_start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start посреди диалога: показать меню и сбросить состояние."""
    logger.info("cmd_start_fallback (mid-flow reset): user %s", update.effective_user.id)
    await show_menu(update.effective_message)
    context.user_data.clear()
    return ConversationHandler.END


async def unexpected_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст/фото/файл в неожиданном шаге — даём подсказку, состояние сохраняем."""
    await update.effective_message.reply_text(
        "На этом шаге это не нужно. /cancel — отмена, /new — начать заново."
    )
    return None  # состояние диалога не меняем


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Отменено. Черновик удалён.",
        reply_markup=ReplyKeyboardMarkup([[NEW_ARTICLE, EDIT_ARTICLE]], resize_keyboard=True),
    )
    return ConversationHandler.END


# ---------- сборка ----------

async def post_init(application: Application) -> None:
    cfg = load_config()
    application.bot_data["config"] = cfg
    application.wp = WordPressClient(cfg.wp_base_url, cfg.wp_username, cfg.wp_application_password)  # type: ignore[attr-defined]
    logging.getLogger("bot").setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    logging.getLogger("wordpress").setLevel(logging.DEBUG if cfg.log_level.upper() == "DEBUG" else logging.INFO)
    logging.getLogger("ai_editor").setLevel(logging.DEBUG if cfg.log_level.upper() == "DEBUG" else logging.INFO)
    logging.getLogger("auth").setLevel(logging.DEBUG if cfg.log_level.upper() == "DEBUG" else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.DEBUG if cfg.log_level.upper() == "DEBUG" else logging.WARNING)
    logger.info("Бот «Редакция» запущен (LOG_LEVEL=%s)", cfg.log_level)


async def post_shutdown(application: Application) -> None:
    wp = getattr(application, "wp", None)
    if wp is not None:
        await wp.close()


def main() -> None:
    cfg = load_config()
    persistence = PicklePersistence(filepath="data/state.pickle")
    app = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Гейт авторизации — отдельная группа с приоритетом (до группы 0)
    app.add_handler(TypeHandler(Update, auth_gate), group=-1)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", cmd_new),
            MessageHandler(filters.Text([NEW_ARTICLE]), cmd_new),
            CallbackQueryHandler(cb_new_article, pattern="^new_article$"),
            CommandHandler("edit", cmd_edit_article),
            MessageHandler(filters.Text([EDIT_ARTICLE]), cmd_edit_article),
        ],
        states={
            CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, content_text_handler),
                MessageHandler(filters.PHOTO, content_photo_handler),
                MessageHandler(filters.Document.IMAGE, content_document_handler),
            ],
            EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_command_handler),
                CallbackQueryHandler(cb_edit_run, pattern="^edit_run$"),
                CallbackQueryHandler(cb_confirm_edit, pattern="^confirm_edit$"),
                CallbackQueryHandler(cb_regen_edit, pattern="^regen_edit$"),
            ],
            DATE: [CallbackQueryHandler(calendar_handler, pattern="^cal:")],
            SUBMIT: [CallbackQueryHandler(retry_submit, pattern="^retry_submit$")],
            EDAY: [CallbackQueryHandler(ecal_handler, pattern="^ecal:")],
            ELIST: [
                CallbackQueryHandler(epick_handler, pattern="^epick:\\d+$"),
                CallbackQueryHandler(ecancel_handler, pattern="^ecancel$"),
            ],
            EREQ: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ereq_handler),
                MessageHandler(filters.PHOTO, ereq_photo_handler),
                MessageHandler(filters.Document.IMAGE, ereq_document_handler),
            ],
            ECONFIRM: [
                CallbackQueryHandler(eapply_handler, pattern="^eapply$"),
                CallbackQueryHandler(eregen_handler, pattern="^eregen$"),
                CallbackQueryHandler(edel_handler, pattern="^edel:\\d+$"),
                CallbackQueryHandler(ecancel_handler, pattern="^ecancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("new", cmd_new),
            CommandHandler("start", cmd_start_fallback),
            MessageHandler(filters.ALL & ~filters.COMMAND, unexpected_input),
        ],
        name="article_flow",
        persistent=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(cb_allow_deny, pattern="^(allow|deny):\\d+$"))
    # Любой другой callback (устаревшая кнопка) — ответить, чтобы не крутился спиннер
    app.add_handler(CallbackQueryHandler(cb_stale_callback))

    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
