import os
import re
import asyncio
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Tuple, Optional, Dict, Any

from telegram import Bot, Message
from telegram.error import BadRequest, Forbidden

from config import CHANNEL_ID

logger = logging.getLogger(__name__)

# ------------------------ Настройки ------------------------
BASE_DIR = Path(__file__).parent / "channel_data"
TEXTS_DIR = BASE_DIR / "texts"
MEDIA_DIR = BASE_DIR / "media"
META_DIR = BASE_DIR / "meta"
DELETED_LOG = META_DIR / "deleted_posts.json"

MEDIA_SUBDIRS = {
    'photo': 'photos',
    'video': 'videos',
    'audio': 'audio',
    'document': 'documents',
    'voice': 'voice',
    'video_note': 'video_notes',
    'animation': 'animations',
    'sticker': 'stickers'
}

HISTORY_LIMIT = 100
REQUEST_DELAY = 0.1

# ------------------------ Вспомогательные функции ------------------------
def ensure_directories():
    """Создаёт все необходимые папки."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    TEXTS_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)
    for subdir in MEDIA_SUBDIRS.values():
        (MEDIA_DIR / subdir).mkdir(parents=True, exist_ok=True)

def sanitize_filename(filename: str) -> str:
    if not filename:
        return "file"
    name = re.sub(r'[\\/*?:"<>|]', "", filename)
    return name if name else "file"

def get_extension_from_mime(mime_type: str) -> str:
    if not mime_type:
        return ""
    mime_map = {
        "jpeg": ".jpg", "jpg": ".jpg",
        "png": ".png",
        "gif": ".gif",
        "mp4": ".mp4",
        "mpeg": ".mp3", "mp3": ".mp3",
        "ogg": ".ogg",
        "pdf": ".pdf",
        "apk": ".apk", "vnd.android.package-archive": ".apk"
    }
    for key, ext in mime_map.items():
        if key in mime_type:
            return ext
    return ""

def get_next_post_number() -> int:
    """Возвращает следующий доступный номер поста (максимальный существующий + 1)."""
    if not TEXTS_DIR.exists():
        return 1
    files = list(TEXTS_DIR.glob("*.txt"))
    if not files:
        return 1
    max_num = 0
    for f in files:
        try:
            # Имя файла может быть "0001.txt" или "0001_edited.txt", берём первые 4 цифры
            num = int(f.stem[:4])
            if num > max_num:
                max_num = num
        except ValueError:
            continue
    return max_num + 1

def get_post_meta_file(post_number: int) -> Path:
    return META_DIR / f"{post_number:04d}.json"

def load_post_meta(post_number: int) -> Optional[Dict[str, Any]]:
    meta_file = get_post_meta_file(post_number)
    if not meta_file.exists():
        return None
    try:
        with open(meta_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def save_post_meta(post_number: int, message: Message):
    meta = {
        'message_id': message.message_id,
        'date': message.date.isoformat(),
        'edit_date': message.edit_date.isoformat() if message.edit_date else None,
        'has_text': bool(message.text or message.caption),
        'media_types': []
    }
    if message.photo: meta['media_types'].append('photo')
    if message.video: meta['media_types'].append('video')
    if message.audio: meta['media_types'].append('audio')
    if message.document: meta['media_types'].append('document')

    with open(get_post_meta_file(post_number), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def post_exists(message_id: int) -> Tuple[bool, Optional[int]]:
    """Проверяет, существует ли уже пост с данным message_id. Возвращает (True, номер_поста) или (False, None)."""
    if not META_DIR.exists():
        return False, None
    for meta_file in META_DIR.glob("*.json"):
        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get('message_id') == message_id:
                    num = int(meta_file.stem)
                    return True, num
        except:
            continue
    return False, None

def clear_old_media(post_number: int):
    """Удаляет все старые медиафайлы, связанные с данным номером поста."""
    pattern = f"{post_number:04d}*"
    for subdir in MEDIA_SUBDIRS.values():
        target_dir = MEDIA_DIR / subdir
        for f in target_dir.glob(pattern):
            try:
                f.unlink()
                logger.debug(f"Удалён старый файл: {f}")
            except Exception as e:
                logger.warning(f"Не удалось удалить {f}: {e}")

def save_deleted_post_info(post_number: int, message_id: int, reason: str = "deleted"):
    """Сохраняет информацию об удалённом посте в JSON-лог."""
    deleted_entry = {
        "post_number": post_number,
        "message_id": message_id,
        "reason": reason,
        "timestamp": datetime.now().isoformat()
    }
    existing = []
    if DELETED_LOG.exists():
        try:
            with open(DELETED_LOG, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except:
            existing = []
    existing.append(deleted_entry)
    with open(DELETED_LOG, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info(f"Информация об удалённом посте #{post_number} сохранена в {DELETED_LOG}")

# ------------------------ Обработка одного поста ------------------------
async def process_single_post(message: Message, post_number: int, bot: Bot, force_overwrite: bool = False) -> Tuple[int, int]:
    """
    Обрабатывает одно сообщение и сохраняет текст и медиа.
    Если пост уже существует и не было изменений, пропускает (если не force_overwrite).
    Возвращает (1 если обработан, 0 если пропущен), количество новых медиа.
    """
    ensure_directories()

    exists, existing_num = post_exists(message.message_id)
    if exists and existing_num != post_number:
        logger.warning(f"Пост {message.message_id} уже сохранён под номером {existing_num}, пропускаем.")
        return 0, 0

    meta = load_post_meta(post_number) if exists else None
    is_edited = message.edit_date is not None
    if meta and meta.get('edit_date') == (message.edit_date.isoformat() if message.edit_date else None):
        if not force_overwrite:
            logger.debug(f"Пост #{post_number} (ID {message.message_id}) не изменился, пропускаем.")
            return 0, 0

    if exists and (is_edited or force_overwrite):
        clear_old_media(post_number)
        logger.info(f"Пост #{post_number} был изменён, старые медиа удалены.")

    # Сохраняем текст с пометкой об редактировании
    text_content = message.text or message.caption or ""
    if text_content or (exists and meta and meta.get('has_text')):
        text_filename = TEXTS_DIR / f"{post_number:04d}.txt"
        if is_edited:
            text_content = f"[EDITED at {message.edit_date}]\n\n{text_content}"
        with open(text_filename, 'w', encoding='utf-8') as f:
            f.write(text_content)
        logger.info(f"💬 Текст поста #{post_number} сохранён в {text_filename}")

    # Собираем медиа
    media_objects = []
    if message.photo:
        media_objects.append(('photo', message.photo[-1]))
    if message.video:
        media_objects.append(('video', message.video))
    if message.audio:
        media_objects.append(('audio', message.audio))
    if message.document:
        media_objects.append(('document', message.document))
    if message.voice:
        media_objects.append(('voice', message.voice))
    if message.video_note:
        media_objects.append(('video_note', message.video_note))
    if message.animation:
        media_objects.append(('animation', message.animation))
    if message.sticker:
        media_objects.append(('sticker', message.sticker))

    media_count = 0
    for idx, (media_type, media_obj) in enumerate(media_objects):
        try:
            base_name = f"{post_number:04d}"
            if idx > 0:
                base_name += f".{idx}"

            file_ext = ""
            if hasattr(media_obj, 'file_name') and media_obj.file_name:
                clean_name = sanitize_filename(media_obj.file_name)
                if '.' in clean_name:
                    file_ext = os.path.splitext(clean_name)[1]
            if not file_ext and hasattr(media_obj, 'mime_type'):
                file_ext = get_extension_from_mime(media_obj.mime_type)

            subdir = MEDIA_SUBDIRS.get(media_type, 'documents')
            target_dir = MEDIA_DIR / subdir
            full_path = target_dir / f"{base_name}{file_ext}"

            file = await bot.get_file(media_obj.file_id)
            await file.download_to_drive(custom_path=full_path)

            media_count += 1
            logger.info(f"📎 Медиа #{idx+1} поста #{post_number} сохранено: {full_path}")
            await asyncio.sleep(0.05)

        except Exception as e:
            logger.error(f"Ошибка скачивания медиа #{idx} поста #{post_number}: {e}")

    save_post_meta(post_number, message)
    return 1, media_count

# ------------------------ Полное сканирование истории ------------------------
async def scan_channel(bot: Bot) -> Tuple[int, int]:
    """Сканирует всю историю канала, обновляя существующие посты при изменениях."""
    ensure_directories()

    total_processed = 0
    total_media = 0
    last_message_id = 0  # 0 означает начало с самого нового сообщения

    logger.info(f"Начинаем полное сканирование канала {CHANNEL_ID}...")

    existing_posts = set()
    for meta_file in META_DIR.glob("*.json"):
        existing_posts.add(int(meta_file.stem))

    processed_ids = set()

    while True:
        try:
            messages = []
            # В PTB 22.7 get_chat_history возвращает асинхронный итератор
            # Параметр before_message_id указывает, ДО какого сообщения загружать (более старые)
            # Передаём None для первого запроса
            async for message in bot.get_chat_history(
                chat_id=CHANNEL_ID,
                limit=HISTORY_LIMIT,
                before_message_id=last_message_id if last_message_id != 0 else None
            ):
                messages.append(message)

            if not messages:
                break

            # Сообщения приходят от новых к старым, переворачиваем для хронологической обработки
            for message in reversed(messages):
                last_message_id = message.message_id
                processed_ids.add(message.message_id)

                exists, post_num = post_exists(message.message_id)
                if not exists:
                    post_num = get_next_post_number()
                    logger.info(f"Новый пост (ID {message.message_id}) получил номер #{post_num}")
                else:
                    logger.debug(f"Пост #{post_num} (ID {message.message_id}) уже существует, проверяем изменения...")

                processed, media_added = await process_single_post(message, post_num, bot)
                if processed:
                    total_processed += 1
                    total_media += media_added
                    if exists:
                        existing_posts.discard(post_num)

            await asyncio.sleep(REQUEST_DELAY)

        except Exception as e:
            logger.error(f"Ошибка сканирования: {e}", exc_info=True)
            break

    # Проверяем удалённые посты
    for missing_post_num in existing_posts:
        meta = load_post_meta(missing_post_num)
        if meta:
            msg_id = meta.get('message_id')
            if msg_id not in processed_ids:
                save_deleted_post_info(missing_post_num, msg_id)
                logger.info(f"Пост #{missing_post_num} (ID {msg_id}) не найден в канале — помечен как удалённый.")

    logger.info(f"✅ Полное сканирование завершено. Обработано постов: {total_processed}, новых медиа: {total_media}")
    return total_processed, total_media

def get_scan_stats() -> Tuple[int, int]:
    text_count = len(list(TEXTS_DIR.glob("*.txt"))) if TEXTS_DIR.exists() else 0
    media_count = 0
    if MEDIA_DIR.exists():
        for subdir in MEDIA_SUBDIRS.values():
            media_count += len(list((MEDIA_DIR / subdir).glob("*")))
    return text_count, media_count