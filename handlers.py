import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden

from config import (
    CHANNEL_ID, CHANNEL_LINK, ADMIN_IDS, SOCIAL_LINKS,
    SUPPORT_NAME, SUPPORT_COOLDOWN_MINUTES, SUPPORT_DAILY_LIMIT,
    TICKETS_PER_PAGE,
)
import database as db
import channel_scanner
import faq as faq_data

logger = logging.getLogger(__name__)

# Кнопки Reply-клавиатуры — при их нажатии в режиме поддержки
# сбрасываем режим ввода, не отправляем текст в тикет.
KEYBOARD_BUTTONS = {"🔄 Перезапуск", "🆘 Помощь", "📖 Справка"}

# Количество блоков на странице раздела справки
FAQ_ITEMS_PER_PAGE = 5

# Минимальная длина темы вопроса
SUBJECT_MIN_LEN = 3


# ===========================================================================
# Вспомогательные функции — главное меню
# ===========================================================================

def get_main_menu_keyboard(user_id: int, exclude: str = None) -> InlineKeyboardMarkup:
    notif_status = db.get_notification_status(user_id)
    notif_text = "🔕 Выключить уведомления" if notif_status else "🔔 Включить уведомления"

    if exclude == "about":
        notif_callback = "toggle_notify_about"
    elif exclude == "links":
        notif_callback = "toggle_notify_links"
    else:
        notif_callback = "toggle_notify"

    keyboard = []
    if exclude != "about":
        keyboard.append([InlineKeyboardButton("📊 О боте", callback_data="about")])
    if exclude != "links":
        keyboard.append([InlineKeyboardButton("🔗 Соцсети и сайт", callback_data="links")])
    keyboard.append([InlineKeyboardButton(notif_text, callback_data=notif_callback)])
    if exclude is not None:
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])

    return InlineKeyboardMarkup(keyboard)


def get_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🔄 Перезапуск"), KeyboardButton("🆘 Помощь")],
        [KeyboardButton("📖 Справка")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


# ===========================================================================
# Вспомогательные функции — раздел помощи
# ===========================================================================

def _build_help_menu(user_id: int) -> tuple:
    """
    Возвращает (text, InlineKeyboardMarkup) для меню раздела поддержки.
    Вид меню зависит от того, есть ли открытый тикет у пользователя.
    Используется как при первой отправке, так и при возврате из листинга тикетов.
    """
    ticket = db.get_open_ticket(user_id)

    if ticket:
        # ── Есть открытый тикет — показываем его состояние ──
        subject = ticket["subject"] or "—"
        messages = db.get_last_ticket_messages(ticket["id"], n=1)
        last_line = ""
        if messages:
            m = messages[0]
            prefix = "Вы" if m["sender_type"] == "user" else SUPPORT_NAME
            preview = m["text"][:150] + ("..." if len(m["text"]) > 150 else "")
            last_line = f"\n\nПоследнее сообщение:\n<b>{prefix}:</b> {preview}"

        text = (
            f"📋 <b>Тикет #{ticket['id']:04d} — {subject}</b> — открыт"
            f"{last_line}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📝 Написать",
                    callback_data=f"support_reply:{ticket['id']}"
                ),
                InlineKeyboardButton(
                    "✅ Закрыть тикет",
                    callback_data=f"user_close:{ticket['id']}"
                ),
            ],
            [InlineKeyboardButton("📋 Тикеты", callback_data="tlist:0")],
            [InlineKeyboardButton("🔙 Назад", callback_data="help_back")],
        ])
    else:
        # ── Нет открытого тикета — стандартное меню поддержки ──
        text = (
            "🆘 <b>Техническая поддержка</b>\n\n"
            "Здесь вы можете связаться с командой MrX.\n"
            "Мы читаем все обращения и отвечаем как можно быстрее."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Написать сообщение", callback_data="help_new")],
            [InlineKeyboardButton("📋 Тикеты", callback_data="tlist:0")],
            [InlineKeyboardButton("🔙 Назад", callback_data="help_back")],
        ])

    return text, keyboard


def _format_ticket_header(ticket: dict) -> str:
    """Возвращает строку-заголовок тикета вида «#0001 — Тема»."""
    subject = ticket["subject"] or "Без темы"
    return f"#{ticket['id']:04d} — {subject}"


# ===========================================================================
# Проверка подписки
# ===========================================================================

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ]
    except BadRequest as e:
        logger.warning(f"Ошибка проверки подписки: {e}")
        return False


async def require_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if await check_subscription(user_id, context):
        return True
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
    ])
    text = "❌ Кажется, вы покинули канал. Для доступа к боту, пожалуйста, подпишитесь снова."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)
    return False


# ===========================================================================
# /start и проверка подписки
# ===========================================================================

WELCOME_TEXT = (
    "Добро пожаловать в официальный бот MrX 🔴\n\n"
    "Здесь ты всегда найдёшь актуальную информацию о мессенджере, "
    "первым узнаешь о новых обновлениях и сможешь получить помощь от команды.\n\n"
    "Что умеет этот бот?\n"
    "📢 Важные новости и анонсы прямо сюда\n"
    "🔧 Техническая поддержка — опиши проблему, разберёмся\n"
    "📖 Вся информация о проекте в одном месте\n"
    "⚡️ Быстрая связь с командой MrX\n\n"
    "Если у тебя вопрос, баг или просто хочешь написать — смело пиши. Читаем всё.\n\n"
    "🌐 Сайт: redmrxgram.work.gd\n"
    "✈️ Канал: t.me/redmrxgram\n\n"
    "☀️ Sic erat, sic est, sic erit."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    db.add_user(user_id)

    # Сбрасываем все состояния поддержки
    db.set_support_mode(user_id, 0)
    context.user_data.pop("pending_topic", None)
    context.user_data.pop("pending_reply", None)

    if await check_subscription(user_id, context):
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!",
            reply_markup=get_reply_keyboard(user_id)
        )
        await update.message.reply_text(
            WELCOME_TEXT,
            reply_markup=get_main_menu_keyboard(user_id),
            disable_web_page_preview=True
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        ])
        await update.message.reply_text(
            "👋 Привет! Для доступа к этому боту, пожалуйста, подпишитесь на наш канал.",
            reply_markup=keyboard
        )


async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if await check_subscription(user_id, context):
        await query.edit_message_text("✅ Подписка подтверждена!\n🔓 Доступ к боту открыт.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Используйте кнопки ниже для навигации:",
            reply_markup=get_reply_keyboard(user_id)
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=WELCOME_TEXT,
            reply_markup=get_main_menu_keyboard(user_id),
            disable_web_page_preview=True
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
        ])
        await query.edit_message_text(
            "❌ Подписка не засчитана, пожалуйста, попробуйте ещё раз.",
            reply_markup=keyboard
        )


# ===========================================================================
# Центральный обработчик текстовых сообщений
# ===========================================================================

async def handle_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text

    # ── 1. Администратор в режиме ответа на тикет ────────────────────────────
    if user_id in ADMIN_IDS:
        pending = context.user_data.get("pending_reply")
        if pending:
            await _handle_admin_reply_input(update, context, pending)
            return

    # ── 2. Проверка подписки (только для обычных пользователей) ──────────────
    if user_id not in ADMIN_IDS:
        if not await check_subscription(user_id, context):
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)],
                [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")],
            ])
            await update.message.reply_text(
                "❌ Кажется, вы покинули канал. Для доступа к боту нужна подписка.",
                reply_markup=keyboard
            )
            return

    # ── 3. Режим поддержки ────────────────────────────────────────────────────
    support_mode = db.get_support_mode(user_id)
    if support_mode > 0:
        if text in KEYBOARD_BUTTONS:
            # Пользователь нажал кнопку меню — отменяем ввод
            db.set_support_mode(user_id, 0)
            context.user_data.pop("pending_topic", None)
            # Дальше обрабатываем кнопку как обычно
        else:
            await _handle_support_input(update, context, support_mode)
            return

    # ── 4. Кнопки Reply-клавиатуры ───────────────────────────────────────────
    if text == "🔄 Перезапуск":
        await start(update, context)
    elif text == "🆘 Помощь":
        await _help_handler(update, context)
    elif text == "📖 Справка":
        await _faq_main_handler(update, context)


# ===========================================================================
# Техническая поддержка — меню помощи (пользовательская сторона)
# ===========================================================================

async def _help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет новое сообщение с меню раздела поддержки."""
    user_id = update.effective_user.id
    text, keyboard = _build_help_menu(user_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


# Callback: «🔙 Назад» в меню поддержки — закрывает меню
async def help_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Используйте кнопки меню для навигации.")


# Callback: возврат к меню поддержки из листинга тикетов
async def help_show_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    text, keyboard = _build_help_menu(user_id)
    await query.answer()
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


# Callback: «✍️ Написать сообщение» — запуск создания нового тикета
async def help_new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    # ── Проверка: нет ли уже открытого тикета ──
    if db.get_open_ticket(user_id):
        await query.answer(
            "У вас уже есть открытый тикет. Закройте его перед созданием нового.",
            show_alert=True
        )
        return

    # ── Дневной лимит ──
    daily = db.get_ticket_count_today(user_id)
    if daily >= SUPPORT_DAILY_LIMIT:
        await query.answer(
            f"Лимит тикетов на сегодня исчерпан ({daily}/{SUPPORT_DAILY_LIMIT}). "
            "Попробуйте завтра.",
            show_alert=True
        )
        return

    # ── Кулдаун ──
    minutes_left = db.get_minutes_until_next_ticket(user_id, SUPPORT_COOLDOWN_MINUTES)
    if minutes_left > 0:
        await query.answer(
            f"Новый тикет можно открыть через {minutes_left} мин.",
            show_alert=True
        )
        return

    # ── Всё чисто — переводим в режим ввода темы ──
    db.set_support_mode(user_id, 1)
    await query.answer()
    await query.edit_message_text(
        "✏️ <b>Введите тему вашего вопроса:</b>\n\n"
        "Кратко опишите суть обращения (минимум 3 символа).\n\n"
        "<i>Для отмены нажмите «🔄 Перезапуск».</i>",
        parse_mode="HTML"
    )


# ===========================================================================
# Обработка текстового ввода пользователя в режиме поддержки
# ===========================================================================

async def _handle_support_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: int
):
    """
    mode=1 — ввод темы (шаг 1 нового тикета)
    mode=2 — ввод сообщения (шаг 2 нового тикета)
    mode=3 — ответ в открытый тикет
    """
    user = update.effective_user
    user_id = user.id
    text = update.message.text

    # ── mode=1: тема вопроса ──────────────────────────────────────────────────
    if mode == 1:
        topic = text.strip()
        if len(topic) < SUBJECT_MIN_LEN:
            await update.message.reply_text(
                f"⚠️ Тема слишком короткая. Введите не менее {SUBJECT_MIN_LEN} символов:\n\n"
                "<i>Для отмены нажмите «🔄 Перезапуск».</i>",
                parse_mode="HTML"
            )
            # Остаёмся в mode=1
            return

        context.user_data["pending_topic"] = topic
        db.set_support_mode(user_id, 2)
        await update.message.reply_text(
            f"✅ Тема принята: <b>{topic}</b>\n\n"
            "Теперь опишите вашу проблему подробнее:\n\n"
            "<i>Для отмены нажмите «🔄 Перезапуск».</i>",
            parse_mode="HTML"
        )
        return

    # ── mode=2: сообщение (тело тикета) ──────────────────────────────────────
    if mode == 2:
        subject = context.user_data.pop("pending_topic", "Без темы")
        username_str = f"@{user.username}" if user.username else user.first_name

        ticket_id = db.create_ticket(user_id, subject, username_str)
        db.add_ticket_message(ticket_id, "user", text)
        db.set_support_mode(user_id, 0)

        await update.message.reply_text(
            f"✅ <b>Тикет #{ticket_id:04d} создан.</b>\n"
            f"📌 Тема: {subject}\n\n"
            "Мы получили ваше обращение и ответим в ближайшее время.",
            parse_mode="HTML"
        )
        await _notify_admins_new_message(context, ticket_id, user, text)
        return

    # ── mode=3: ответ в существующий тикет ───────────────────────────────────
    if mode == 3:
        ticket = db.get_open_ticket(user_id)
        if not ticket:
            db.set_support_mode(user_id, 0)
            await update.message.reply_text(
                "❌ Тикет был закрыт до отправки вашего сообщения. "
                "Вы можете открыть новый через «🆘 Помощь»."
            )
            return

        db.add_ticket_message(ticket["id"], "user", text)
        db.set_support_mode(user_id, 0)

        await update.message.reply_text("✅ Ваше сообщение отправлено.")
        await _notify_admins_new_message(context, ticket["id"], user, text)


# ===========================================================================
# Техническая поддержка — пользовательские callback-кнопки
# ===========================================================================

# Кнопка «📝 Написать / Ответить» у пользователя (из меню помощи или просмотра тикета)
async def user_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    ticket_id = int(query.data.split(":")[1])
    ticket = db.get_ticket_by_id(ticket_id)

    if not ticket or ticket["status"] != "open":
        await query.answer("❌ Этот тикет уже закрыт.", show_alert=True)
        return

    await query.answer()
    db.set_support_mode(user_id, 3)
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "✏️ Напишите ваш ответ:\n\n"
            "<i>Для отмены нажмите «🔄 Перезапуск».</i>"
        ),
        parse_mode="HTML"
    )


# Кнопка «✅ Закрыть тикет» у пользователя
async def user_close_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id

    ticket_id = int(query.data.split(":")[1])
    ticket = db.get_ticket_by_id(ticket_id)

    if not ticket or ticket["status"] != "open":
        await query.answer("❌ Тикет уже закрыт.", show_alert=True)
        return

    await query.answer()

    db.close_ticket(ticket_id, "user")
    db.set_last_ticket_closed(user_id)
    db.set_support_mode(user_id, 0)
    context.user_data.pop("pending_topic", None)

    header = _format_ticket_header(ticket)
    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ <b>Тикет {header} закрыт.</b> Спасибо за обращение!",
        parse_mode="HTML"
    )

    # Уведомляем администраторов (пропускаем если admin_id == user_id)
    username = f"@{user.username}" if user.username else user.first_name
    for admin_id in ADMIN_IDS:
        if admin_id == user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"✅ <b>Тикет {header}</b> закрыт пользователем {username}.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")


# ===========================================================================
# Техническая поддержка — листинг и просмотр тикетов
# ===========================================================================

async def tickets_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает список тикетов (редактирует текущее сообщение).
    Пользователи видят только свои тикеты; администраторы — все.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    page = int(query.data.split(":")[1])
    is_admin = user_id in ADMIN_IDS

    await query.answer()

    if is_admin:
        total = db.get_all_ticket_count()
        tickets = db.get_all_tickets(offset=page * TICKETS_PER_PAGE, limit=TICKETS_PER_PAGE)
        title = "Все тикеты"
    else:
        total = db.get_user_ticket_count(user_id)
        tickets = db.get_user_tickets(user_id, offset=page * TICKETS_PER_PAGE, limit=TICKETS_PER_PAGE)
        title = "Ваши тикеты"

    total_pages = max(1, (total + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE)

    if not tickets:
        text = f"📋 <b>{title}</b>\n\nТикетов пока нет."
    else:
        text = f"📋 <b>{title}</b> — стр. {page + 1} / {total_pages}"

    keyboard = []

    for t in tickets:
        status_icon = "🟢" if t["status"] == "open" else "🔴"
        subject = t["subject"] or "Без темы"
        # Обрезаем тему: для администратора оставляем меньше места (нужно под имя)
        if is_admin:
            label_subject = subject[:28] + ("…" if len(subject) > 28 else "")
            label = f"{status_icon} #{t['id']:04d} — {label_subject} ({t['username'] or '?'})"
        else:
            label_subject = subject[:38] + ("…" if len(subject) > 38 else "")
            label = f"{status_icon} #{t['id']:04d} — {label_subject}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"tview:{t['id']}")])

    # Навигационная строка
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"tlist:{page - 1}"))
    if (page + 1) * TICKETS_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"tlist:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="help_show")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def ticket_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает детальный просмотр тикета (редактирует текущее сообщение).
    Пользователь может видеть только свои тикеты; администратор — любые.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    ticket_id = int(query.data.split(":")[1])
    is_admin = user_id in ADMIN_IDS

    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket:
        await query.answer("Тикет не найден.", show_alert=True)
        return

    # Защита: обычный пользователь не может смотреть чужие тикеты
    if not is_admin and ticket["user_id"] != user_id:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    await query.answer()

    subject = ticket["subject"] or "Без темы"
    status_text = "🟢 Открыт" if ticket["status"] == "open" else "🔴 Закрыт"
    created = ticket["created_at"][:10] if ticket["created_at"] else "—"

    text = f"📋 <b>Тикет #{ticket_id:04d} — {subject}</b>\n"
    if is_admin:
        text += f"👤 {ticket['username'] or '?'} (ID: <code>{ticket['user_id']}</code>)\n"
    text += f"Статус: {status_text} | Создан: {created}\n"

    # Последние сообщения
    messages = db.get_last_ticket_messages(ticket_id, n=3)
    if messages:
        text += "\n─────────────────\n"
        for m in messages:
            if m["sender_type"] == "admin":
                sender = SUPPORT_NAME
            else:
                # Для администратора показываем имя пользователя, для пользователя — «Вы»
                sender = ticket["username"] or "Пользователь" if is_admin else "Вы"
            preview = m["text"][:120] + ("…" if len(m["text"]) > 120 else "")
            text += f"<b>{sender}:</b> {preview}\n"
        text += "─────────────────"

    keyboard = []

    if ticket["status"] == "open":
        if is_admin:
            keyboard.append([
                InlineKeyboardButton(
                    "✉️ Ответить",
                    callback_data=f"admin_reply:{ticket_id}:{ticket['user_id']}"
                ),
                InlineKeyboardButton(
                    "✅ Закрыть",
                    callback_data=f"admin_close:{ticket_id}:{ticket['user_id']}"
                ),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(
                    "📝 Написать",
                    callback_data=f"support_reply:{ticket_id}"
                ),
                InlineKeyboardButton(
                    "✅ Закрыть",
                    callback_data=f"user_close:{ticket_id}"
                ),
            ])

    keyboard.append([InlineKeyboardButton("🔙 К списку", callback_data="tlist:0")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# ===========================================================================
# Техническая поддержка — административные callback-кнопки
# ===========================================================================

# Кнопка «✉️ Ответить» у администратора
async def admin_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = update.effective_user.id

    if admin_id not in ADMIN_IDS:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    parts = query.data.split(":")
    ticket_id = int(parts[1])
    user_id = int(parts[2])

    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] != "open":
        await query.answer("❌ Тикет уже закрыт.", show_alert=True)
        return

    await query.answer("✏️ Режим ответа активирован.")

    context.user_data["pending_reply"] = {
        "ticket_id": ticket_id,
        "user_id": user_id,
    }

    header = _format_ticket_header(ticket)
    await context.bot.send_message(
        chat_id=admin_id,
        text=(
            f"✏️ <b>Режим ответа — тикет {header}</b>\n\n"
            "Следующее ваше сообщение будет отправлено пользователю.\n\n"
            "<i>Для отмены отправьте /cancel</i>"
        ),
        parse_mode="HTML"
    )


# Кнопка «✅ Закрыть тикет» у администратора
async def admin_close_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = update.effective_user.id

    if admin_id not in ADMIN_IDS:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    parts = query.data.split(":")
    ticket_id = int(parts[1])
    user_id = int(parts[2])

    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] != "open":
        await query.answer("❌ Тикет уже закрыт.", show_alert=True)
        return

    await query.answer()

    db.close_ticket(ticket_id, "admin")
    db.set_last_ticket_closed(user_id)
    db.set_support_mode(user_id, 0)
    context.user_data.pop("pending_reply", None)

    header = _format_ticket_header(ticket)

    # Уведомляем пользователя (пропускаем если user_id == admin_id)
    if user_id != admin_id:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ <b>Тикет {header} закрыт администратором.</b>\n\n"
                    "Если проблема возникнет снова — вы всегда можете открыть новый тикет."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    # Уведомляем всех администраторов
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=f"✅ <b>Тикет {header}</b> закрыт администратором.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {aid}: {e}")


async def _handle_admin_reply_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict
):
    """
    Обрабатывает сообщение администратора в режиме ответа на тикет.
    Вызывается из handle_reply_buttons при наличии pending_reply.
    """
    ticket_id = pending["ticket_id"]
    user_id = pending["user_id"]
    admin_id = update.effective_user.id
    reply_text = update.message.text

    # Очищаем pending_reply сразу
    context.user_data.pop("pending_reply", None)

    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] != "open":
        await update.message.reply_text(
            f"❌ Тикет #{ticket_id:04d} уже закрыт. Ответ не отправлен.",
            parse_mode="HTML"
        )
        return

    db.add_ticket_message(ticket_id, "admin", reply_text)
    header = _format_ticket_header(ticket)

    # ── Отправляем ответ пользователю ──
    user_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Ответить", callback_data=f"support_reply:{ticket_id}"),
            InlineKeyboardButton("✅ Закрыть тикет", callback_data=f"user_close:{ticket_id}"),
        ]
    ])
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"💬 <b>Ответ от {SUPPORT_NAME}:</b>\n"
                f"─────────────────\n"
                f"{reply_text}"
            ),
            reply_markup=user_keyboard,
            parse_mode="HTML"
        )
    except (Forbidden, BadRequest) as e:
        logger.error(f"Не удалось доставить ответ пользователю {user_id}: {e}")
        await update.message.reply_text(
            f"❌ Не удалось доставить ответ пользователю (ID: {user_id}). "
            "Возможно, он заблокировал бота."
        )
        return

    await update.message.reply_text(
        f"✅ Ответ на тикет {header} отправлен.", parse_mode="HTML"
    )

    # ── Уведомляем других администраторов ──
    try:
        user_chat = await context.bot.get_chat(user_id)
        username = f"@{user_chat.username}" if user_chat.username else user_chat.first_name
    except Exception:
        username = f"ID:{user_id}"

    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✉️ Ответить снова", callback_data=f"admin_reply:{ticket_id}:{user_id}"),
            InlineKeyboardButton("✅ Закрыть тикет", callback_data=f"admin_close:{ticket_id}:{user_id}"),
        ]
    ])

    for aid in ADMIN_IDS:
        if aid == admin_id:
            continue  # Этот админ только что ответил
        if aid == user_id:
            continue  # Пользователь является администратором — получит ответ выше
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=(
                    f"📨 <b>Тикет {header}</b> — ответ отправлен\n"
                    f"👤 {username}\n"
                    f"─────────────────\n"
                    f"<b>Ответ команды:</b> {reply_text}"
                ),
                reply_markup=admin_keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {aid}: {e}")


# ===========================================================================
# Уведомление администраторов о новом сообщении пользователя
# ===========================================================================

async def _notify_admins_new_message(
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    user,
    text: str
):
    """
    Рассылает администраторам уведомление о новом сообщении пользователя в тикете.
    Пропускает администраторов, чей ID совпадает с user_id (один человек в обоих ролях).
    """
    user_id = user.id
    username = f"@{user.username}" if user.username else user.first_name

    ticket = db.get_ticket_by_id(ticket_id)
    header = _format_ticket_header(ticket) if ticket else f"#{ticket_id:04d}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✉️ Ответить", callback_data=f"admin_reply:{ticket_id}:{user_id}"),
            InlineKeyboardButton("✅ Закрыть тикет", callback_data=f"admin_close:{ticket_id}:{user_id}"),
        ]
    ])

    msg_text = (
        f"📨 <b>Тикет {header}</b>\n"
        f"👤 {username} (ID: <code>{user_id}</code>)\n"
        f"─────────────────\n"
        f"{text}"
    )

    for admin_id in ADMIN_IDS:
        if admin_id == user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=msg_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")


# ===========================================================================
# /cancel
# ===========================================================================

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        if context.user_data.pop("pending_reply", None):
            await update.message.reply_text("❌ Режим ответа отменён.")
        else:
            await update.message.reply_text("Нет активных действий для отмены.")
    else:
        db.set_support_mode(user_id, 0)
        context.user_data.pop("pending_topic", None)
        await update.message.reply_text("Действие отменено. Используйте меню для навигации.")


# ===========================================================================
# Остальные инлайн-кнопки главного меню
# ===========================================================================

async def about_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return
    text = (
        "Официальный бот мессенджера MrX.\n\n"
        "Здесь публикуются важные новости проекта, анонсы обновлений и технические уведомления. "
        "Также бот принимает обращения в службу поддержки — если что-то не работает или есть вопросы "
        "по приложению, пишите сюда.\n\n"
        "Мы читаем каждое сообщение.\n\n"
        "t.me/redmrxgram"
    )
    await query.edit_message_text(
        text,
        reply_markup=get_main_menu_keyboard(update.effective_user.id, exclude="about"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def links_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return
    msg = "🔗 <b>Наши соцсети и сайт:</b>\n\n"
    for name, link in SOCIAL_LINKS.items():
        display_name = name.replace("Telegram Chat", "Telegram")
        msg += f"{display_name}: {link}\n"
    await query.edit_message_text(
        msg,
        reply_markup=get_main_menu_keyboard(update.effective_user.id, exclude="links"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def toggle_notifications_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    db.toggle_notifications(user_id)

    data = query.data
    if data == "toggle_notify_about":
        exclude = "about"
    elif data == "toggle_notify_links":
        exclude = "links"
    else:
        exclude = None

    await query.edit_message_reply_markup(
        reply_markup=get_main_menu_keyboard(user_id, exclude=exclude)
    )


async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_subscription(update, context):
        return
    user_id = update.effective_user.id
    await query.edit_message_text(
        WELCOME_TEXT,
        reply_markup=get_main_menu_keyboard(user_id),
        disable_web_page_preview=True
    )


# ===========================================================================
# Административные команды
# ===========================================================================

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    user_count = db.get_user_count()
    posts_count = channel_scanner.get_next_post_number() - 1
    _, media_count = channel_scanner.get_scan_stats()
    ticket_count = db.get_all_ticket_count()

    await update.message.reply_text(
        f"📊 <b>Статистика бота:</b>\n\n"
        f"👥 Пользователей: {user_count}\n"
        f"📝 Сохранено постов: {posts_count}\n"
        f"📎 Медиафайлов: {media_count}\n"
        f"🎫 Тикетов всего: {ticket_count}",
        parse_mode="HTML"
    )


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "ℹ️ Использование: ответьте на любое сообщение командой /broadcast, "
            "чтобы разослать его всем пользователям."
        )
        return

    original_msg = update.message.reply_to_message
    users = db.get_all_users()
    success, failed = 0, 0
    status_msg = await update.message.reply_text(
        f"📤 Начинаю рассылку на {len(users)} пользователей..."
    )

    for uid in users:
        try:
            await original_msg.copy(chat_id=uid)
            success += 1
        except (Forbidden, BadRequest) as e:
            logger.warning(f"Не удалось отправить пользователю {uid}: {e}")
            failed += 1
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {uid}: {e}")
            failed += 1

    await status_msg.edit_text(
        f"✅ Рассылка завершена.\n\n"
        f"📊 Всего пользователей: {len(users)}\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {failed}"
    )


async def admin_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "ℹ️ Использование: ответьте на любое сообщение командой /notify, "
            "чтобы разослать его пользователям с включёнными уведомлениями."
        )
        return

    original_msg = update.message.reply_to_message
    users = db.get_all_users()
    notified, skipped, failed = 0, 0, 0
    status_msg = await update.message.reply_text("📤 Отправка уведомлений...")

    for uid in users:
        if not db.get_notification_status(uid):
            skipped += 1
            continue
        try:
            await original_msg.copy(chat_id=uid)
            notified += 1
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {uid}: {e}")
            failed += 1

    await status_msg.edit_text(
        f"✅ Уведомления отправлены.\n\n"
        f"📊 Всего пользователей: {len(users)}\n"
        f"🔔 Получили уведомление: {notified}\n"
        f"🔕 Пропущено (выключены): {skipped}\n"
        f"❌ Ошибок: {failed}"
    )


# ===========================================================================
# Защита от добавления в чужие чаты
# ===========================================================================

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    new_status = update.my_chat_member.new_chat_member.status

    if new_status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        return
    if chat.type == "private":
        return
    if str(chat.id) == CHANNEL_ID:
        logger.info(f"Бот находится в целевом канале {CHANNEL_ID}")
        return

    logger.warning(
        f"Бот добавлен в неразрешённый чат: {chat.id} ({chat.title or 'без названия'}). "
        "Попытка выхода..."
    )
    try:
        await context.bot.leave_chat(chat.id)
        logger.info(f"Успешно покинут чат {chat.id}")
    except Exception as e:
        logger.error(
            f"Не удалось покинуть чат {chat.id}. "
            f"Тип чата: {chat.type}. Ошибка: {type(e).__name__} - {e}"
        )


# ===========================================================================
# Обработчики событий канала (логирование)
# ===========================================================================

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return
    next_num = channel_scanner.get_next_post_number()
    logger.info(f"🆕 Новый пост в канале (ID: {message.message_id}). Присваиваем номер #{next_num}")
    _, media_count = await channel_scanner.process_single_post(message, next_num, context.bot)
    logger.info(f"✅ Новый пост #{next_num} обработан. Медиа: {media_count}")


async def on_edited_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.edited_channel_post
    if not message:
        return
    exists, post_num = channel_scanner.post_exists(message.message_id)
    if not exists:
        post_num = channel_scanner.get_next_post_number()
        logger.warning(
            f"Редактирование неизвестного поста ID {message.message_id}, "
            f"сохраняем как новый #{post_num}"
        )
    logger.info(f"✏️ Пост #{post_num} (ID {message.message_id}) отредактирован. Обновляем...")
    _, media_count = await channel_scanner.process_single_post(
        message, post_num, context.bot, force_overwrite=True
    )
    logger.info(f"✅ Пост #{post_num} обновлён. Новых медиа: {media_count}")


# ===========================================================================
# Справка (FAQ)
# ===========================================================================

# ── Вспомогательные функции ──────────────────────────────────────────────────

def _faq_main_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура главной страницы справки: 4 раздела + Назад."""
    rows = []
    for section in faq_data.SECTIONS:
        rows.append([InlineKeyboardButton(section["title"], callback_data=f"faq_section:{section['id']}:0")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="faq_back")])
    return InlineKeyboardMarkup(rows)


def _faq_section_keyboard(section_id: str, page: int) -> InlineKeyboardMarkup:
    """
    Клавиатура страницы раздела: список блоков (с пагинацией) + Назад.
    """
    section = faq_data.SECTIONS_BY_ID.get(section_id)
    if not section:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="faq_main")]])

    items = section["items"]
    total = len(items)
    start = page * FAQ_ITEMS_PER_PAGE
    end = start + FAQ_ITEMS_PER_PAGE
    page_items = items[start:end]
    total_pages = max(1, (total + FAQ_ITEMS_PER_PAGE - 1) // FAQ_ITEMS_PER_PAGE)

    rows = []
    for item in page_items:
        rows.append([InlineKeyboardButton(item["title"], callback_data=f"faq_item:{item['id']}")])

    # Навигация по страницам раздела (если нужна)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"faq_section:{section_id}:{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="faq_noop"))
    if end < total:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"faq_section:{section_id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="faq_main")])
    return InlineKeyboardMarkup(rows)


def _faq_item_keyboard(item_id: str) -> InlineKeyboardMarkup:
    """Клавиатура открытого блока: только кнопка Назад к разделу."""
    result = faq_data.ITEMS_BY_ID.get(item_id)
    if not result:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="faq_main")]])
    section, _ = result
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data=f"faq_section:{section['id']}:0")]
    ])


# ── Хендлеры ─────────────────────────────────────────────────────────────────

async def _faq_main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply-кнопка «📖 Справка» — отправляет новое сообщение с меню справки."""
    await update.message.reply_text(
        "📖 <b>Справка MrX</b>\n\nВыберите раздел:",
        reply_markup=_faq_main_keyboard(),
        parse_mode="HTML"
    )


async def faq_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback «faq_main» — возврат на главную страницу справки."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📖 <b>Справка MrX</b>\n\nВыберите раздел:",
        reply_markup=_faq_main_keyboard(),
        parse_mode="HTML"
    )


async def faq_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback «faq_back» — закрывает меню справки."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Используйте кнопки меню для навигации.")


async def faq_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback «faq_section:{id}:{page}» — показывает список блоков раздела."""
    query = update.callback_query
    parts = query.data.split(":")
    section_id = parts[1]
    page = int(parts[2])

    section = faq_data.SECTIONS_BY_ID.get(section_id)
    if not section:
        await query.answer("Раздел не найден.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(
        f"📖 <b>{section['title']}</b>\n\nВыберите вопрос:",
        reply_markup=_faq_section_keyboard(section_id, page),
        parse_mode="HTML"
    )


async def faq_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback «faq_item:{id}» — показывает текст конкретного блока."""
    query = update.callback_query
    item_id = query.data.split(":")[1]

    result = faq_data.ITEMS_BY_ID.get(item_id)
    if not result:
        await query.answer("Блок не найден.", show_alert=True)
        return

    _, item = result
    await query.answer()
    await query.edit_message_text(
        item["text"],
        reply_markup=_faq_item_keyboard(item_id),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def faq_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback для кнопки счётчика страниц — ничего не делает."""
    await update.callback_query.answer()
