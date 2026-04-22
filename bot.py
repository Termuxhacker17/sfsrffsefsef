import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import BOT_TOKEN, CHANNEL_ID
from handlers import (
    # Основные
    start,
    check_subscription_callback,
    about_callback,
    links_callback,
    toggle_notifications_callback,
    back_to_main_callback,
    handle_reply_buttons,
    # Административные команды
    admin_stats,
    admin_broadcast,
    admin_notify,
    cancel_command,
    # Поддержка — меню помощи
    help_back_callback,
    help_show_callback,
    help_new_callback,
    # Поддержка — тикеты (листинг и просмотр)
    tickets_list_callback,
    ticket_view_callback,
    # Поддержка — пользовательские кнопки
    user_reply_callback,
    user_close_ticket_callback,
    # Поддержка — административные кнопки
    admin_reply_callback,
    admin_close_ticket_callback,
    # Справка (FAQ)
    faq_main_callback,
    faq_back_callback,
    faq_section_callback,
    faq_item_callback,
    faq_noop_callback,
    # События канала и чатов
    on_my_chat_member,
    on_channel_post,
    on_edited_channel_post,
)
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Исключение при обработке обновления:", exc_info=context.error)


def main():
    db.init_db()
    logger.info("База данных инициализирована.")

    application = Application.builder().token(BOT_TOKEN).build()

    # ── Команды ──────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("start",     start,           filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("stats",     admin_stats,     filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("broadcast", admin_broadcast, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("notify",    admin_notify,    filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("cancel",    cancel_command,  filters=filters.ChatType.PRIVATE))

    # ── Инлайн-кнопки главного меню ──────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(check_subscription_callback,   pattern=r"^check_sub$"))
    application.add_handler(CallbackQueryHandler(about_callback,                pattern=r"^about$"))
    application.add_handler(CallbackQueryHandler(links_callback,                pattern=r"^links$"))
    application.add_handler(CallbackQueryHandler(toggle_notifications_callback, pattern=r"^toggle_notify$"))
    application.add_handler(CallbackQueryHandler(toggle_notifications_callback, pattern=r"^toggle_notify_about$"))
    application.add_handler(CallbackQueryHandler(toggle_notifications_callback, pattern=r"^toggle_notify_links$"))
    application.add_handler(CallbackQueryHandler(back_to_main_callback,         pattern=r"^back_to_main$"))

    # ── Инлайн-кнопки меню поддержки ─────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(help_back_callback, pattern=r"^help_back$"))
    application.add_handler(CallbackQueryHandler(help_show_callback, pattern=r"^help_show$"))
    application.add_handler(CallbackQueryHandler(help_new_callback,  pattern=r"^help_new$"))

    # ── Листинг и просмотр тикетов ────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(tickets_list_callback, pattern=r"^tlist:\d+$"))
    application.add_handler(CallbackQueryHandler(ticket_view_callback,  pattern=r"^tview:\d+$"))

    # ── Пользовательские кнопки тикета ───────────────────────────────────────
    application.add_handler(CallbackQueryHandler(user_reply_callback,        pattern=r"^support_reply:\d+$"))
    application.add_handler(CallbackQueryHandler(user_close_ticket_callback, pattern=r"^user_close:\d+$"))

    # ── Административные кнопки тикета ───────────────────────────────────────
    application.add_handler(CallbackQueryHandler(admin_reply_callback,        pattern=r"^admin_reply:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_close_ticket_callback, pattern=r"^admin_close:\d+:\d+$"))

    # ── Справка (FAQ) ─────────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(faq_main_callback,    pattern=r"^faq_main$"))
    application.add_handler(CallbackQueryHandler(faq_back_callback,    pattern=r"^faq_back$"))
    application.add_handler(CallbackQueryHandler(faq_noop_callback,    pattern=r"^faq_noop$"))
    application.add_handler(CallbackQueryHandler(faq_section_callback, pattern=r"^faq_section:[a-z]+:\d+$"))
    application.add_handler(CallbackQueryHandler(faq_item_callback,    pattern=r"^faq_item:[a-z_]+$"))

    # ── Защита от чужих чатов ─────────────────────────────────────────────────
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # ── События канала ────────────────────────────────────────────────────────
    application.add_handler(
        MessageHandler(
            filters.Chat(int(CHANNEL_ID)) & filters.UpdateType.CHANNEL_POST,
            on_channel_post
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Chat(int(CHANNEL_ID)) & filters.UpdateType.EDITED_CHANNEL_POST,
            on_edited_channel_post
        )
    )

    # ── Текстовые сообщения в личном чате ────────────────────────────────────
    # Единая точка входа: кнопки меню, режимы поддержки (тема/сообщение/ответ),
    # режим ответа администратора — всё маршрутизируется внутри handle_reply_buttons.
    application.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_reply_buttons)
    )

    application.add_error_handler(error_handler)

    logger.info("Бот запущен и ожидает новые посты...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
